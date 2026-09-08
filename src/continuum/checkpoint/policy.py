"""When to checkpoint.

Checkpointing every turn is the obvious design and the wrong one: it costs an
fsync per step and fills history with versions that mean nothing. Checkpointing
too rarely loses work. A policy decides.

Policies answer one question (``should_checkpoint(...) -> Decision``) and are
pure: same inputs, same answer, no clock reads hidden inside except the one
passed in. That makes checkpoint timing testable instead of a source of
flakiness.

Available policies
------------------

``ManualPolicy``      only when asked explicitly
``IntervalPolicy``    at most once per N seconds
``EventPolicy``       when specific event types occur
``SemanticPolicy``    when the state changed in ways that matter
``HybridPolicy``      any of the above (the practical default)

The important one is ``SemanticPolicy``: it fires on *meaning*, not on volume.
Processing a thousand documents changes progress but nothing structural;
invalidating one decision changes what the agent may safely do next.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from continuum.events import Event, EventType
from continuum.models import SemanticState, StateStatus, utcnow
from continuum.state.versioning import state_fingerprint

__all__ = [
    "CheckpointTrigger",
    "CheckpointDecision",
    "CheckpointPolicy",
    "ManualPolicy",
    "IntervalPolicy",
    "EventPolicy",
    "SemanticPolicy",
    "HybridPolicy",
    "default_policy",
    "SIDE_EFFECT_EVENTS",
    "MILESTONE_EVENTS",
]


class CheckpointTrigger:
    """Why a checkpoint was taken. Recorded on the checkpoint for auditing."""

    MANUAL = "manual"
    INTERVAL = "interval"
    SIDE_EFFECT = "external_side_effect"
    MILESTONE = "milestone"
    IMPORTANT_STATE_CHANGE = "important_state_change"
    CONTEXT_PRESSURE = "context_pressure"
    RUN_COMPLETED = "run_completed"
    RECOVERY = "recovery"


#: Events that mean the outside world changed. Losing these is expensive:
#: without a checkpoint, recovery cannot tell whether the effect happened.
SIDE_EFFECT_EVENTS = frozenset(
    {
        EventType.ACTION_RECORDED,
        EventType.ACTION_RECONCILED,
        EventType.ACTION_COMPENSATED,
        EventType.TOOL_COMPLETED,
    }
)

#: Events that mark a structural point in the task.
MILESTONE_EVENTS = frozenset(
    {
        EventType.RUN_COMPLETED,
        EventType.ENVIRONMENT_CHANGED,
        EventType.MODEL_CHANGED,
        EventType.APPROVAL_GRANTED,
        EventType.APPROVAL_REVOKED,
        EventType.RECOVERY_COMPLETED,
    }
)


@dataclass(frozen=True, slots=True)
class CheckpointDecision:
    """Whether to checkpoint, and the reason to record if so."""

    should: bool
    trigger: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.should

    @classmethod
    def no(cls) -> CheckpointDecision:
        return cls(should=False)

    @classmethod
    def yes(cls, trigger: str, reason: str = "") -> CheckpointDecision:
        return cls(should=True, trigger=trigger, reason=reason)


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything a policy is allowed to consider."""

    state: SemanticState
    previous_state: SemanticState | None = None
    new_events: Sequence[Event] = ()
    last_checkpoint_at: datetime | None = None
    now: datetime = field(default_factory=utcnow)
    explicit: bool = False
    context_tokens: int | None = None


class CheckpointPolicy(ABC):
    """Decides when durable state should be written."""

    name: str = "policy"

    @abstractmethod
    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision: ...


class ManualPolicy(CheckpointPolicy):
    """Checkpoint only on an explicit request."""

    name = "manual"

    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision:
        if context.explicit:
            return CheckpointDecision.yes(CheckpointTrigger.MANUAL, "explicitly requested")
        return CheckpointDecision.no()


class IntervalPolicy(CheckpointPolicy):
    """Checkpoint when enough time has passed since the last one."""

    name = "interval"

    def __init__(self, max_interval_seconds: float = 300.0) -> None:
        if max_interval_seconds <= 0:
            raise ValueError("max_interval_seconds must be positive")
        self.max_interval = timedelta(seconds=max_interval_seconds)

    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision:
        if context.last_checkpoint_at is None:
            return CheckpointDecision.yes(
                CheckpointTrigger.INTERVAL, "no checkpoint exists for this run"
            )
        elapsed = context.now - context.last_checkpoint_at
        if elapsed >= self.max_interval:
            return CheckpointDecision.yes(
                CheckpointTrigger.INTERVAL,
                f"{elapsed.total_seconds():.0f}s since last checkpoint",
            )
        return CheckpointDecision.no()


class EventPolicy(CheckpointPolicy):
    """Checkpoint when particular event types appear.

    Defaults to side effects and milestones, the events whose loss actually
    costs something.
    """

    name = "event"

    def __init__(
        self,
        side_effects: bool = True,
        milestones: bool = True,
        extra: Iterable[EventType] = (),
    ) -> None:
        watched: set[EventType] = set(extra)
        if side_effects:
            watched |= SIDE_EFFECT_EVENTS
        if milestones:
            watched |= MILESTONE_EVENTS
        self.watched = frozenset(watched)

    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision:
        for event in context.new_events:
            if event.type not in self.watched:
                continue
            trigger = (
                CheckpointTrigger.SIDE_EFFECT
                if event.type in SIDE_EFFECT_EVENTS
                else CheckpointTrigger.MILESTONE
            )
            return CheckpointDecision.yes(trigger, f"{event.type} at sequence {event.sequence}")
        return CheckpointDecision.no()


