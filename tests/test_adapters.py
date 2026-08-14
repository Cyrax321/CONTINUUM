from __future__ import annotations

import pytest

from continuum.adapters import AgentAdapter, GenericAgentAdapter
from continuum.environment import StaticProvider, capture
from continuum.models import Goal, Progress, RecoveryMode, SemanticState
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    return storage


def test_generic_adapter_implements_agent_adapter(store: SQLiteStorage) -> None:
    adapter = GenericAgentAdapter(store)
    assert isinstance(adapter, AgentAdapter)


def test_start_run_and_capture_restore_round_trip(store: SQLiteStorage) -> None:
    adapter = GenericAgentAdapter(store)

    run = adapter.start_run(goal="Analyze documents", run_id="run_101")
    assert run.run_id == "run_101"
    assert run.goal == "Analyze documents"

    initial_state = SemanticState(
        run_id="run_101",
        goal=Goal(description="Analyze documents"),
        progress=Progress(total=100, completed=25),
    )

    env = capture("run_101", StaticProvider(dataset="v1"))
    chk = adapter.capture_state("run_101", initial_state, environment=env, reason="initial batch")
    assert chk.version == 0  # version numbering starts at 0

    restored_state = adapter.restore_state("run_101")
    assert restored_state.run_id == "run_101"
    assert restored_state.progress.completed == 25
    assert restored_state.goal.description == "Analyze documents"


def test_intercept_action_deduplicates_repeated_call(store: SQLiteStorage) -> None:
    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Process payment", run_id="run_102")

    call_count = 0

    def perform_charge() -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"transaction_id": "tx_9982", "status": "success"}

    # First call: executes perform_charge
    res1 = adapter.intercept_action(
        "run_102",
        "stripe.charge",
        perform_charge,
        arguments={"amount": 5000, "currency": "usd"},
    )
    assert call_count == 1
    assert res1 == {"transaction_id": "tx_9982", "status": "success"}

    # Second call with same arguments: intercept_action deduplicates via ledger and skips perform_charge
    res2 = adapter.intercept_action(
        "run_102",
        "stripe.charge",
        perform_charge,
        arguments={"amount": 5000, "currency": "usd"},
    )
    assert call_count == 1  # Not incremented!
    assert res2 == {"transaction_id": "tx_9982", "status": "success"}


def test_intercept_action_handles_scalar_return_value(store: SQLiteStorage) -> None:
    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Compute hash", run_id="run_103")

    call_count = 0

    def compute() -> int:
        nonlocal call_count
        call_count += 1
        return 42

    v1 = adapter.intercept_action("run_103", "compute_val", compute, arguments={"x": 10})
    assert v1 == 42
    assert call_count == 1

    v2 = adapter.intercept_action("run_103", "compute_val", compute, arguments={"x": 10})
    assert v2 == 42
    assert call_count == 1  # Deduplicated


def test_a_result_dict_holding_the_envelope_key_survives_the_cache(
    store: SQLiteStorage,
) -> None:
    """A completed action must return the same value on every call.

    The cached path unwraps the envelope key; if a caller's own dict carried
    that key and were stored as-is, the second call would return only that
    member and silently drop the rest.
    """
    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Collide", run_id="run_110")

    call_count = 0

    def action() -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"__return_value__": 42, "other": "payload"}

    first = adapter.intercept_action("run_110", "act.collide", action, arguments={"k": 1})
    second = adapter.intercept_action("run_110", "act.collide", action, arguments={"k": 1})

    assert call_count == 1  # the second call is a pure cache hit
    assert first == {"__return_value__": 42, "other": "payload"}
    assert second == first


