"""Turning findings into repair steps.

Validation says *what* is wrong. The planner says *what to do about it*, as an
ordered list of concrete steps.

Ordering is not cosmetic. Reconciling an uncertain side effect must come before
any new work, because until the ledger knows whether that GitHub issue exists,
the agent cannot safely act on the assumption that it does or does not.
Similarly, a stale dependency must be re-pinned before the findings derived from
it are re-derived — repairing in the wrong order produces work that is stale the
moment it completes.

Steps are declarative. The planner does not execute anything; it produces the
plan the recovery contract will gate on, and the agent (or a human) carries it
out. Keeping planning free of side effects means a plan can be inspected,
diffed, logged and tested without touching the world.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from continuum.models import (
    Action,
    ActionStatus,
    Component,
    ComponentValidationEntry,
    StateStatus,
)

__all__ = ["RepairKind", "RepairStep", "RepairPlan", "plan_repairs"]


class RepairKind(StrEnum):
    """What kind of work a step represents."""

    RECONCILE_ACTION = "reconcile_action"
    """Determine whether an external side effect actually happened."""

    REVALIDATE_DEPENDENCY = "revalidate_dependency"
    """Re-pin a dependency whose version moved."""

    REDERIVE_EVIDENCE = "rederive_evidence"
    """Re-fetch evidence whose source changed."""

    REDERIVE_FINDING = "rederive_finding"
    """Recompute a finding that rested on changed support."""

    REVIEW_DECISION = "review_decision"
    """Re-examine a decision whose justification no longer holds."""

    RENEW_APPROVAL = "renew_approval"
    """Obtain a fresh human approval."""

    REVALIDATE_MODEL_STATE = "revalidate_model_state"
    """Confirm assumptions tied to a model that is no longer active."""

    HUMAN_REVIEW = "human_review"
    """Something no automated step can settle."""


#: Lower sorts earlier. Uncertain side effects first: nothing else is safe
#: while the world may or may not have been modified.
_ORDER: dict[RepairKind, int] = {
    RepairKind.RECONCILE_ACTION: 0,
    RepairKind.HUMAN_REVIEW: 1,
    RepairKind.RENEW_APPROVAL: 2,
    RepairKind.REVALIDATE_DEPENDENCY: 3,
    RepairKind.REVALIDATE_MODEL_STATE: 4,
    RepairKind.REDERIVE_EVIDENCE: 5,
    RepairKind.REDERIVE_FINDING: 6,
    RepairKind.REVIEW_DECISION: 7,
}


class RepairStep(BaseModel):
    """One unit of work needed before the run can safely continue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RepairKind
    target: str
    reason: str = ""
    blocking: bool = True
    """Whether the run may proceed while this is outstanding."""

    requires_human: bool = False

    @property
    def action_name(self) -> str:
        """The permitted-action identifier a contract gates on."""
        return f"{self.kind.value}:{self.target}"

    def render(self) -> str:
        mark = "[human]" if self.requires_human else "[auto] "
        detail = f" — {self.reason}" if self.reason else ""
        return f"{mark} {self.kind.value} {self.target}{detail}"


