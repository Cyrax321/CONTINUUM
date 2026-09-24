from __future__ import annotations

import pytest

from continuum.adapters import AgentAdapter, GenericAgentAdapter
from continuum.environment import StaticProvider, capture
from continuum.models import Goal, Progress, RecoveryMode, Run, SemanticState
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


def test_captured_environment_is_declared_with_the_adapters_provenance(
    store: SQLiteStorage,
) -> None:
    """A trusted facade must not stamp its own capture as an agent self-report.

    This adapter is the in-process facade; every write it makes is
    ``Origin.DETERMINISTIC``. Declaring the environment it pinned itself as
    ``EXTERNAL_AGENT`` made a fully trusted local run carry one untrusted,
    agent-self-reported fact (issue #1391).
    """
    from continuum.events import EventType
    from continuum.models import Origin
    from continuum.state.semantic import project

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Pin env", run_id="run_120")
    adapter.storage.append_event(
        "run_120", EventType.RUN_STARTED, {"goal": "Pin env"}
    )

    state = SemanticState(run_id="run_120", goal=Goal(description="Pin env"))
    env = capture("run_120", StaticProvider(dataset="v1"))
    adapter.capture_state("run_120", state, environment=env, reason="pin")

    events = list(store.read_events("run_120"))
    declared = [e for e in events if e.type is EventType.DEPENDENCY_DECLARED]
    assert [e.payload["resource"] for e in declared] == ["dataset"]

    # The rest of the adapter's writes are deterministic: the checkpoint
    # annotation and the ledger events. The declaration must match them.
    annotated = [e for e in events if e.type is EventType.STATE_CHECKPOINTED]
    assert annotated and annotated[0].source is Origin.DETERMINISTIC
    assert {e.source for e in declared} == {Origin.DETERMINISTIC}

    # And it must still be deterministic once projection carries it forward.
    dependency = project("run_120", events).external_dependencies[0]
    assert dependency.provenance.origin is Origin.DETERMINISTIC


def test_a_pinned_environment_does_not_degrade_the_advisory_trust_score(
    store: SQLiteStorage,
) -> None:
    """The mislabelled declaration cost the run a third of its trust (issue #1391).

    A run whose dependency the adapter pinned scored 0.622 against 0.967 for the
    identical run that declared the same dependency deterministically, so the
    advisory score reported a trusted in-process run as partly self-certified.
    """
    from continuum.analysis.prefix_trust import trust_over_prefix
    from continuum.events import EventType
    from continuum.models import Origin
    from continuum.state.semantic import project

    def score(declared_with: Origin) -> float:
        with SQLiteStorage(":memory:") as inner:
            inner.create_run(Run(run_id="r", goal="g"))
            inner.append_event("r", EventType.RUN_STARTED, {"goal": "g"})
            inner.append_event(
                "r", EventType.WORK_COMPLETED, {"count": 1, "task_id": "t"}
            )
            inner.append_event(
                "r",
                EventType.DEPENDENCY_DECLARED,
                {"resource": "dataset", "version": "v1"},
                source=declared_with,
            )
            return trust_over_prefix(project("r", list(inner.read_events("r"))))[
                "trust_score"
            ]

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="g", run_id="r")
    adapter.storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"})
    adapter.storage.append_event(
        "r", EventType.WORK_COMPLETED, {"count": 1, "task_id": "t"}
    )
    state = project("r", list(store.read_events("r")))
    adapter.capture_state(
        "r",
        state,
        environment=capture("r", StaticProvider(dataset="v1")),
        reason="pin",
    )

    actual = trust_over_prefix(project("r", list(store.read_events("r"))))[
        "trust_score"
    ]
    assert actual == score(Origin.DETERMINISTIC)
    assert actual > score(Origin.EXTERNAL_AGENT)