def test_a_result_dict_that_is_only_the_envelope_key_survives_the_cache(
    store: SQLiteStorage,
) -> None:
    """The degenerate case: the caller's dict is indistinguishable from an envelope."""
    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Collide", run_id="run_111")

    def action() -> dict[str, object]:
        return {"__return_value__": "mine"}

    first = adapter.intercept_action("run_111", "act.bare", action, arguments={"k": 1})
    second = adapter.intercept_action("run_111", "act.bare", action, arguments={"k": 1})

    assert first == {"__return_value__": "mine"}
    assert second == first


def test_a_nested_envelope_key_survives_the_cache(store: SQLiteStorage) -> None:
    """Only one level is ever wrapped, so only one level is ever unwrapped."""
    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Collide", run_id="run_112")

    payload = {"__return_value__": {"__return_value__": "deep"}}

    first = adapter.intercept_action("run_112", "act.nested", lambda: payload, arguments={"k": 1})
    second = adapter.intercept_action("run_112", "act.nested", lambda: payload, arguments={"k": 1})

    assert first == payload
    assert second == payload


def test_an_ordinary_dict_result_is_still_stored_unwrapped(store: SQLiteStorage) -> None:
    """The envelope must not start wrapping dicts that never needed it."""
    from continuum.actions.ledger import ActionLedger

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Plain", run_id="run_113")

    adapter.intercept_action(
        "run_113",
        "act.plain",
        lambda: {"transaction_id": "tx_1"},
        arguments={"k": 1},
    )

    stored = ActionLedger(store, "run_113").all()
    assert stored[-1].result == {"transaction_id": "tx_1"}


def test_a_raised_action_leaves_the_side_effect_uncertain(store: SQLiteStorage) -> None:
    """An exception from an external call does not prove nothing happened.

    A timeout may mean the request already landed. The action must therefore
    stay uncertain: visible in ledger.pending(), and blocking a clean resume
    until a probe settles it. Recording it as a definite failure would hide it
    from reconciliation and let a retry duplicate the effect.
    """
    from continuum.actions import ActionLedger
    from continuum.models import ActionStatus

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Charge card", run_id="run_105")

    state = SemanticState(run_id="run_105", goal=Goal(description="Charge card"))
    env = capture("run_105", StaticProvider(gateway="v1"))
    adapter.capture_state("run_105", state, environment=env)

    def charge() -> dict[str, str]:
        raise TimeoutError("gateway did not respond after 30s")

    with pytest.raises(TimeoutError):
        adapter.intercept_action("run_105", "stripe.charge", charge, arguments={"amount": 5000})

    ledger = ActionLedger(store, "run_105")
    pending = ledger.pending()
    assert len(pending) == 1, "a timed-out charge must remain unresolved"
    assert pending[0].status is ActionStatus.UNKNOWN
    assert pending[0].side_effect_uncertain

    decision = adapter.resume("run_105", current_environment=env)
    assert decision.mode is not RecoveryMode.RESUME
    assert not decision.safe
    assert decision.uncertain_actions


def test_a_retry_after_a_raised_action_is_refused(store: SQLiteStorage) -> None:
    """The agent must not be allowed to blindly re-run an uncertain effect."""
    from continuum.models import UnknownSideEffect

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Charge card", run_id="run_106")

    calls = 0

    def charge() -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise ConnectionError("connection reset")

    with pytest.raises(ConnectionError):
        adapter.intercept_action("run_106", "stripe.charge", charge, arguments={"amount": 1})

    with pytest.raises(UnknownSideEffect):
        adapter.intercept_action("run_106", "stripe.charge", charge, arguments={"amount": 1})

    assert calls == 1, "the effect must not be re-attempted while its outcome is unknown"


def test_adapter_resume(store: SQLiteStorage) -> None:
    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Task", run_id="run_104")

    state = SemanticState(run_id="run_104", goal=Goal(description="Task"))
    env = capture("run_104", StaticProvider(db="v1"))
    adapter.capture_state("run_104", state, environment=env)

    decision = adapter.resume("run_104", current_environment=env)
    assert decision.mode is RecoveryMode.RESUME
    assert decision.safe