class RepairPlan(BaseModel):
    """An ordered, inspectable set of repair steps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[RepairStep] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.steps)

    def __bool__(self) -> bool:
        return bool(self.steps)

    @property
    def blocking(self) -> tuple[RepairStep, ...]:
        return tuple(s for s in self.steps if s.blocking)

    @property
    def requires_human(self) -> bool:
        return any(s.requires_human for s in self.steps)

    @property
    def first(self) -> RepairStep | None:
        """The only step permitted to run next."""
        return self.steps[0] if self.steps else None

    def of_kind(self, kind: RepairKind) -> tuple[RepairStep, ...]:
        return tuple(s for s in self.steps if s.kind is kind)

    def render(self) -> str:
        if not self.steps:
            return "No repairs required."
        return "\n".join(f"  {i}. {s.render()}" for i, s in enumerate(self.steps, 1))


def _step_for(entry: ComponentValidationEntry, *, strict_unknown: bool = True) -> RepairStep | None:
    """Map one validation finding to the repair it implies."""
    if entry.status is StateStatus.VALID:
        return None

    target = entry.component_id or entry.component.value

    match entry.component:
        case Component.EXTERNAL_DEPENDENCY:
            return RepairStep(
                kind=RepairKind.REVALIDATE_DEPENDENCY,
                target=target,
                reason=entry.detail,
                # An unverifiable resource normally needs a person, because
                # nobody knows what is true. Callers who opted into tolerating
                # uncertainty get an automatic step instead — the policy has to
                # hold here too, or the setting would be silently ignored.
                requires_human=entry.status is StateStatus.UNKNOWN and strict_unknown,
            )
        case Component.EVIDENCE:
            return RepairStep(kind=RepairKind.REDERIVE_EVIDENCE, target=target, reason=entry.detail)
        case Component.FINDING:
            return RepairStep(kind=RepairKind.REDERIVE_FINDING, target=target, reason=entry.detail)
        case Component.DECISION:
            return RepairStep(kind=RepairKind.REVIEW_DECISION, target=target, reason=entry.detail)
        case Component.APPROVAL:
            return RepairStep(
                kind=RepairKind.RENEW_APPROVAL,
                target=target,
                reason=entry.detail,
                requires_human=True,
            )
        case Component.MODEL:
            return RepairStep(
                kind=RepairKind.REVALIDATE_MODEL_STATE, target=target, reason=entry.detail
            )
        case Component.PROGRESS | Component.GOAL:
            # Progress and goal cannot be "repaired" by re-running a step; if
            # they are in doubt the situation needs a person, not a retry.
            return RepairStep(
                kind=RepairKind.HUMAN_REVIEW,
                target=target,
                reason=entry.detail or f"{entry.component.value} is {entry.status}",
                requires_human=True,
            )
        case _:
            return RepairStep(
                kind=RepairKind.HUMAN_REVIEW,
                target=target,
                reason=entry.detail,
                requires_human=True,
            )


def plan_repairs(
    findings: Sequence[ComponentValidationEntry] = (),
    *,
    uncertain_actions: Sequence[Action] = (),
    strict_unknown: bool = True,
) -> RepairPlan:
    """Build an ordered repair plan from validation findings and ledger state.

    Steps are deduplicated by identity and sorted so that prerequisites precede
    the work that depends on them. Sorting is stable and total, so the same
    inputs always yield the same plan — a contract that varied between runs
    would be impossible to audit.
    """
    steps: list[RepairStep] = []

    for action in uncertain_actions:
        if action.status not in (
            ActionStatus.UNKNOWN,
            ActionStatus.STARTED,
            ActionStatus.REQUIRES_REVIEW,
        ):
            continue
        # An action already escalated to REQUIRES_REVIEW has defeated automatic
        # reconciliation once; sending it back through the same machinery would
        # loop. It needs a person. So does an action whose outcome is still
        # unknown while strict mode is on: the engine escalates such runs to
        # REQUEST_HUMAN, so the step must agree rather than quietly offering an
        # automatic reconcile the contract would then permit (issue #42).
        escalated = action.status is ActionStatus.REQUIRES_REVIEW
        needs_person = escalated or strict_unknown
        steps.append(
            RepairStep(
                kind=RepairKind.RECONCILE_ACTION,
                target=action.action_id,
                # Keyed on what actually happened, not on who must handle it: an
                # interrupted action was never "escalated for review".
                reason=(
                    f"{action.action_type} was escalated for review"
                    if escalated
                    else f"{action.action_type} was interrupted; the side effect may or "
                    f"may not have occurred"
                ),
                requires_human=needs_person,
            )
        )

    for entry in findings:
        step = _step_for(entry, strict_unknown=strict_unknown)
        if step is not None:
            steps.append(step)

    seen: set[tuple[RepairKind, str]] = set()
    unique: list[RepairStep] = []
    for step in steps:
        identity = (step.kind, step.target)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(step)

    unique.sort(key=lambda s: (_ORDER[s.kind], s.target))
    return RepairPlan(steps=unique)
