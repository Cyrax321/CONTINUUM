from __future__ import annotations

from datetime import timedelta

import pytest

from continuum.checkpoint.policy import (
    CheckpointTrigger,
    ContextPressurePolicy,
    EventPolicy,
    HybridPolicy,
    IntervalPolicy,
    ManualPolicy,
    PolicyContext,
    SemanticPolicy,
    default_policy,
)
from continuum.events import EventLog, EventType
from continuum.models import (
    Approval,
    ApprovalStatus,
    Decision,
    ExternalDependency,
    Finding,
    Goal,
    ModelState,
    Progress,
    SemanticState,
    StateStatus,
    utcnow,
)


def state(**overrides: object) -> SemanticState:
    base: dict[str, object] = {"run_id": "run_1", "goal": Goal(description="g")}
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


def context(**overrides: object) -> PolicyContext:
    base: dict[str, object] = {"state": state()}
    base.update(overrides)
    return PolicyContext(**base)  # type: ignore[arg-type]


# --- manual ---------------------------------------------------------------- #


def test_manual_policy_only_fires_when_asked() -> None:
    policy = ManualPolicy()
    assert not policy.should_checkpoint(context())
    decision = policy.should_checkpoint(context(explicit=True))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.MANUAL


def test_a_decision_is_truthy_so_it_reads_naturally() -> None:
    assert bool(ManualPolicy().should_checkpoint(context(explicit=True))) is True
    assert bool(ManualPolicy().should_checkpoint(context())) is False


# --- interval -------------------------------------------------------------- #


def test_interval_policy_fires_when_no_checkpoint_exists() -> None:
    assert IntervalPolicy(300).should_checkpoint(context()).should


def test_interval_policy_waits_for_the_interval() -> None:
    now = utcnow()
    policy = IntervalPolicy(300)
    assert not policy.should_checkpoint(
        context(last_checkpoint_at=now - timedelta(seconds=100), now=now)
    )
    decision = policy.should_checkpoint(
        context(last_checkpoint_at=now - timedelta(seconds=301), now=now)
    )
    assert decision.should
    assert decision.trigger == CheckpointTrigger.INTERVAL


def test_a_nonpositive_interval_is_refused() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        IntervalPolicy(0)


# --- events ---------------------------------------------------------------- #


def test_event_policy_fires_on_a_side_effect() -> None:
    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
    action = log.append("run_1", EventType.ACTION_RECORDED, {"action_id": "a1"})

    decision = EventPolicy().should_checkpoint(context(new_events=[action]))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.SIDE_EFFECT


def test_event_policy_fires_on_a_milestone() -> None:
    log = EventLog()
    event = log.append("run_1", EventType.ENVIRONMENT_CHANGED, {})
    decision = EventPolicy().should_checkpoint(context(new_events=[event]))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.MILESTONE


def test_event_policy_ignores_routine_events() -> None:
    log = EventLog()
    events = [
        log.append("run_1", EventType.TOOL_CALLED, {}),
        log.append("run_1", EventType.WORK_COMPLETED, {}),
    ]
    assert not EventPolicy().should_checkpoint(context(new_events=events))


def test_event_policy_can_watch_extra_types() -> None:
    log = EventLog()
    event = log.append("run_1", EventType.WORK_COMPLETED, {})
    policy = EventPolicy(side_effects=False, milestones=False, extra=[EventType.WORK_COMPLETED])
    assert policy.should_checkpoint(context(new_events=[event])).should


# --- semantic: the interesting one ----------------------------------------- #


def test_semantic_policy_fires_on_the_first_state() -> None:
    assert SemanticPolicy().should_checkpoint(context()).should


def test_semantic_policy_ignores_an_unchanged_state() -> None:
    current = state()
    assert not SemanticPolicy().should_checkpoint(context(state=current, previous_state=current))


def test_grinding_through_documents_does_not_checkpoint_every_step() -> None:
    """The whole point: volume is not meaning."""
    policy = SemanticPolicy(progress_stride=100)
    previous = state(progress=Progress(total=10_000, completed=3400))
    current = state(progress=Progress(total=10_000, completed=3401))
    assert not policy.should_checkpoint(context(state=current, previous_state=previous))


def test_crossing_a_progress_stride_does_checkpoint() -> None:
    policy = SemanticPolicy(progress_stride=100)
    previous = state(progress=Progress(total=10_000, completed=3499))
    current = state(progress=Progress(total=10_000, completed=3500))
    decision = policy.should_checkpoint(context(state=current, previous_state=previous))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.MILESTONE


def test_failures_count_toward_the_stride() -> None:
    policy = SemanticPolicy(progress_stride=10)
    previous = state(progress=Progress(completed=9, failed=0))
    current = state(progress=Progress(completed=9, failed=1))
    assert policy.should_checkpoint(context(state=current, previous_state=previous)).should


