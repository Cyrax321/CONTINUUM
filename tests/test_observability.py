"""Tests for the observability module (metrics collector + Phase 14 dashboard)."""

from __future__ import annotations

from continuum.checkpoint.manager import RestoredRun
from continuum.environment.diff import EnvironmentDiff
from continuum.models import (
    Action,
    ActionStatus,
    Component,
    ComponentValidationEntry,
    Goal,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    SemanticState,
    StateStatus,
    StateValidationResult,
)
from continuum.observability import (
    CHECKPOINTS_CREATED,
    RECOVERIES_BLOCKED,
    RECOVERIES_RESUMED,
    UNKNOWN_SIDE_EFFECTS,
    VALIDATIONS_RUN,
    Metrics,
    collect_from_decision,
    get_metrics,
    render_dashboard,
    reset_metrics,
    set_metrics,
)
from continuum.recovery.engine import RecoveryDecision
from continuum.recovery.planner import RepairKind, RepairPlan, RepairStep
from continuum.state.validator import ValidationOutcome


def _decision(can_resume: bool, uncertain: tuple[Action, ...] = ()) -> RecoveryDecision:
    """Build a minimal RecoveryDecision for dashboard/metrics tests."""
    state = SemanticState(run_id="run_x", goal=Goal(description="recover"))
    contract = RecoveryContract(
        run_id="run_x",
        checkpoint_version=3,
        recovery_status=RecoverySafety.SAFE_TO_RESUME if can_resume else RecoverySafety.BLOCKED,
        verified=["goal"],
        invalidated=["external_dependency dataset (CONFLICTED)"],
        required_actions=[],
        next_allowed_action=None,
    )
    plan = RepairPlan(
        steps=[
            RepairStep(
                kind=RepairKind.REVALIDATE_DEPENDENCY,
                target="dataset",
                reason="version drift",
            )
        ]
    )
    validation = ValidationOutcome(
        state=state,
        report=StateValidationResult(
            run_id="run_x",
            checkpoint_version=3,
            statuses=[
                ComponentValidationEntry(component=Component.GOAL, status=StateStatus.VALID),
                ComponentValidationEntry(
                    component=Component.EXTERNAL_DEPENDENCY,
                    component_id="dataset",
                    status=StateStatus.CONFLICTED,
                    detail="v3 vs v4",
                ),
            ],
            safe_to_resume=can_resume,
        ),
        environment_diff=EnvironmentDiff(),
    )
    restored = RestoredRun(
        run_id="run_x", state=state, checkpoint=None, pending_events=0, replayed=True
    )
    return RecoveryDecision(
        run_id="run_x",
        mode=RecoveryMode.RESUME if can_resume else RecoveryMode.WAIT,
        contract=contract,
        plan=plan,
        validation=validation,
        restored=restored,
        uncertain_actions=uncertain,
        rationale=("dataset version drift",),
    )


def test_metrics_counter_is_monotonic_and_rejects_negative() -> None:
    m = Metrics()
    m.increment(CHECKPOINTS_CREATED, 3)
    m.increment(CHECKPOINTS_CREATED)
    assert m.counters[CHECKPOINTS_CREATED] == 4
    try:
        m.increment(CHECKPOINTS_CREATED, -1)
    except ValueError:
        pass
    else:
        raise AssertionError("negative increment should raise")


def test_metrics_timer_accumulates() -> None:
    m = Metrics()
    with m.timer("validate"):
        pass
    assert "validate" in m.timers
    assert m.timers["validate"] >= 0.0


def test_get_metrics_returns_active_collector() -> None:
    reset_metrics()
    before = get_metrics()
    token = set_metrics(Metrics())
    try:
        assert get_metrics() is not before
    finally:
        token.var.reset(token)


def test_collect_from_decision_counts_resumed() -> None:
    reset_metrics()
    collect_from_decision(_decision(can_resume=True))
    snap = get_metrics().snapshot()
    assert snap["counters"][VALIDATIONS_RUN] == 1
    assert snap["counters"][RECOVERIES_RESUMED] == 1
    assert snap["counters"].get(RECOVERIES_BLOCKED, 0) == 0
    assert snap["gauges"]["validation.invalid"] == 1


def test_collect_from_decision_counts_blocked_and_unknown() -> None:
    reset_metrics()
    action = Action(run_id="run_x", action_type="github.create_issue", status=ActionStatus.UNKNOWN)
    collect_from_decision(_decision(can_resume=False, uncertain=(action,)))
    snap = get_metrics().snapshot()
    assert snap["counters"][RECOVERIES_BLOCKED] == 1
    assert snap["counters"][UNKNOWN_SIDE_EFFECTS] == 1


