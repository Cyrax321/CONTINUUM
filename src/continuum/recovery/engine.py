"""Deciding how — and whether — a run may resume.

The engine reduces three independent signals to one decision:

* validation statuses (Phase 5) — is the state still true?
* the action ledger (Phase 6) — did an external effect land?
* checkpoint integrity (Phases 3–4) — is the record itself sound?

The decision rule
-----------------

**The most cautious applicable signal wins.** Not the first one evaluated, not
the most common — the most cautious. Each signal proposes a mode; the engine
takes the maximum on a severity ordering:

    RESUME < REPAIR_AND_RESUME < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT

Order-independence matters because these signals genuinely co-occur. A run can
have a stale dataset *and* an uncertain side effect at once. If the engine
returned whichever it noticed first, the same situation would recover
differently depending on iteration order — and the unsafe answer would win
roughly half the time. Taking the maximum makes the outcome deterministic and
always errs toward caution.

What the engine does not do
---------------------------

It does not execute repairs, mutate the run, or contact anything external. It
reads state and returns a decision plus a contract. Keeping it free of side
effects means a recovery decision can be computed, logged and reviewed without
committing to it — which is what makes ``continuum validate`` safe to run
against a live database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from continuum.actions.ledger import ActionLedger
from continuum.analysis.depends import DependencyGraph as SourceDependencyGraph
from continuum.checkpoint.manager import CheckpointManager, RestoredRun
from continuum.environment.diff import EnvironmentDiff
from continuum.events import EventType
from continuum.models import (
    Action,
    ActionStatus,
    EnvironmentSnapshot,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    SemanticState,
    StateStatus,
)
from continuum.recovery.contract import build_contract
from continuum.recovery.observations import collect_observations
from continuum.recovery.planner import RepairPlan, plan_repairs
from continuum.recovery.summary import build_informed_retry
from continuum.state.validator import StateValidator, ValidationOutcome
from continuum.storage.base import Storage

__all__ = ["RecoveryEngine", "RecoveryDecision", "SEVERITY"]


#: Ascending caution. The engine always returns the maximum proposed mode.
SEVERITY: dict[RecoveryMode, int] = {
    RecoveryMode.RESUME: 0,
    RecoveryMode.REPAIR_AND_RESUME: 1,
    RecoveryMode.REPLAN: 2,
    RecoveryMode.WAIT: 3,
    RecoveryMode.REQUEST_HUMAN: 4,
    RecoveryMode.ROLLBACK: 5,
    RecoveryMode.ABORT: 6,
}

_SAFETY_FOR_MODE: dict[RecoveryMode, RecoverySafety] = {
    RecoveryMode.RESUME: RecoverySafety.SAFE_TO_RESUME,
    RecoveryMode.REPAIR_AND_RESUME: RecoverySafety.REQUIRES_REPAIR,
    RecoveryMode.REPLAN: RecoverySafety.REQUIRES_REVALIDATION,
    RecoveryMode.WAIT: RecoverySafety.REQUIRES_REVALIDATION,
    RecoveryMode.REQUEST_HUMAN: RecoverySafety.REQUIRES_HUMAN,
    RecoveryMode.ROLLBACK: RecoverySafety.BLOCKED,
    RecoveryMode.ABORT: RecoverySafety.UNSAFE,
}


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """The engine's verdict, with everything needed to justify it."""

    run_id: str
    mode: RecoveryMode
    contract: RecoveryContract
    plan: RepairPlan
    validation: ValidationOutcome
    restored: RestoredRun
    uncertain_actions: tuple[Action, ...] = ()
    rationale: tuple[str, ...] = ()
    impacted_files: frozenset[str] = frozenset()
    tail_evidence: str | None = None
    #: Informed-retry block (#265): a pure projection of prior recovery-path
    #: events plus current failure signals, or None when there is no history.
    #: Informational only; presence never changes mode or safety.
    informed_retry: dict[str, Any] | None = None

    @property
    def state(self) -> SemanticState:
        """State with validation statuses already applied."""
        return self.validation.state

    @property
    def safe(self) -> bool:
        return self.mode is RecoveryMode.RESUME

    @property
    def environment_diff(self) -> EnvironmentDiff:
        return self.validation.environment_diff

    @property
    def next_allowed_action(self) -> str | None:
        return self.contract.next_allowed_action

    def permits(self, action: str) -> bool:
        """Whether ``action`` is the one step the contract currently allows."""
        if self.mode is RecoveryMode.RESUME:
            return True
        return action == self.contract.next_allowed_action

    def render(self) -> str:
        lines = [
            "CONTINUUM RECOVERY",
            "",
            f"Run: {self.run_id}",
            f"Checkpoint: v{self.contract.checkpoint_version}",
            "",
            "State validation:",
        ]
        for entry in self.validation.report.statuses:
            mark = "[ok]" if entry.status is StateStatus.VALID else "[!!]"
            label = entry.component.value.replace("_", " ")
            identifier = f" {entry.component_id}" if entry.component_id else ""
            detail = f" - {entry.detail}" if entry.detail else ""
            lines.append(f"  {mark} {label}{identifier}{detail}")

        if self.uncertain_actions:
            lines.append("")
            lines.append("Action ledger:")
            for action in self.uncertain_actions:
                lines.append(
                    f"  [!!] {action.action_type}: outcome unknown (id {action.action_id[:14]})"
                )
        elif self.restored.from_checkpoint:
            lines.append("")
            lines.append("Action ledger:  [ok] no uncertain side effects")

        lines += ["", f"Recovery decision: {self.mode.value.upper()}"]
        for reason in self.rationale:
            lines.append(f"  because {reason}")

        if self.plan:
            lines += ["", "Repairs required:", self.plan.render()]

        permitted = self.next_allowed_action or (
            "continue"
            if self.mode is RecoveryMode.RESUME
            else "none (settle the repairs above first)"
        )
        lines += ["", f"Next permitted action: {permitted}"]
        return "\n".join(lines)


