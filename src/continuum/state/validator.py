"""Deciding whether a checkpoint can still be trusted.

The rule this module exists to enforce: **a persisted checkpoint is not
trustworthy merely because it was persisted.** Before an agent resumes, every
component is checked against the environment as it is *now*.

Staleness propagates. If a dataset moves from v3 to v4, the dependency is not
the only casualty — every finding whose evidence came from that dataset, and
every decision resting on those findings, is now suspect. Marking only the
dependency would leave the agent reasoning from conclusions it can no longer
justify. Propagation walks:

    dependency -> evidence -> finding -> decision

Uncertainty degrades rather than resolves. An unverifiable resource yields
``UNKNOWN``, not ``VALID``, and ``UNKNOWN`` is enough to withhold a clean
resume. The system is allowed to say "I cannot tell"; it is not allowed to
guess in its own favour.

This module decides *status*. Choosing what to do about it — resume, repair,
abort — is the recovery engine's job in Phase 7.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from continuum.environment.diff import EnvironmentDiff, ResourceChange, diff_environments
from continuum.models import (
    ApprovalStatus,
    Component,
    ComponentValidationEntry,
    EnvironmentSnapshot,
    SemanticState,
    StateStatus,
    StateValidationResult,
    utcnow,
)

__all__ = ["StateValidator", "ValidationOutcome", "validate_state"]


#: Statuses that mean the component cannot be relied on as-is.
#:
#: REQUIRES_REVIEW belongs here: something is asking for human judgement, and
#: reporting "safe to resume" while that is outstanding is precisely the false
#: assurance this layer exists to prevent. The recovery engine already refused
#: to resume in these cases via the repair plan, but the validator's own
#: `safe_to_resume` disagreed with it — so anything reading the validation
#: report directly got the wrong answer.
_UNUSABLE = frozenset(
    {
        StateStatus.INVALID,
        StateStatus.STALE,
        StateStatus.CONFLICTED,
        StateStatus.EXPIRED,
        StateStatus.UNKNOWN,
        StateStatus.REQUIRES_REVIEW,
    }
)


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """The validated state plus the report explaining every downgrade.

    ``state`` carries the *revised* statuses, so callers act on a state that
    already knows what is stale rather than re-deriving it.
    """

    state: SemanticState
    report: StateValidationResult
    environment_diff: EnvironmentDiff

    @property
    def safe(self) -> bool:
        return self.report.safe_to_resume

    @property
    def downgraded(self) -> tuple[ComponentValidationEntry, ...]:
        return tuple(e for e in self.report.statuses if e.status is not StateStatus.VALID)

    def render(self) -> str:
        lines = [f"Run: {self.report.run_id}", f"Checkpoint: v{self.report.checkpoint_version}", ""]
        symbols = {StateStatus.VALID: "[ok]"}
        for entry in self.report.statuses:
            mark = symbols.get(entry.status, "[!!]")
            label = entry.component.value.replace("_", " ")
            identifier = f" {entry.component_id}" if entry.component_id else ""
            detail = f" - {entry.detail}" if entry.detail else ""
            lines.append(f"{mark} {label}{identifier}: {entry.status}{detail}")
        lines.append("")
        lines.append(f"Safe to resume: {'yes' if self.safe else 'no'}")
        if self.report.reason:
            lines.append(f"Reason: {self.report.reason}")
        return "\n".join(lines)


class StateValidator:
    """Checks a checkpoint against the current environment.

    ``strict_unknown`` controls whether unverifiable resources block a clean
    resume. It defaults to True because failing open is how silent corruption
    starts; a caller who genuinely tolerates uncertainty can opt out.
    """

    def __init__(self, *, strict_unknown: bool = True, confirmed: bool = False) -> None:
        self.strict_unknown = strict_unknown
        # Set when a human has explicitly confirmed the run's self-reported
        # goal/progress (via a REVIEW_CONFIRMED event). Confirmation clears the
        # REQUIRES_REVIEW that self-certified origins would otherwise force, so
        # an externally-driven run can be resumed after a human has eyeballed it.
        self.confirmed = confirmed

    def validate(
        self,
        state: SemanticState,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        checkpoint_environment: EnvironmentSnapshot | None = None,
        checkpoint_version: int = 0,
        expected_model: str | None = None,
        confirmed: bool = False,
        scope: Iterable[str] | None = None,
    ) -> ValidationOutcome:
        self.confirmed = confirmed
        environment_diff = diff_environments(checkpoint_environment, current_environment)
        entries: list[ComponentValidationEntry] = []

        broken = self._broken_resources(environment_diff)

        # diff_environments returns an empty diff when *either* snapshot is
        # absent, so the diff alone cannot distinguish "the caller supplied no
        # observation" from "the caller supplied one that omits this resource".
        # Only this frame knows which it was, so the distinction is passed down.
        observed = current_environment is not None

        if scope is None:
            state = self._apply_dependency_status(
                state, environment_diff, entries, observed=observed
            )
            state = self._propagate(state, broken, entries)
            self._check_goal(state, entries)
            self._check_progress(state, entries)
            self._check_approvals(state, entries)
            self._check_model(state, expected_model, entries)
            self._check_evidence(state, entries)
            self._check_derived(state, entries)
        else:
            # Scoped re-validation: only the named dependency resources are
            # re-checked and only their derivation subtree is allowed to go
            # stale. Everything else keeps the status it already had, so a
            # localized recovery does not re-taint components it is not
            # responsible for.
            scope_set = set(scope)
            broken = {r: c for r, c in broken.items() if r in scope_set}
            state = self._apply_dependency_status(
                state, environment_diff, entries, scope=scope_set, observed=observed
            )
            state = self._propagate(state, broken, entries)
            self._check_derived(state, entries)

        blocking = [
            e
            for e in entries
            if e.status in _UNUSABLE
            and (self.strict_unknown or e.status is not StateStatus.UNKNOWN)
        ]
        safe = not blocking
        reason = (
            "all components verified against the current environment"
            if safe
            else "; ".join(
                f"{e.component.value}{f' {e.component_id}' if e.component_id else ''} is {e.status}"
                for e in blocking[:5]
            )
        )

        report = StateValidationResult(
            run_id=state.run_id,
            checkpoint_version=checkpoint_version,
            statuses=entries,
            safe_to_resume=safe,
            reason=reason,
            validated_at=utcnow(),
        )
        return ValidationOutcome(state=state, report=report, environment_diff=environment_diff)

    # -- environment ------------------------------------------------------ #

    @staticmethod
    def _broken_resources(diff: EnvironmentDiff) -> Mapping[str, ResourceChange]:
        return {d.resource: d.change for d in diff.breaking}

    def _apply_dependency_status(
        self,
        state: SemanticState,
        diff: EnvironmentDiff,
        entries: list[ComponentValidationEntry],
        scope: set[str] | None = None,
        observed: bool = True,
    ) -> SemanticState:
        if not state.external_dependencies:
            return state

        updated = []
        for dependency in state.external_dependencies:
            if scope is not None and dependency.resource not in scope:
                # Out of scope: preserve the status already recorded for it.
                updated.append(dependency)
                continue
            delta = diff.for_resource(dependency.resource)
            status = dependency.status
            detail = ""

            if delta is None:
                if diff.deltas:
                    status = StateStatus.UNKNOWN
                    detail = "not present in the current environment snapshot"
                else:
                    # A checkpoint snapshot may well exist; what is absent is the
                    # caller's *current* observation, so name that rather than
                    # blaming the stored side, which is the one thing definitely
                    # present (issue #307). The status still degrades to UNKNOWN:
                    # never validated is not the same as validated clean, and
                    # strict_unknown is how a caller opts out of that.
                    detail = (
                        (
                            "no current version supplied for comparison; pass "
                            f'env={{"{dependency.resource}": "<version>"}}'
                        )
                        if not observed
                        else "no environment snapshot to compare against"
                    )
                    status = StateStatus.UNKNOWN if self.strict_unknown else dependency.status
            elif delta.change is ResourceChange.CHANGED:
                status = StateStatus.CONFLICTED
                detail = f"{delta.before} -> {delta.after}"
            elif delta.change is ResourceChange.REMOVED:
                status = StateStatus.INVALID
                detail = "resource no longer exists"
            elif delta.change is ResourceChange.UNKNOWN:
                status = StateStatus.UNKNOWN
                detail = delta.detail
            else:
                status = StateStatus.VALID
                detail = "verified unchanged"

            updated.append(dependency.model_copy(update={"status": status}))
            entries.append(
                ComponentValidationEntry(
                    component=Component.EXTERNAL_DEPENDENCY,
                    component_id=dependency.resource,
                    status=status,
                    detail=detail,
                )
            )

        return state.model_copy(update={"external_dependencies": updated})

    # -- propagation ------------------------------------------------------ #

    def _propagate(
        self,
        state: SemanticState,
        broken: Mapping[str, ResourceChange],
        entries: list[ComponentValidationEntry],
    ) -> SemanticState:
        """Cascade staleness from broken resources through the evidence graph."""
        if not broken:
            return state

        tainted_evidence: set[str] = set()
        evidence = []
        for item in state.evidence:
            source = item.source
            if source is not None and source in broken:
                change = broken[source]
                status = (
                    StateStatus.UNKNOWN if change is ResourceChange.UNKNOWN else StateStatus.STALE
                )
                tainted_evidence.add(item.evidence_id)
                evidence.append(item.model_copy(update={"status": status}))
                entries.append(
                    ComponentValidationEntry(
                        component=Component.EVIDENCE,
                        component_id=item.evidence_id,
                        status=status,
                        detail=f"source {source!r} changed",
                    )
                )
            else:
                evidence.append(item)

        tainted_findings: set[str] = set()
        findings = []
        for finding in state.findings:
            affected = sorted(set(finding.evidence) & tainted_evidence)
            if affected and finding.status is StateStatus.VALID:
                tainted_findings.add(finding.finding_id)
                findings.append(finding.model_copy(update={"status": StateStatus.STALE}))
                entries.append(
                    ComponentValidationEntry(
                        component=Component.FINDING,
                        component_id=finding.finding_id,
                        status=StateStatus.STALE,
                        detail=f"rests on changed evidence: {', '.join(affected)}",
                    )
                )
            else:
                findings.append(finding)

        decisions = []
        for decision in state.decisions:
            affected = sorted(set(decision.evidence) & (tainted_evidence | tainted_findings))
            if affected and decision.status is StateStatus.VALID:
                decisions.append(
                    decision.model_copy(
                        update={
                            "status": StateStatus.STALE,
                            "invalidated_reason": (
                                f"supporting state changed: {', '.join(affected)}"
                            ),
                            "invalidated_at": utcnow(),
                        }
                    )
                )
                entries.append(
                    ComponentValidationEntry(
                        component=Component.DECISION,
                        component_id=decision.decision_id,
                        status=StateStatus.STALE,
                        detail=f"rests on changed support: {', '.join(affected)}",
                    )
                )
            else:
                decisions.append(decision)

        return state.model_copy(
            update={"evidence": evidence, "findings": findings, "decisions": decisions}
        )

    # -- per-component checks --------------------------------------------- #

    def _check_goal(self, state: SemanticState, entries: list[ComponentValidationEntry]) -> None:
        origin = state.goal.provenance.origin
        if origin.self_certified and not self.confirmed:
            entries.append(
                ComponentValidationEntry(
                    component=Component.GOAL,
                    status=StateStatus.REQUIRES_REVIEW,
                    detail=f"v{state.goal.version}, asserted by {origin.value}",
                )
            )
            return
        entries.append(
            ComponentValidationEntry(
                component=Component.GOAL,
                status=StateStatus.VALID,
                detail=f"v{state.goal.version}",
            )
        )

    def _check_progress(
        self, state: SemanticState, entries: list[ComponentValidationEntry]
    ) -> None:
        # Counter arithmetic (completed + pending + failed <= total) is enforced
        # by the Progress model on construction *and* on deserialization, so a
        # state that reaches here cannot violate it. Re-checking would be dead
        # code; the invariant is tested at the model level instead.
        progress = state.progress
        status, detail = StateStatus.VALID, ""

        # An agent reporting its own progress has not verified anything. The
        # figure may be perfectly accurate, but nothing independent supports
        # it, so it cannot count as verified state. A human confirmation
        # (REVIEW_CONFIRMED) clears this so the run can resume.
        origin = progress.provenance.origin
        if origin.self_certified and not self.confirmed:
            status = StateStatus.REQUIRES_REVIEW
            detail = (
                f"{progress.completed} completed, self-reported by {origin.value} "
                f"and not independently verified"
            )
        elif state.source_sequence == 0 and progress.completed > 0:
            # Self-certified progress is already REQUIRES_REVIEW above and must
            # stay that way, so this branch only downgrades independently
            # recorded progress to UNKNOWN, never a self-report.
            status = StateStatus.UNKNOWN
            detail = "progress recorded but no source events"

        entries.append(
            ComponentValidationEntry(
                component=Component.PROGRESS,
                status=status,
                detail=detail or f"{progress.completed} completed",
            )
        )

    @staticmethod
    def _check_approvals(state: SemanticState, entries: list[ComponentValidationEntry]) -> None:
        now = utcnow()
        for approval in state.approvals:
            if approval.status is ApprovalStatus.REVOKED:
                status, detail = StateStatus.INVALID, "approval was revoked"
            elif approval.status is ApprovalStatus.EXPIRED:
                status, detail = StateStatus.EXPIRED, "approval expired"
            elif approval.expires_at is not None and approval.expires_at <= now:
                status, detail = (
                    StateStatus.EXPIRED,
                    f"expired at {approval.expires_at.isoformat()}",
                )
            elif approval.status is ApprovalStatus.PENDING:
                status, detail = StateStatus.REQUIRES_REVIEW, "approval never granted"
            else:
                status, detail = StateStatus.VALID, "granted"

            entries.append(
                ComponentValidationEntry(
                    component=Component.APPROVAL,
                    component_id=approval.approval_id,
                    status=status,
                    detail=detail,
                )
            )

    @staticmethod
    def _check_model(
        state: SemanticState,
        expected_model: str | None,
        entries: list[ComponentValidationEntry],
    ) -> None:
        recorded = state.model.model if state.model else None
        assumptions = list(state.model.model_specific_state) if state.model else []

        if expected_model is not None and recorded is not None and expected_model != recorded:
            # The model changed. Anything model-specific must be revalidated,
            # and switching models is never assumed safe.
            entries.append(
                ComponentValidationEntry(
                    component=Component.MODEL,
                    component_id=recorded,
                    status=StateStatus.REQUIRES_REVIEW if not assumptions else StateStatus.STALE,
                    detail=(
                        f"state was produced by {recorded!r} but {expected_model!r} is now active"
                        + (
                            f"; {len(assumptions)} model-specific assumption(s) need revalidation"
                            if assumptions
                            else ""
                        )
                    ),
                )
            )
            return

        if expected_model is not None and recorded is None:
            # The caller asked for a drift check the state cannot answer: no
            # writer ever recorded which model produced this run. Returning
            # silently here reads as "no drift" and is indistinguishable from a
            # clean comparison, so a caller passing expected_model believes it
            # got an assurance it never received (issue #308). Report the gap
            # instead. Reached whenever no writer named the model: pass model_id
            # to continuum_checkpoint to make the comparison answerable (#370).
            entries.append(
                ComponentValidationEntry(
                    component=Component.MODEL,
                    component_id=None,
                    status=StateStatus.UNKNOWN,
                    detail=(
                        f"no model recorded for this run, cannot compare against {expected_model!r}"
                    ),
                )
            )
            return

        if not assumptions:
            return

        if expected_model is None or recorded is None:
            # Assumptions were recorded but either the resume model or the
            # recording model is unknown, so they cannot be verified. Say so
            # rather than guessing "valid" in the state's favour.
            unknown_side = (
                "resume model is unknown" if expected_model is None else "recorded model is unknown"
            )
            entries.append(
                ComponentValidationEntry(
                    component=Component.MODEL,
                    component_id=recorded,
                    status=StateStatus.UNKNOWN,
                    detail=(
                        f"{len(assumptions)} model-specific assumption(s) recorded but the "
                        f"{unknown_side}; cannot verify"
                    ),
                )
            )
            return

        # expected_model == recorded: assumptions were verified against the
        # model that will actually resume the run.
        entries.append(
            ComponentValidationEntry(
                component=Component.MODEL,
                component_id=recorded,
                status=StateStatus.VALID,
                detail=f"{len(assumptions)} model-specific assumption(s) recorded",
            )
        )

    @staticmethod
    def _check_evidence(state: SemanticState, entries: list[ComponentValidationEntry]) -> None:
        dangling = sorted(state.dangling_evidence())
        if dangling:
            entries.append(
                ComponentValidationEntry(
                    component=Component.EVIDENCE,
                    status=StateStatus.UNKNOWN,
                    detail=f"cited but unavailable: {', '.join(dangling)}",
                )
            )

    def _check_derived(self, state: SemanticState, entries: list[ComponentValidationEntry]) -> None:
        """Derived artifacts must never amplify weak sources (issue #392)."""
        for finding in state.findings:
            if finding.provenance.origin.self_certified and finding.status is StateStatus.VALID:
                entries.append(
                    ComponentValidationEntry(
                        component=Component.FINDING,
                        component_id=finding.finding_id,
                        status=StateStatus.REQUIRES_REVIEW,
                        detail=f"derived from {finding.provenance.origin.value} and not independently verified",
                    )
                )
        for decision in state.decisions:
            if decision.provenance.origin.self_certified and decision.status is StateStatus.VALID:
                entries.append(
                    ComponentValidationEntry(
                        component=Component.DECISION,
                        component_id=decision.decision_id,
                        status=StateStatus.REQUIRES_REVIEW,
                        detail=f"derived from {decision.provenance.origin.value} and not independently verified",
                    )
                )
        for ev in state.evidence:
            if ev.provenance.origin.self_certified and ev.status is StateStatus.VALID:
                entries.append(
                    ComponentValidationEntry(
                        component=Component.EVIDENCE,
                        component_id=ev.evidence_id,
                        status=StateStatus.REQUIRES_REVIEW,
                        detail=f"derived from {ev.provenance.origin.value} and not independently verified",
                    )
                )


def validate_state(
    state: SemanticState,
    *,
    current_environment: EnvironmentSnapshot | None = None,
    checkpoint_environment: EnvironmentSnapshot | None = None,
    checkpoint_version: int = 0,
    expected_model: str | None = None,
    strict_unknown: bool = True,
    confirmed: bool = False,
    scope: Iterable[str] | None = None,
) -> ValidationOutcome:
    """Validate a state against the current environment.

    When ``scope`` names specific dependency resources, only those resources are
    re-checked and only their derivation subtree may go stale; the rest of the
    state keeps its recorded status (localized recovery).
    """
    return StateValidator(strict_unknown=strict_unknown, confirmed=confirmed).validate(
        state,
        current_environment=current_environment,
        checkpoint_environment=checkpoint_environment,
        checkpoint_version=checkpoint_version,
        expected_model=expected_model,
        confirmed=confirmed,
        scope=scope,
    )
