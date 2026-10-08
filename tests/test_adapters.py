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


def test_generic_adapter_declares_dependencies_as_deterministic(
    store: SQLiteStorage,
) -> None:
    """GenericAgentAdapter writes trusted DETERMINISTIC state, including dependencies (issue #1391)."""
    from continuum.events import EventType
    from continuum.models import Origin
    from continuum.state.semantic import project

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Trusted task", run_id="run_dep_trust")
    store.append_event("run_dep_trust", EventType.RUN_STARTED, {"goal": "Trusted task"})
    state = SemanticState(run_id="run_dep_trust", goal=Goal(description="Trusted task"))
    env = capture("run_dep_trust", StaticProvider(db="v1"))
    adapter.capture_state("run_dep_trust", state, environment=env)

    events = list(store.read_events("run_dep_trust"))
    dep_events = [e for e in events if e.type == EventType.DEPENDENCY_DECLARED]
    assert len(dep_events) == 1
    assert dep_events[0].source == Origin.DETERMINISTIC

    proj = project("run_dep_trust", events)
    assert len(proj.external_dependencies) == 1
    assert proj.external_dependencies[0].provenance.origin == Origin.DETERMINISTIC


def test_a_non_canonical_result_does_not_wedge_the_action(store: SQLiteStorage) -> None:
    """Issue #1394: a result the ledger cannot hash must not strand the effect.

    The side effect has already run when completion is attempted. Completing it
    used to raise, leaving the action STARTED and its caller holding a raw
    TypeError; a retry on the same key was then refused as an unknown side
    effect, so a once-only effect could neither be recorded nor redone.
    """
    from decimal import Decimal

    from continuum.actions import ActionLedger

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Charge card", run_id="run_107")

    calls = 0

    def charge() -> dict[str, Decimal]:
        nonlocal calls
        calls += 1
        return {"amount": Decimal("19.99")}

    # Before the fix this raised TypeError out of the ledger, after the effect.
    returned = adapter.intercept_action(
        "run_107", "stripe.charge", charge, arguments={"c": 1}, key="charge:1"
    )
    assert calls == 1
    # The caller gets its own return value back, untouched.
    assert returned == {"amount": Decimal("19.99")}

    ledger = ActionLedger(store, "run_107")
    assert ledger.all()[0].status.value == "completed"
    assert ledger.pending() == []

    # A retry on the same key reads the recorded result back and does not re-fire.
    again = adapter.intercept_action(
        "run_107", "stripe.charge", charge, arguments={"c": 1}, key="charge:1"
    )
    assert calls == 1
    assert again is not None
    assert ledger.pending() == []


def test_a_non_canonical_result_does_not_break_recovery(store: SQLiteStorage) -> None:
    from decimal import Decimal

    from continuum.events import EventType

    adapter = GenericAgentAdapter(store)
    adapter.start_run(goal="Charge card", run_id="run_108")
    store.append_event("run_108", EventType.RUN_STARTED, {"goal": "Charge card", "total": 1})

    adapter.intercept_action(
        "run_108",
        "stripe.charge",
        lambda: {"amount": Decimal("19.99")},
        arguments={"c": 1},
        key="charge:1",
    )

    # Recovery reads the recorded result; a sanitized one must not crash it.
    decision = adapter.resume("run_108")
    assert decision.mode is not None


def test_start_run_records_the_run_started_a_resume_folds(store: SQLiteStorage) -> None:
    """A run created through the adapter must be resumable on its own.

    ``start_run`` used to call only ``create_run``, so the run row existed
    while the event log stayed empty. Projection folds the log, not the row,
    so it raised ``ProjectionError`` and the run was indistinguishable from one
    that crashed before writing anything.
    """
    from continuum.state.semantic import project

    adapter = GenericAgentAdapter(store)
    run = adapter.start_run(goal="Ship the thing", run_id="run_120")

    events = [e.type.value for e in store.read_events(run.run_id)]
    assert events == ["RUN_STARTED"]

    folded = project(run.run_id, store.read_all_events(run.run_id))
    assert folded.goal.description == "Ship the thing"


def test_start_run_is_idempotent_on_an_existing_run(store: SQLiteStorage) -> None:
    """Calling ``start_run`` twice on the same id does not append twice.

    The run row is fetched when it already exists, and the event is backfilled
    only for an empty log, so a caller re-entering ``start_run`` after a crash
    lands on the same run with one ``RUN_STARTED``.
    """
    from continuum.events import EventType

    adapter = GenericAgentAdapter(store)
    first = adapter.start_run(goal="Re-entrant", run_id="run_121")
    second = adapter.start_run(goal="Re-entrant", run_id="run_121")

    assert second.run_id == first.run_id
    events = [e.type for e in store.read_events("run_121")]
    assert events.count(EventType.RUN_STARTED) == 1


def test_start_run_refuses_a_log_that_begins_without_run_started(store: SQLiteStorage) -> None:
    """A log whose first event is not ``RUN_STARTED`` is rejected, not misordered.

    Backfilling the start after events that supposedly preceded it would make
    every state projected from that log quietly wrong, so the adapter raises
    rather than guessing.
    """
    from continuum.events import EventType
    from continuum.models import Run

    adapter = GenericAgentAdapter(store)
    # A run row exists but its log begins with the wrong event, which is the
    # shape a misordered hand-written integration leaves behind.
    store.create_run(Run(run_id="run_122", goal="Original"))
    store.append_event("run_122", EventType.TASK_UPDATED, {"note": "out of order"})

    with pytest.raises(ValueError, match="does not begin with RUN_STARTED"):
        adapter.start_run(goal="Original", run_id="run_122")
