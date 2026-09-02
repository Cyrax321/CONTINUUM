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
from typing import Any

from continuum.environment.diff import EnvironmentDiff, ResourceChange, diff_environments
from continuum.events import Event  # for caused_by graph
from continuum.models import (
    Action,
    ActionStatus,
    ApprovalStatus,
    Component,
    ComponentValidationEntry,
    EnvironmentSnapshot,
    SemanticState,
    StateCheckpoint,
    StateStatus,
    StateValidationResult,
    utcnow,
)

__all__ = ["StateValidator", "ValidationOutcome", "validate_state", "AdmissibilityResult", "check_admissibility"]


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
class AdmissibilityResult:
    """Result of checking whether a checkpoint is admissible for plain RESUME.

    A checkpoint is inadmissible when a completed downstream action consumed
    state produced after the checkpoint. The check is a deterministic graph
    reachability over hash-chained positions, no heuristics.
    """

    admissible: bool
    blocking: tuple[Action, ...]
    reason: str
    details: tuple[dict[str, Any], ...]


def check_admissibility(
    checkpoint: StateCheckpoint | None,
    actions: Iterable[Action],
) -> AdmissibilityResult:
    """Check whether ``checkpoint`` is admissible given downstream ``actions``.

    A checkpoint is inadmissible for plain RESUME when any COMPLETED action
    consumed inputs that were produced after the checkpoint. Consumed inputs
    include checkpoint_seq, event_positions, component_ids and prior action_ids.
    Empty consumed_inputs is always admissible and old rows without the field
    remain admissible.

    ``checkpoint`` may be None when restoring without a checkpoint (pure event
    replay); such restores are always admissible. ``actions`` is the full
    ledger fold; only COMPLETED actions are examined, since other statuses
    have not committed downstream work.
    """
    if checkpoint is None:
        return AdmissibilityResult(admissible=True, blocking=(), reason="", details=())
    blocking: list[Action] = []
    details: list[dict[str, Any]] = []
    actions_list = list(actions)
    known_ids: set[str] = set()
    for d in checkpoint.state.decisions:
        known_ids.add(d.decision_id)
    for f in checkpoint.state.findings:
        known_ids.add(f.finding_id)
    for e in checkpoint.state.evidence:
        known_ids.add(e.evidence_id)
    for p in checkpoint.state.plan:
        known_ids.add(p.step_id)
    for w in checkpoint.state.pending_work:
        known_ids.add(w.task_id)
    for dep in checkpoint.state.external_dependencies:
        known_ids.add(dep.resource)
    for pid in checkpoint.state.pins:
        known_ids.add(pid)
    for idx, action in enumerate(actions_list):
        if action.status is not ActionStatus.COMPLETED:
            continue
        ci = action.consumed_inputs
        if ci.checkpoint_seq == 0 and not ci.event_positions and not ci.component_ids and not ci.action_ids:
            continue
        reasons: list[str] = []
        chain_pos = idx + 1
        if ci.checkpoint_seq > checkpoint.version:
            reasons.append(f"checkpoint_seq {ci.checkpoint_seq} after checkpoint version {checkpoint.version}")
        for pos in ci.event_positions:
            if pos > checkpoint.state.source_sequence:
                reasons.append(f"event position {pos} after checkpoint source_sequence {checkpoint.state.source_sequence}")
                break
        if ci.component_ids:
            for cid in ci.component_ids:
                if cid not in known_ids:
                    reasons.append(f"component {cid!r} not in checkpoint (produced after)")
                    break
        if ci.action_ids:
            reasons.append(f"consumed prior action(s) {', '.join(ci.action_ids)}")
        if reasons:
            blocking.append(action)
            details.append(
                {
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "chain_position": chain_pos,
                    "consumed_inputs": ci.model_dump(),
                    "reason": "; ".join(reasons),
                }
            )
    if not blocking:
        return AdmissibilityResult(admissible=True, blocking=(), reason="", details=())
    reason = f"checkpoint v{checkpoint.version} inadmissible: {len(blocking)} blocking commitment(s): " + "; ".join(
        f"{d['action_id'][:12]} at position {d['chain_position']} ({d['reason']})" for d in details[:3]
    )
    return AdmissibilityResult(admissible=False, blocking=tuple(blocking), reason=reason, details=tuple(details))



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

    def __init__(
        self, *, strict_unknown: bool = True, confirmed: bool | Iterable[str] = False
    ) -> None:
        self.strict_unknown = strict_unknown
        # Set when a human has explicitly confirmed the run's self-reported
        # goal/progress (via a REVIEW_CONFIRMED event). Confirmation clears the
        # REQUIRES_REVIEW that self-certified origins would otherwise force, so
        # an externally-driven run can be resumed after a human has eyeballed it.
        # Scoped confirm (issue #394) narrows this to named components only;
        # a boolean True still means both goal and progress, while an iterable
        # names the confirmed subset.
        if isinstance(confirmed, bool):
            self.confirmed: set[str] = {"goal", "progress"} if confirmed else set()
        else:
            # Back-compat: a future caller may pass an iterable directly.
            self.confirmed = set(confirmed)
        # Keep the legacy boolean attribute for any external reader that
        # checks truthiness, but the per-component checks below use the set.
        self._legacy_confirmed = bool(self.confirmed)

    def validate(
        self,
        state: SemanticState,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        checkpoint_environment: EnvironmentSnapshot | None = None,
        checkpoint_version: int = 0,
        expected_model: str | None = None,
        confirmed: bool | Iterable[str] = False,
        scope: Iterable[str] | None = None,
        events: Iterable[Event] | None = None,
    ) -> ValidationOutcome:
        if isinstance(confirmed, bool):
            self.confirmed = {"goal", "progress"} if confirmed else set()
        else:
            self.confirmed = set(confirmed)
        self._legacy_confirmed = bool(self.confirmed)
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
            if events is not None:
                state = self._propagate_caused_by(state, events, entries)
            self._check_goal(state, entries)
            self._check_progress(state, entries)
            self._check_plan(state, entries)
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
            if events is not None:
                state = self._propagate_caused_by(state, events, entries)
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

    def _propagate_caused_by(
        self,
        state: SemanticState,
        events: Iterable[Event],
        entries: list[ComponentValidationEntry],
    ) -> SemanticState:
        """Propagate staleness N hops via caused_by DAG (issue #553)."""
        try:
            from continuum.provenance.graph import build_provenance_graph
        except ImportError:
            return state
        try:
            graph = build_provenance_graph(events)
        except Exception:
            return state
        if not graph.nodes:
            return state
        event_to_component: dict[str, tuple[str, str, object]] = {}
        for ev in state.evidence:
            eid = ev.provenance.source_event_id
            if eid:
                event_to_component[eid] = ("evidence", ev.evidence_id, ev)
        for f in state.findings:
            eid = f.provenance.source_event_id
            if eid:
                event_to_component[eid] = ("finding", f.finding_id, f)
        for d in state.decisions:
            eid = d.provenance.source_event_id
            if eid:
                event_to_component[eid] = ("decision", d.decision_id, d)
        for plan in state.plan:
            eid = plan.provenance.source_event_id
            if eid:
                event_to_component[eid] = ("plan", plan.step_id, plan)
        tainted_events: set[str] = set()
        for entry in entries:
            if entry.status in (
                StateStatus.STALE,
                StateStatus.CONFLICTED,
                StateStatus.INVALID,
                StateStatus.UNKNOWN,
            ):
                for eid, (comp, cid, _) in event_to_component.items():
                    if comp == entry.component.value and cid == entry.component_id:
                        tainted_events.add(eid)
                        break
                if entry.component is Component.EVIDENCE and entry.component_id:
                    for eid, (_comp, cid, _) in event_to_component.items():
                        if cid == entry.component_id:
                            tainted_events.add(eid)
        for ev in state.evidence:
            if ev.status is not StateStatus.VALID and ev.provenance.source_event_id:
                tainted_events.add(ev.provenance.source_event_id)
        for f in state.findings:
            if f.status is not StateStatus.VALID and f.provenance.source_event_id:
                tainted_events.add(f.provenance.source_event_id)
        for d in state.decisions:
            if d.status is not StateStatus.VALID and d.provenance.source_event_id:
                tainted_events.add(d.provenance.source_event_id)
        if not tainted_events:
            return state
        from collections import deque

        dq = deque(tainted_events)
        seen: set[str] = set(tainted_events)
        downstream_events: set[str] = set()
        while dq:
            cur = dq.popleft()
            for child in graph.edges.get(cur, []):
                if child in seen:
                    continue
                seen.add(child)
                downstream_events.add(child)
                dq.append(child)
        has_cycle = False
        visited_dfs: set[str] = set()

        def dfs(node: str, stack: set[str]) -> bool:
            if node in stack:
                return True
            if node in visited_dfs:
                return False
            visited_dfs.add(node)
            stack.add(node)
            for child in graph.edges.get(node, []):
                if (child in downstream_events or child in tainted_events) and dfs(child, stack):
                    return True
            stack.remove(node)
            return False

        for start in tainted_events:
            if dfs(start, set()):
                has_cycle = True
                break
        evidence_by_id = {e.evidence_id: e for e in state.evidence}
        findings_by_id = {f.finding_id: f for f in state.findings}
        decisions_by_id = {d.decision_id: d for d in state.decisions}
        plan_by_id = {p.step_id: p for p in state.plan}
        tainted_downstream: list[tuple[str, str, str]] = []
        for eid in downstream_events:
            if eid not in event_to_component:
                # Handle downstream ACTION_RECORDED not in state (ledger actions)
                node = graph.nodes.get(eid)
                if node and node.type.value == "ACTION_RECORDED":
                    from continuum.events import EventType as _ET

                    if node.type is _ET.ACTION_RECORDED:
                        action_id = (
                            node.payload.get("action_id")
                            or node.payload.get("action", {}).get("action_id")
                            or eid[:8]
                        )
                        # Avoid duplicate if already marked
                        already_action = any(
                            e.component is Component.ACTION
                            and e.component_id == action_id
                            and e.status is not StateStatus.VALID
                            for e in entries
                        )
                        if not already_action:
                            detail = "via caused_by downstream of tainted evidence (N-hop)"
                            # Check for cycle already computed
                            status_to_set_action = (
                                StateStatus.CONFLICTED if has_cycle else StateStatus.STALE
                            )
                            entries.append(
                                ComponentValidationEntry(
                                    component=Component.ACTION,
                                    component_id=action_id,
                                    status=status_to_set_action,
                                    detail=detail,
                                )
                            )
                continue
            comp, cid, obj = event_to_component[eid]
            already = any(
                e.component.value == comp
                and e.component_id == cid
                and e.status is not StateStatus.VALID
                for e in entries
            )
            if already:
                continue
            current_status = obj.status if hasattr(obj, "status") else StateStatus.VALID
            if current_status is not StateStatus.VALID:
                continue
            tainted_downstream.append((eid, comp, cid))
        status_to_set = StateStatus.CONFLICTED if has_cycle else StateStatus.STALE
        new_evidence = list(state.evidence)
        new_findings = list(state.findings)
        new_decisions = list(state.decisions)
        new_plan = list(state.plan)
        for eid, comp, cid in tainted_downstream:
            parents = graph.reverse_edges.get(eid, [])
            parent_str = parents[0][:8] if parents else "unknown"
            detail = f"via caused_by from {parent_str} (N-hop staleness)"
            if has_cycle:
                detail = f"cycle detected via caused_by, downstream of {parent_str}"
            # Map comp to Component
            try:
                comp_enum = Component(comp)
            except ValueError:
                comp_enum = Component.DECISION
            entries.append(
                ComponentValidationEntry(
                    component=comp_enum,
                    component_id=cid,
                    status=status_to_set,
                    detail=detail,
                )
            )
            if comp == "evidence" and cid in evidence_by_id:
                idx = next(i for i, e in enumerate(new_evidence) if e.evidence_id == cid)
                new_evidence[idx] = evidence_by_id[cid].model_copy(update={"status": status_to_set})
            elif comp == "finding" and cid in findings_by_id:
                idx = next(i for i, f in enumerate(new_findings) if f.finding_id == cid)
                new_findings[idx] = findings_by_id[cid].model_copy(update={"status": status_to_set})
            elif comp == "decision" and cid in decisions_by_id:
                idx = next(i for i, d in enumerate(new_decisions) if d.decision_id == cid)
                update: dict[str, object] = {"status": status_to_set}
                if status_to_set is StateStatus.STALE:
                    from continuum.models import utcnow

                    update["invalidated_reason"] = detail
                    update["invalidated_at"] = utcnow()
                new_decisions[idx] = decisions_by_id[cid].model_copy(update=update)
            elif comp == "plan" and cid in plan_by_id:
                idx = next(i for i, p in enumerate(new_plan) if p.step_id == cid)
                import contextlib

                with contextlib.suppress(Exception):
                    new_plan[idx] = plan_by_id[cid].model_copy(update={"status": status_to_set})
        if tainted_downstream:
            return state.model_copy(
                update={
                    "evidence": new_evidence,
                    "findings": new_findings,
                    "decisions": new_decisions,
                    "plan": new_plan,
                }
            )
        return state

    # -- per-component checks --------------------------------------------- #

    def _check_goal(self, state: SemanticState, entries: list[ComponentValidationEntry]) -> None:
        origin = state.goal.provenance.origin
        if origin.self_certified and "goal" not in self.confirmed:
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
        if origin.self_certified and "progress" not in self.confirmed:
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

    def _check_plan(self, state: SemanticState, entries: list[ComponentValidationEntry]) -> None:
        if not state.plan:
            return
        seen: set[str] = set()
        by_id: dict[str, Any] = {}
        for step in state.plan:
            if not step.step_id or not step.step_id.strip():
                entries.append(
                    ComponentValidationEntry(
                        component=Component.PLAN,
                        component_id=step.step_id,
                        status=StateStatus.CONFLICTED,
                        detail="plan unit id must be non-empty",
                    )
                )
                continue
            if step.step_id in seen:
                entries.append(
                    ComponentValidationEntry(
                        component=Component.PLAN,
                        component_id=step.step_id,
                        status=StateStatus.CONFLICTED,
                        detail=f"duplicate plan unit id {step.step_id!r}",
                    )
                )
            seen.add(step.step_id)
            by_id[step.step_id] = step
        for step in state.plan:
            for dep in step.depends_on:
                if dep not in by_id:
                    entries.append(
                        ComponentValidationEntry(
                            component=Component.PLAN,
                            component_id=step.step_id,
                            status=StateStatus.CONFLICTED,
                            detail=f"depends_on {dep!r} not in plan",
                        )
                    )
        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []
        cycle: list[str] | None = None

        def dfs(node: str) -> bool:
            nonlocal cycle
            if node in visited:
                return False
            if node in visiting:
                idx = stack.index(node) if node in stack else 0
                cycle = stack[idx:] + [node]
                return True
            if node not in by_id:
                return False
            visiting.add(node)
            stack.append(node)
            for dep in by_id[node].depends_on:
                if dfs(dep):
                    return True
            stack.pop()
            visiting.remove(node)
            visited.add(node)
            return False

        for sid in list(by_id):
            if sid not in visited and dfs(sid):
                break
        if cycle:
            detail = f"cycle detected: {' -> '.join(cycle)}"
            for sid in cycle:
                entries.append(
                    ComponentValidationEntry(
                        component=Component.PLAN,
                        component_id=sid,
                        status=StateStatus.CONFLICTED,
                        detail=detail,
                    )
                )
        for step in state.plan:
            if step.provenance.origin.self_certified:
                entries.append(
                    ComponentValidationEntry(
                        component=Component.PLAN,
                        component_id=step.step_id,
                        status=StateStatus.REQUIRES_REVIEW,
                        detail=f"plan unit {step.step_id!r} asserted by {step.provenance.origin.value}",
                    )
                )
        stale_ids: set[str] = set()
        for e in entries:
            if (
                e.component is Component.PLAN
                and e.status
                in (
                    StateStatus.STALE,
                    StateStatus.CONFLICTED,
                    StateStatus.REQUIRES_REVIEW,
                    StateStatus.INVALID,
                )
                and e.component_id
            ):
                stale_ids.add(e.component_id)
        changed = True
        while changed:
            changed = False
            for step in state.plan:
                if step.step_id in stale_ids:
                    continue
                for dep in step.depends_on:
                    if dep in stale_ids:
                        if step.step_id not in stale_ids:
                            stale_ids.add(step.step_id)
                            entries.append(
                                ComponentValidationEntry(
                                    component=Component.PLAN,
                                    component_id=step.step_id,
                                    status=StateStatus.STALE,
                                    detail=f"depends on stale plan unit {dep!r}",
                                )
                            )
                            changed = True
                        break

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
    confirmed: bool | Iterable[str] = False,
    scope: Iterable[str] | None = None,
    events: Iterable[Event] | None = None,
) -> ValidationOutcome:
    """Validate a state against the current environment.

    When ``scope`` names specific dependency resources, only those resources are
    re-checked and only their derivation subtree may go stale; the rest of the
    state keeps its recorded status (localized recovery).

    ``confirmed`` may be a boolean (True means both goal and progress) or an
    iterable of component names for scoped confirm (issue #394).
    """
    return StateValidator(strict_unknown=strict_unknown, confirmed=confirmed).validate(
        state,
        current_environment=current_environment,
        checkpoint_environment=checkpoint_environment,
        checkpoint_version=checkpoint_version,
        expected_model=expected_model,
        confirmed=confirmed,
        scope=scope,
        events=events,
    )