@pytest.mark.parametrize(
    ("label", "current"),
    [
        ("goal", state(goal=Goal(description="different"))),
        ("decision added", state(decisions=[Decision(decision_id="d1", decision="x")])),
        ("finding added", state(findings=[Finding(finding_id="f1", claim="c")])),
        (
            "dependency changed",
            state(external_dependencies=[ExternalDependency(resource="dataset", version="v4")]),
        ),
        (
            "approval changed",
            state(
                approvals=[Approval(approval_id="a1", subject="s", status=ApprovalStatus.GRANTED)]
            ),
        ),
        ("model changed", state(model=ModelState(model="model-b"))),
    ],
)
def test_structural_changes_always_checkpoint(label: str, current: SemanticState) -> None:
    decision = SemanticPolicy().should_checkpoint(context(state=current, previous_state=state()))
    assert decision.should, f"{label} should have triggered a checkpoint"


def test_invalidating_a_decision_checkpoints() -> None:
    """Invalidation changes what the agent may do, so it must be durable."""
    valid = Decision(decision_id="d1", decision="x")
    previous = state(decisions=[valid])
    current = state(decisions=[valid.model_copy(update={"status": StateStatus.STALE})])

    decision = SemanticPolicy().should_checkpoint(context(state=current, previous_state=previous))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.IMPORTANT_STATE_CHANGE


def test_approving_a_decision_checkpoints_though_the_count_is_unchanged() -> None:
    """Issue #1353: a status transition that leaves the count and the terminal
    tally unchanged is still a change in what the agent may do next.

    REQUIRES_REVIEW -> VALID (the decision is now approved) is invisible to a
    count-based check -- nothing added or removed, neither status terminal -- yet
    state_fingerprint already treats it as a change of meaning.
    """
    reviewing = Decision(decision_id="d1", decision="cut over", status=StateStatus.REQUIRES_REVIEW)
    previous = state(decisions=[reviewing])
    current = state(decisions=[reviewing.model_copy(update={"status": StateStatus.VALID})])

    decision = SemanticPolicy().should_checkpoint(context(state=current, previous_state=previous))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.IMPORTANT_STATE_CHANGE


def test_a_finding_moving_between_two_terminal_statuses_checkpoints() -> None:
    """The lumped terminal tally was unchanged by STALE -> CONFLICTED, so the
    old detector missed it; the identity+status signature catches it (#1353)."""
    stale = Finding(finding_id="f1", claim="c", status=StateStatus.STALE)
    previous = state(findings=[stale])
    current = state(findings=[stale.model_copy(update={"status": StateStatus.CONFLICTED})])

    decision = SemanticPolicy().should_checkpoint(context(state=current, previous_state=previous))
    assert decision.should


def test_an_invalid_stride_is_refused() -> None:
    with pytest.raises(ValueError, match="progress_stride"):
        SemanticPolicy(progress_stride=0)


# --- context pressure ------------------------------------------------------ #


def test_context_pressure_fires_near_the_budget() -> None:
    policy = ContextPressurePolicy(token_budget=1000, threshold=0.8)
    assert not policy.should_checkpoint(context(context_tokens=700))
    decision = policy.should_checkpoint(context(context_tokens=850))
    assert decision.should
    assert decision.trigger == CheckpointTrigger.CONTEXT_PRESSURE


def test_context_pressure_is_inert_without_a_measurement() -> None:
    assert not ContextPressurePolicy(1000).should_checkpoint(context(context_tokens=None))


def test_context_pressure_validates_its_configuration() -> None:
    with pytest.raises(ValueError, match="token_budget"):
        ContextPressurePolicy(0)
    with pytest.raises(ValueError, match="threshold"):
        ContextPressurePolicy(100, threshold=1.5)


# --- hybrid ---------------------------------------------------------------- #


def test_hybrid_fires_if_any_policy_agrees() -> None:
    policy = HybridPolicy([ManualPolicy(), IntervalPolicy(300)])
    assert policy.should_checkpoint(context(explicit=True)).should


def test_hybrid_reports_the_first_matching_reason() -> None:
    """Time is checked last so real reasons win the attribution."""
    now = utcnow()
    policy = default_policy(max_interval_seconds=1)
    decision = policy.should_checkpoint(
        context(explicit=True, last_checkpoint_at=now - timedelta(hours=1), now=now)
    )
    assert decision.trigger == CheckpointTrigger.MANUAL


def test_hybrid_declines_when_every_policy_declines() -> None:
    now = utcnow()
    current = state()
    policy = default_policy(max_interval_seconds=3600)
    assert not policy.should_checkpoint(
        context(
            state=current,
            previous_state=current,
            last_checkpoint_at=now,
            now=now,
        )
    )


def test_hybrid_requires_at_least_one_policy() -> None:
    with pytest.raises(ValueError, match="at least one policy"):
        HybridPolicy([])