class SemanticPolicy(CheckpointPolicy):
    """Checkpoint when the *meaning* of the state changed.

    Progress alone does not qualify unless it crosses a stride: counting from
    3,400 to 3,401 is not worth an fsync, but losing 500 documents of work is.
    Structural changes (a new or invalidated decision, a new finding, a changed
    dependency, an approval, a model switch) always qualify, because they
    change what the agent is allowed to do next.
    """

    name = "semantic"

    def __init__(self, progress_stride: int = 100) -> None:
        if progress_stride < 1:
            raise ValueError("progress_stride must be >= 1")
        self.progress_stride = progress_stride

    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision:
        current = context.state
        previous = context.previous_state

        if previous is None:
            return CheckpointDecision.yes(
                CheckpointTrigger.IMPORTANT_STATE_CHANGE, "first state for this run"
            )

        if state_fingerprint(previous) == state_fingerprint(current):
            return CheckpointDecision.no()

        for label, change in self._structural_changes(previous, current):
            if change:
                return CheckpointDecision.yes(CheckpointTrigger.IMPORTANT_STATE_CHANGE, label)

        done_before = previous.progress.completed + previous.progress.failed
        done_now = current.progress.completed + current.progress.failed
        if done_now // self.progress_stride > done_before // self.progress_stride:
            return CheckpointDecision.yes(
                CheckpointTrigger.MILESTONE,
                f"progress crossed a multiple of {self.progress_stride} ({done_now})",
            )

        return CheckpointDecision.no()

    @staticmethod
    def _structural_changes(
        previous: SemanticState, current: SemanticState
    ) -> Sequence[tuple[str, bool]]:
        def invalidated(state: SemanticState) -> int:
            terminal = {StateStatus.INVALID, StateStatus.STALE, StateStatus.CONFLICTED}
            return sum(1 for d in state.decisions if d.status in terminal) + sum(
                1 for f in state.findings if f.status in terminal
            )

        def dependency_signature(state: SemanticState) -> tuple[tuple[str, str | None], ...]:
            return tuple(sorted((d.resource, d.version) for d in state.external_dependencies))

        def approval_signature(state: SemanticState) -> tuple[tuple[str, str], ...]:
            return tuple(sorted((a.approval_id, a.status.value) for a in state.approvals))

        return (
            ("goal changed", previous.goal != current.goal),
            ("a decision was recorded", len(current.decisions) != len(previous.decisions)),
            ("a finding was recorded", len(current.findings) != len(previous.findings)),
            ("state was invalidated", invalidated(current) != invalidated(previous)),
            (
                "an external dependency changed",
                dependency_signature(previous) != dependency_signature(current),
            ),
            ("an approval changed", approval_signature(previous) != approval_signature(current)),
            (
                "the model changed",
                (previous.model.model if previous.model else None)
                != (current.model.model if current.model else None),
            ),
        )


class HybridPolicy(CheckpointPolicy):
    """Checkpoint if any constituent policy says so.

    Order matters only for the reason reported: the first policy to say yes
    supplies the trigger.
    """

    name = "hybrid"

    def __init__(self, policies: Iterable[CheckpointPolicy]) -> None:
        self.policies = tuple(policies)
        if not self.policies:
            raise ValueError("HybridPolicy requires at least one policy")

    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision:
        for policy in self.policies:
            decision = policy.should_checkpoint(context)
            if decision.should:
                return decision
        return CheckpointDecision.no()


class ContextPressurePolicy(CheckpointPolicy):
    """Checkpoint when the LLM context is filling up.

    This is the case CONTINUUM exists for: the transcript is about to be
    compacted or truncated, so durable state must be written before the
    in-memory history disappears.
    """

    name = "context_pressure"

    def __init__(self, token_budget: int, threshold: float = 0.8) -> None:
        if token_budget <= 0:
            raise ValueError("token_budget must be positive")
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be within (0, 1]")
        self.token_budget = token_budget
        self.threshold = threshold

    def should_checkpoint(self, context: PolicyContext) -> CheckpointDecision:
        if context.context_tokens is None:
            return CheckpointDecision.no()
        used = context.context_tokens / self.token_budget
        if used >= self.threshold:
            return CheckpointDecision.yes(
                CheckpointTrigger.CONTEXT_PRESSURE,
                f"context {used:.0%} of budget ({context.context_tokens}/{self.token_budget})",
            )
        return CheckpointDecision.no()


def default_policy(max_interval_seconds: float = 300.0) -> HybridPolicy:
    """The recommended default: explicit requests, side effects, meaning, then time.

    Time is last so that a checkpoint taken for a real reason reports that
    reason rather than "the timer went off".
    """
    return HybridPolicy(
        [
            ManualPolicy(),
            EventPolicy(),
            SemanticPolicy(),
            IntervalPolicy(max_interval_seconds),
        ]
    )
