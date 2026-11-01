"""Turning findings into repair steps.

Validation says *what* is wrong. The planner says *what to do about it*, as an
ordered list of concrete steps.

Ordering is not cosmetic. Reconciling an uncertain side effect must come before
any new work, because until the ledger knows whether that GitHub issue exists,
the agent cannot safely act on the assumption that it does or does not.
Similarly, a stale dependency must be re-pinned before the findings derived from
it are re-derived. Repairing in the wrong order produces work that is stale the
moment it completes.

Steps are declarative. The planner does not execute anything; it produces the
plan the recovery contract will gate on, and the agent (or a human) carries it
out. Keeping planning free of side effects means a plan can be inspected,
diffed, logged and tested without touching the world.

Risk-driven steps arrive as typed ``RiskSignal`` projections from
``continuum.recovery.risk``. Interpreting monitor payloads and locating a
rollback anchor is risk work, not planning: keeping that interpretation out of
this module is what lets ``plan_repairs`` stay a pure projection with a typed
signature instead of duck-typing over free-form events (issue #1423 review).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from continuum.models import (
    Action,
    ActionStatus,
    Component,
    ComponentValidationEntry,
    SemanticState,
    StateStatus,
)
from continuum.recovery.risk import RiskSignal, locate_last_fact_gathering_step

__all__ = ["RepairKind", "RepairStep", "RepairPlan", "plan_repairs"]


class RepairKind(StrEnum):
    """What kind of work a step represents."""

    RECONCILE_ACTION = "reconcile_action"
    """Determine whether an external side effect actually happened."""

    REPAIR_LOG = "repair_log"
    """Correct or authorise the event whose refusal stopped the log folding
    (issue #383). No automated surface can settle it yet; it is a human step
    by construction until a repair/amend command exists."""

    REVALIDATE_DEPENDENCY = "revalidate_dependency"
    """Re-pin a dependency whose version moved."""

    REDERIVE_EVIDENCE = "rederive_evidence"
    """Re-fetch evidence whose source changed."""

    REDERIVE_FINDING = "rederive_finding"
    """Recompute a finding that rested on changed support."""

    REVIEW_DECISION = "review_decision"
    """Re-examine a decision whose justification no longer holds.

    The target is always a decision id; ``next_allowed_action`` is handed to
    agents verbatim, so a step whose target is not a decision must carry its
    own kind rather than borrowing this one (issue #1423 review)."""

    REPLAN_EXCLUDING = "replan_excluding"
    """Re-plan with the named repeating trajectory excluded (issue #1423).

    The target is a step id or an action signature the risk feed flagged as
    repeating, never a decision id."""

    RENEW_APPROVAL = "renew_approval"
    """Obtain a fresh human approval."""

    REVALIDATE_MODEL_STATE = "revalidate_model_state"
    """Confirm assumptions tied to a model that is no longer active."""

    HUMAN_REVIEW = "human_review"
    """Something no automated step can settle."""


#: Lower sorts earlier. An unreadable log first: until the fold can run again,
#: nothing else about the run can even be assessed, including whether any
#: other repair did what it claimed. Uncertain side effects come next: nothing
#: else is safe while the world may or may not have been modified.
_ORDER: dict[RepairKind, int] = {
    RepairKind.REPAIR_LOG: 0,
    RepairKind.RECONCILE_ACTION: 1,
    RepairKind.HUMAN_REVIEW: 2,
    RepairKind.RENEW_APPROVAL: 3,
    RepairKind.REVALIDATE_DEPENDENCY: 4,
    RepairKind.REVALIDATE_MODEL_STATE: 5,
    RepairKind.REDERIVE_EVIDENCE: 6,
    RepairKind.REDERIVE_FINDING: 7,
    RepairKind.REVIEW_DECISION: 8,
    RepairKind.REPLAN_EXCLUDING: 9,
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

    scope: str | None = None
    """The dependency whose recovery-attempt budget this step charges.

    ``None`` means the plan could not attribute the work to exactly one
    dependency, so the attempt falls back to the run-wide bucket (issue #744).
    A step can carry at most one scope: work spanning two dependencies charges
    neither of their private budgets, which is the conservative reading.
    """

    @property
    def action_name(self) -> str:
        """The permitted-action identifier a contract gates on."""
        return f"{self.kind.value}:{self.target}"

    def render(self) -> str:
        """Render this step as a single human-readable status line, prefixed with [human] when human action is required, else [auto]."""
        mark = "[human]" if self.requires_human else "[auto] "
        detail = f" - {self.reason}" if self.reason else ""
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
        """The blocking steps, in original order."""
        return tuple(s for s in self.steps if s.blocking)

    @property
    def requires_human(self) -> bool:
        """Check if any step in this plan needs a person to step in. Returns True if at least one step has requires_human=True."""
        return any(s.requires_human for s in self.steps)

    @property
    def first(self) -> RepairStep | None:
        """The only step permitted to run next."""
        return self.steps[0] if self.steps else None

    @property
    def scopes(self) -> tuple[str, ...]:
        """Dependency budgets this plan charges, sorted and de-duplicated.

        More than one entry means the plan spans several dependencies, so a
        single budget scope cannot be named for it: the caller must fall back to
        the run-wide bucket rather than picking one and charging the other's
        noise to it (issue #744).
        """
        return tuple(sorted({s.scope for s in self.steps if s.scope is not None}))

    def of_kind(self, kind: RepairKind) -> tuple[RepairStep, ...]:
        """Get only the steps that match a specific RepairKind.

        Args:
            kind: The RepairKind to filter by.

        Returns:
            All steps where step.kind is the given kind, in original order.
        """
        return tuple(s for s in self.steps if s.kind is kind)

    def render(self) -> str:
        """Render every step as a numbered list, or "No repairs required." when the plan is empty."""
        if not self.steps:
            return "No repairs required."
        return "\n".join(f"  {i}. {s.render()}" for i, s in enumerate(self.steps, 1))


def _step_for(
    entry: ComponentValidationEntry,
    *,
    strict_unknown: bool = True,
    exhausted_dependencies: Collection[str] = (),
    run_budget_exhausted: bool = False,
) -> RepairStep | None:
    """Map one validation finding to the repair it implies."""
    if entry.status is StateStatus.VALID:
        return None

    target = entry.component_id or entry.component.value

    match entry.component:
        case Component.EXTERNAL_DEPENDENCY:
            is_exhausted = run_budget_exhausted or (target in exhausted_dependencies)
            return RepairStep(
                kind=RepairKind.REVALIDATE_DEPENDENCY,
                target=target,
                reason=entry.detail,
                # An unverifiable resource normally needs a person, because
                # nobody knows what is true. Callers who opted into tolerating
                # uncertainty get an automatic step instead: the policy has to
                # hold here too, or the setting would be silently ignored.
                requires_human=is_exhausted
                or (entry.status is StateStatus.UNKNOWN and strict_unknown),
                # The finding names the dependency that moved, so the attempt
                # is chargeable to it (issue #744). A finding with no component
                # id names no owner and stays run-wide.
                scope=entry.component_id or None,
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
        case Component.ACTION:
            return RepairStep(
                kind=RepairKind.HUMAN_REVIEW,
                target=target,
                reason=entry.detail,
                requires_human=entry.status is StateStatus.REQUIRES_REVIEW,
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
    unprojectable: tuple[int, str, str] | None = None,
    exhausted_dependencies: Collection[str] = (),
    run_budget_exhausted: bool = False,
    risk_events: Sequence[RiskSignal] = (),
    state: SemanticState | None = None,
) -> RepairPlan:
    """Build an ordered repair plan from validation findings and ledger state.

    Steps are deduplicated by identity and sorted so that prerequisites precede
    the work that depends on them. Sorting is stable and total, so the same
    inputs always yield the same plan, since a contract that varied between runs
    would be impossible to audit.

    ``unprojectable`` is ``(sequence, event_type, reason)`` for a log whose fold
    stops partway (issue #383). Without it the plan would be empty on a poisoned
    run whose last-good prefix validates cleanly, and a contract with no steps
    renders ``next_allowed: continue`` over a verdict of requires_human: prose
    and structure disagreeing, with a machine reader most likely to act on the
    structure. The break becomes a step so required_actions names real work.

    ``risk_events`` are typed ``RiskSignal`` projections from
    ``continuum.recovery.risk``, never raw events, and ``state`` is the semantic
    state their interpretation reads when a rollback target has to be located
    (issue #1423).
    """
    steps: list[RepairStep] = []

    if unprojectable is not None:
        sequence, event_type, reason = unprojectable
        steps.append(
            RepairStep(
                kind=RepairKind.REPAIR_LOG,
                target=f"sequence_{sequence}",
                reason=f"{event_type} refuses to fold: {reason}",
                requires_human=True,
            )
        )

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
        # An action tagged to an exhausted dependency also requires human (#1459).
        escalated = action.status is ActionStatus.REQUIRES_REVIEW
        is_action_exhausted = run_budget_exhausted or (
            bool(action.dep_scope) and action.dep_scope in exhausted_dependencies
        )
        needs_person = escalated or strict_unknown or is_action_exhausted
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
                # The action's own tag names the dependency it could have touched,
                # which is the ownership signal the budget needs (issue #744). An
                # untagged action owns nothing, so it charges the run-wide bucket.
                scope=action.dep_scope,
            )
        )

    for entry in findings:
        step = _step_for(
            entry,
            strict_unknown=strict_unknown,
            exhausted_dependencies=exhausted_dependencies,
            run_budget_exhausted=run_budget_exhausted,
        )
        if step is not None:
            steps.append(step)

    for signal in risk_events:
        trig_lower = signal.trigger.strip().lower()

        if trig_lower == "loop":
            targets: list[str] = []
            if signal.step_id is not None:
                targets.append(signal.step_id)
            repeated = (
                signal.detail.get("repeating_steps")
                or signal.detail.get("repeated_steps")
                or signal.detail.get("action_signature")
                or signal.detail.get("action")
            )
            if isinstance(repeated, (list, tuple)):
                for item in repeated:
                    cleaned = item.strip() if isinstance(item, str) else None
                    if cleaned and cleaned not in targets:
                        targets.append(cleaned)
            else:
                cleaned = repeated.strip() if isinstance(repeated, str) else None
                if cleaned and cleaned not in targets:
                    targets.append(cleaned)
            if not targets:
                # A loop verdict with no locatable target leaves the replanning
                # agent nothing to avoid: the gap #1426 describes. Name the
                # episode (or at least the trigger) so the plan is still
                # actionable instead of empty under a replan verdict.
                targets.append(signal.episode_id or f"trigger_{trig_lower}")
            for target in targets:
                # The parenthetical is this step's own provenance: the event's
                # step_id would describe a different step than the one this
                # repair names (issue #1423 review), so the target itself and
                # the trigger and episode are all that belong here.
                reason = (
                    f"replan excluding the repeating trajectory at {target} "
                    f"(trigger: {trig_lower}, episode_id: {signal.episode_id or 'none'})"
                )
                steps.append(
                    RepairStep(
                        kind=RepairKind.REPLAN_EXCLUDING,
                        target=target,
                        reason=reason,
                        blocking=True,
                        requires_human=False,
                    )
                )

        elif trig_lower == "meltdown":
            fact_step = locate_last_fact_gathering_step(
                state, trigger_step_id=signal.step_id, detail=signal.detail
            )
            trigger_step = signal.step_id or "none"
            episode = signal.episode_id or "none"
            if fact_step is not None:
                target = fact_step
                reason = (
                    f"rollback to the last verified fact-gathering step {target} "
                    f"before the anomalous cascade (step_id: {trigger_step}, "
                    f"trigger: {trig_lower}, episode_id: {episode})"
                )
            else:
                # No honest anchor exists. Never back-fill the trigger step and
                # claim it is the last verified fact-gathering step: it is the
                # step that was melting down, and the reason is the one field
                # an operator reads first (issue #1423 review).
                target = signal.step_id or signal.episode_id or "run"
                reason = (
                    f"no verified fact-gathering step could be located before the "
                    f"anomalous cascade; the rollback point must be chosen by "
                    f"inspecting {target} (step_id: {trigger_step}, trigger: "
                    f"{trig_lower}, episode_id: {episode})"
                )
            steps.append(
                RepairStep(
                    kind=RepairKind.HUMAN_REVIEW,
                    target=target,
                    reason=reason,
                    blocking=True,
                    requires_human=True,
                )
            )

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