def test_product_paths_increment_the_counters() -> None:
    """The counters are driven by real product calls, not just this module.

    Pins the wiring added in #1032: assess(), checkpoint() and the ledger's
    claim()/complete() are the call sites that make the collector live. If any
    of them drops its collection call, this fails rather than the metric
    silently going stale.
    """
    from continuum.actions import ActionLedger
    from continuum.checkpoint import CheckpointManager
    from continuum.events import EventType
    from continuum.models import Run
    from continuum.observability import ACTIONS_CLAIMED, ACTIONS_COMPLETED
    from continuum.recovery import RecoveryEngine
    from continuum.storage import SQLiteStorage

    reset_metrics()
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="prod_run", goal="wire the counters"))
    storage.append_event("prod_run", EventType.RUN_STARTED, {"goal": "wire the counters"})

    CheckpointManager(storage).checkpoint("prod_run")
    ledger = ActionLedger(storage, run_id="prod_run")
    outcome = ledger.claim("send_email", {"to": "nobody@example.com"})
    ledger.complete(outcome.key, result={"message_id": "1"})
    RecoveryEngine(storage).assess("prod_run")

    counters = get_metrics().snapshot()["counters"]
    assert counters[CHECKPOINTS_CREATED] == 1, "checkpoint() did not count"
    assert counters[ACTIONS_CLAIMED] == 1, "claim() did not count"
    assert counters[ACTIONS_COMPLETED] == 1, "complete() did not count"
    assert counters[VALIDATIONS_RUN] == 1, "assess() did not count"


def test_idempotent_completion_re_report_does_not_re_count() -> None:
    """Re-reporting an already-COMPLETED action must not re-count it.

    ``complete`` documents that re-reporting a COMPLETED action after a dropped
    response is allowed and "is not asserting anything new", and its settlement
    drawdown is already gated on the pre-call status being STARTED. The
    completion counter added by #1032 was left ungated, so each idempotent
    re-report bumped ``actions.completed`` again -- inflating a monotonic
    recovery signal for a settlement that already happened once. The sibling
    ``claim`` counter is the correct model: it does not count when a claim
    defers to a COMPLETED record. This pins both counters to the settlement,
    not the call.
    """
    from continuum.actions import ActionLedger
    from continuum.events import EventType
    from continuum.models import Run
    from continuum.observability import ACTIONS_CLAIMED, ACTIONS_COMPLETED
    from continuum.storage import SQLiteStorage

    reset_metrics()
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="dup_run", goal="idempotent completion"))
    storage.append_event("dup_run", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, run_id="dup_run")

    outcome = ledger.claim("send_email", {"to": "nobody@example.com"})
    ledger.complete(outcome.key, external_id="mid-1")
    # Two idempotent re-reports of the same completion (dropped-response retry).
    ledger.complete(outcome.key, external_id="mid-1")
    ledger.complete(outcome.key, external_id="mid-1")

    counters = get_metrics().snapshot()["counters"]
    assert counters[ACTIONS_CLAIMED] == 1, "one claim, counted once"
    assert counters[ACTIONS_COMPLETED] == 1, "one settlement, re-reports assert nothing new"


def test_metrics_failures_never_break_the_safety_critical_path() -> None:
    """A broken collector must not change what claim/complete return.

    The collection calls are best-effort by design: a metrics failure inside
    claim() would be a safety regression if it changed whether an action can
    be claimed. This pins that it does not.
    """
    from continuum.actions import ActionLedger
    from continuum.events import EventType
    from continuum.models import Run
    from continuum.observability import set_metrics
    from continuum.storage import SQLiteStorage

    class _BrokenMetrics(Metrics):
        def increment(self, name: str, by: int = 1) -> None:
            raise RuntimeError("collector is broken")

    token = set_metrics(_BrokenMetrics())
    try:
        storage = SQLiteStorage(":memory:")
        storage.create_run(Run(run_id="broken_run", goal="survive a bad collector"))
        storage.append_event("broken_run", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, run_id="broken_run")
        outcome = ledger.claim("send_email", {"to": "nobody@example.com"})
        # The claim still succeeds and the action is still claimable.
        assert outcome.fresh is True
        completed = ledger.complete(outcome.key, result={"ok": True})
        assert completed.status is ActionStatus.COMPLETED
    finally:
        set_metrics(token)


def test_render_dashboard_contains_state_components_and_plan() -> None:
    out = render_dashboard(_decision(can_resume=False))
    assert "CONTINUUM RECOVERY DASHBOARD" in out
    assert "run_x" in out
    assert "external dependency dataset: conflicted" in out
    assert "REPAIRS REQUIRED" in out
    assert "revalidate_dependency dataset" in out
    assert "RATIONALE" in out


def test_render_dashboard_marks_resume() -> None:
    out = render_dashboard(_decision(can_resume=True))
    assert "safe to resume:     yes" in out