class RecoveryEngine:
    """Computes how a run may resume. Read-only."""

    def __init__(
        self,
        storage: Storage,
        *,
        validator: StateValidator | None = None,
        strict_unknown: bool = True,
    ) -> None:
        self.storage = storage
        self.validator = validator or StateValidator(strict_unknown=strict_unknown)
        self.strict_unknown = strict_unknown
        self._manager = CheckpointManager(storage)

    def assess(
        self,
        run_id: str,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
        replay: bool = True,
        scope: Iterable[str] | None = None,
        source_graph: SourceDependencyGraph | None = None,
    ) -> RecoveryDecision:
        """Decide how ``run_id`` may resume, without changing anything.

        When ``scope`` names specific dependency resources, validation and the
        resulting repair plan are confined to those resources' derivation
        subtree; the rest of the state is left exactly as it was. The issued
        contract records the scope so the localization is auditable.

        Scoping also applies to the action ledger. An uncertain side effect
        explicitly tagged via ``dep_scope`` to a dependency outside the scope
        cannot have invalidated the part being resumed, so it does not block
        this decision. Untagged actions stay blocking: they could have touched
        anything, which is the conservative reading.

        Passing ``source_graph`` (the source-level graph from
        :mod:`continuum.analysis`) records on the returned decision every file
        whose imports belong to a scoped dependency.
        """
        # Degrade, not raise (issue #383): the engine's whole job is to answer
        # "where does this run stand", and a poisoned log is precisely the run
        # that needs an answer. An unprojectable tail yields the last-good
        # prefix marked INVALID; _decide turns that into request_human rather
        # than letting it pass for a complete state.
        restored = self._manager.restore(run_id, replay=replay, on_unprojectable="degrade")
        checkpoint_environment = restored.checkpoint.environment if restored.checkpoint else None
        checkpoint_version = restored.checkpoint.version if restored.checkpoint else 0

        # A human confirmation (REVIEW_CONFIRMED event) clears the self_certified
        # REQUIRES_REVIEW on goal/progress, unblocking an otherwise permanent
        # request_human for externally-driven runs. See issue #35.
        has_confirmation = any(
            e.type is EventType.REVIEW_CONFIRMED for e in self.storage.read_events(run_id)
        )

        validation = self.validator.validate(
            restored.state,
            current_environment=current_environment,
            checkpoint_environment=checkpoint_environment,
            checkpoint_version=checkpoint_version,
            expected_model=expected_model,
            confirmed=has_confirmation,
            scope=scope,
        )

        ledger = ActionLedger(self.storage, run_id)
        all_uncertain = tuple(
            a
            for a in ledger.all()
            if a.status
            in (ActionStatus.UNKNOWN, ActionStatus.STARTED, ActionStatus.REQUIRES_REVIEW)
        )
        # An action tagged to a dependency outside the repair scope cannot have
        # invalidated the part being resumed, so it does not block here. An
        # untagged action could have touched anything and stays blocking.
        if scope is None:
            uncertain = all_uncertain
        else:
            allowed = frozenset(scope)
            uncertain = tuple(
                a for a in all_uncertain if a.dep_scope is None or a.dep_scope in allowed
            )

        impacted_files: frozenset[str] = frozenset()
        if source_graph is not None and scope is not None:
            impacted_files = frozenset(
                str(path)
                for resource in frozenset(scope)
                for path in source_graph.files_using(resource)
            )

        # The degraded fold must reach the plan (issue #383): without a step,
        # required_actions is empty and the contract's next_allowed falls
        # through to "continue" over a requires_human verdict.
        broken = restored.state
        unprojectable = (
            (
                broken.unprojectable_at_sequence,
                broken.unprojectable_event_type or "unknown event",
                broken.unprojectable_reason or "unknown projection failure",
            )
            if broken.status is StateStatus.INVALID and broken.unprojectable_at_sequence is not None
            else None
        )
        plan = plan_repairs(
            validation.report.statuses,
            uncertain_actions=uncertain,
            strict_unknown=self.validator.strict_unknown,
            unprojectable=unprojectable,
        )
        mode, rationale = self._decide(validation, uncertain, plan, restored, self.strict_unknown)

        reason = "; ".join(rationale) if rationale else validation.report.reason

        # Post-checkpoint file observations (#208): informational evidence for
        # the resuming agent, never a factor in the decision itself.
        after_sequence = 0
        latest_version = self.storage.latest_version(run_id)
        if latest_version is not None:
            after_sequence = latest_version.source_sequence
        observations = collect_observations(self.storage, run_id, after_sequence=after_sequence)

        contract = build_contract(
            run_id=run_id,
            checkpoint_version=checkpoint_version,
            safety=_SAFETY_FOR_MODE[mode],
            validation=validation,
            plan=plan,
            reason=reason,
            scope=scope,
            post_checkpoint_observations=observations,
        )

        tail_evidence: str | None = None
        for ev in reversed(validation.state.evidence):
            if ev.evidence_id.startswith("tail_"):
                tail_evidence = ev.summary
                break

        # Informed retry (#265): derive the prior-attempt account from events
        # already in the chain. Read-only, deterministic, None without history.
        informed_retry = build_informed_retry(
            self.storage,
            run_id,
            validation_report=validation.report,
            uncertain_actions=uncertain,
            plan=plan,
        )

        return RecoveryDecision(
            run_id=run_id,
            mode=mode,
            contract=contract,
            plan=plan,
            validation=validation,
            restored=restored,
            uncertain_actions=uncertain,
            rationale=rationale,
            impacted_files=impacted_files,
            tail_evidence=tail_evidence,
            informed_retry=informed_retry,
        )

    # -- the decision rule ------------------------------------------------ #

    def assess_scoped(
        self,
        run_id: str,
        resources: Iterable[str],
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
        replay: bool = True,
        source_graph: SourceDependencyGraph | None = None,
    ) -> RecoveryDecision:
        """Assess ``run_id`` confined to the derivation subtree of ``resources``.

        A thin wrapper over :meth:`assess` that passes ``scope`` through; kept as
        a named entry point so callers express intent (localized recovery) rather
        than remembering the keyword. ``source_graph`` forwards to
        :meth:`assess` for impacted-file reporting.
        """
        return self.assess(
            run_id,
            current_environment=current_environment,
            expected_model=expected_model,
            replay=replay,
            scope=resources,
            source_graph=source_graph,
        )

    def _decide(
        self,
        validation: ValidationOutcome,
        uncertain: Sequence[Action],
        plan: RepairPlan,
        restored: RestoredRun,
        strict_unknown: bool,
    ) -> tuple[RecoveryMode, tuple[str, ...]]:
        """Collect a proposal per signal and return the most cautious."""
        proposals: list[tuple[RecoveryMode, str]] = []

        # A degraded fold (issue #383) outranks every signal computed below,
        # which is why it is proposed unconditionally rather than guarded: the
        # validation below ran on the last-good prefix, so its statuses describe
        # only what was known before the break. REQUEST_HUMAN, not ABORT: the
        # log is not necessarily dead (a human can confirm or rewrite the
        # offending event), and declaring the run permanently unanswerable is
        # exactly the terminal state #383 exists to remove.
        state = restored.state
        if state.unprojectable_at_sequence is not None and state.status is StateStatus.INVALID:
            proposals.append(
                (
                    RecoveryMode.REQUEST_HUMAN,
                    f"the event log stops folding at sequence "
                    f"{state.unprojectable_at_sequence} "
                    f"({state.unprojectable_event_type}: {state.unprojectable_reason}); "
                    f"this verdict covers events through sequence "
                    f"{state.source_sequence} only",
                )
            )

        if validation.safe and not uncertain:
            proposals.append((RecoveryMode.RESUME, "all state verified against the environment"))

        # An uncertain side effect outranks everything repairable: until we know
        # whether the world was modified, no further work is safe.
        if uncertain:
            reviewable = [a for a in uncertain if a.status is ActionStatus.REQUIRES_REVIEW]
            if reviewable:
                proposals.append(
                    (
                        RecoveryMode.REQUEST_HUMAN,
                        f"{len(reviewable)} side effect(s) could not be reconciled automatically",
                    )
                )
            elif strict_unknown:
                proposals.append(
                    (
                        RecoveryMode.REQUEST_HUMAN,
                        f"{len(uncertain)} external side effect(s) have unknown outcomes",
                    )
                )
            else:
                # Non-strict: pause and wait rather than immediately escalating to
                # human intervention. The caller has opted in to tolerating uncertainty.
                proposals.append(
                    (
                        RecoveryMode.WAIT,
                        f"{len(uncertain)} external side effect(s) have unknown outcomes (lenient mode)",
                    )
                )

        if not validation.safe:
            # Count only what actually needs repair. Reporting every status
            # would include the VALID ones and overstate the damage — a run
            # with two verified components and one stale one would claim three
            # need repair. The decision itself is unaffected; the operator
            # reading the rationale is not.
            needs_repair = [
                e for e in validation.report.statuses if e.status is not StateStatus.VALID
            ]
            proposals.append(
                (
                    RecoveryMode.REPAIR_AND_RESUME,
                    f"{len(needs_repair)} component(s) need repair before continuing",
                )
            )

        if plan.requires_human:
            proposals.append((RecoveryMode.REQUEST_HUMAN, "at least one repair needs a person"))

        # A goal that is no longer valid cannot be repaired by re-running work.
        if any(
            e.component.value == "goal" and e.status is not StateStatus.VALID
            for e in validation.report.statuses
        ):
            proposals.append((RecoveryMode.REPLAN, "the goal itself is no longer valid"))

        # A run with neither checkpoint nor events cannot reach here: restore()
        # raises CheckpointError first, so there is nothing to decide about.
        # ABORT is reserved for callers who reject a contract outright.
        #
        # `proposals` is likewise never empty: either validation was clean and
        # the ledger quiet (RESUME), or something is blocking and produced an
        # entry. Both facts are asserted by tests rather than defended by dead
        # branches here.
        mode = max(proposals, key=lambda p: SEVERITY[p[0]])[0]
        rationale = tuple(reason for proposed, reason in proposals if proposed is mode)
        return mode, rationale
