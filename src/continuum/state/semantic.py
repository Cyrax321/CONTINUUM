"""Deterministic projection of an event prefix into semantic state.

``project`` is a pure fold::

    state = reduce(apply, events, empty_state)

Two properties matter more than convenience here, and both are tested:

**Reproducibility.** Folding the same prefix twice yields an equal state
(ignoring wall-clock stamps, which are taken from the events themselves rather
than from ``now()``). If a projection ever depended on the clock, a recovered
state could differ from the original for no reason the operator could see.

**Prefix-closure.** ``project(events[:n])`` equals the state that existed after
event ``n``. Combined with the log's ``trusted_through``, this means a run whose
tail was tampered with can still be recovered up to its last verified event.

Unknown event types are ignored rather than rejected: a newer writer may record
types this reader does not model, and refusing to project would turn a
forward-compatible log into an unrecoverable run. Unknown types are counted and
reported so the loss of fidelity is visible instead of silent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from continuum.events import Event, EventType
from continuum.models import (
    Approval,
    ApprovalStatus,
    AttemptLesson,
    ConstraintPin,
    ConstraintPinned,
    ConstraintRetracted,
    Decision,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    PendingWork,
    Progress,
    Provenance,
    SemanticState,
    StateStatus,
    TrajectoryReport,
    utcnow,
)

__all__ = [
    "ProjectionError",
    "ProjectionReport",
    "first_unprojectable_event",
    "project",
    "project_incremental",
]


class ProjectionError(ValueError):
    """Raised when events cannot be folded into a coherent state."""


@dataclass(slots=True)
class ProjectionReport:
    """What the fold consumed and what it could not interpret."""

    consumed: int = 0
    applied: int = 0
    ignored_types: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.ignored_types


def _payload_str(event: Event, key: str, default: str | None = None) -> str:
    value = event.payload.get(key, default)
    if value is None:
        raise ProjectionError(
            f"event {event.event_id} ({event.type}) is missing required field {key!r}"
        )
    return str(value)


def _provenance(event: Event) -> Provenance:
    """Carry the event's own trust marker into the projected component.

    Previously this hardcoded ``DETERMINISTIC``, which was true of the *fold*
    but said nothing about the event being folded — so an agent's self-report
    projected as indistinguishable from a verified fact.

    For derived artifacts (issue #392) the payload may carry a stamped
    ``derived_origin`` that is the minimum authority of all source events.
    When present, that degraded origin is used, so the projection never
    amplifies weak sources. Missing field degrades to unverified.
    """
    raw_derived = event.payload.get("derived_origin")
    if isinstance(raw_derived, str):
        try:
            from continuum.models import Origin

            origin = Origin(raw_derived)
        except ValueError:
            from continuum.models import Origin

            origin = Origin.EXTERNAL_AGENT
    else:
        origin = event.source
    return Provenance(
        origin=origin,
        source_sequence=event.sequence,
        source_event_id=event.event_id,
        extractor="deterministic",
    )


def _provenance_clamped(event: Event, weakest_seen: Any | None) -> Provenance:
    """Projected provenance that never upgrades trust (issue #294).

    Trust monotonicity: a summary may never assert a fact at higher trust
    than its strongest source. The clamped origin is the minimum over the
    claimed derived origin (when present), the writer's own source, and
    the weakest origin seen in the prefix so far. This is a deterministic
    rule checked in the pure fold, not by an LLM, and it survives
    compaction because weakest_seen is seeded from the checkpoint's
    per-fact origins.
    """
    raw_derived = event.payload.get("derived_origin")
    if isinstance(raw_derived, str):
        try:
            from continuum.models import Origin

            claimed = Origin(raw_derived)
        except ValueError:
            from continuum.models import Origin

            claimed = Origin.EXTERNAL_AGENT
        from continuum.models import Origin
        from continuum.provenance_map import clamp_derived_origin

        weakest = weakest_seen if isinstance(weakest_seen, Origin) else None
        origin = clamp_derived_origin(claimed, event.source, weakest)
    else:
        origin = event.source
    return Provenance(
        origin=origin,
        source_sequence=event.sequence,
        source_event_id=event.event_id,
        extractor="deterministic",
    )


def _weakest_from_state(state: SemanticState | None) -> Any | None:
    """Weakest origin among a state's per-fact provenances, for seeding."""
    if state is None:
        return None
    from continuum.models import Origin

    candidates: list[Origin] = []
    candidates.append(state.goal.provenance.origin)
    candidates.append(state.progress.provenance.origin)
    for p in state.plan:
        candidates.append(p.provenance.origin)
    for d in state.decisions:
        candidates.append(d.provenance.origin)
    for f in state.findings:
        candidates.append(f.provenance.origin)
    for e in state.evidence:
        candidates.append(e.provenance.origin)
    for w in state.pending_work:
        candidates.append(w.provenance.origin)
    for pin in state.pins.values():
        candidates.append(pin.provenance.origin)
    if not candidates:
        return None
    from continuum.provenance_map import derived_origin

    return derived_origin(candidates)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


def _replace(items: list[Any], key: str, identifier: str, updated: Any) -> bool:
    for index, item in enumerate(items):
        if getattr(item, key) == identifier:
            items[index] = updated
            return True
    return False


class _Accumulator:
    """Mutable working set for the fold; converted to a frozen state at the end."""

    def __init__(self, run_id: str, base: SemanticState | None) -> None:
        self.run_id = run_id
        self.goal: Goal | None = base.goal if base else None
        self.progress: Progress = base.progress if base else Progress()
        self.plan = list(base.plan) if base else []
        self.decisions: list[Decision] = list(base.decisions) if base else []
        self.findings: list[Finding] = list(base.findings) if base else []
        self.evidence: list[Evidence] = list(base.evidence) if base else []
        self.pending_work: list[PendingWork] = list(base.pending_work) if base else []
        self.approvals: list[Approval] = list(base.approvals) if base else []
        self.dependencies: list[ExternalDependency] = (
            list(base.external_dependencies) if base else []
        )
        self.pins: dict[str, ConstraintPin] = dict(base.pins) if base else {}
        self.unmatched_pin_retractions: list[str] = (
            list(base.unmatched_pin_retractions) if base else []
        )
        self.attempt_lessons: list[AttemptLesson] = (
            list(base.attempt_lessons) if base and base.attempt_lessons else []
        )
        self.trajectory_reports: list[TrajectoryReport] = (
            list(base.trajectory_reports) if base and base.trajectory_reports else []
        )
        self.model: ModelState | None = base.model if base else None
        self.created_at: datetime | None = base.created_at if base else None
        self.updated_at: datetime | None = base.updated_at if base else None
        self.source_sequence: int = base.source_sequence if base else 0
        self.version: int = base.version if base else 0
        self._weakest_seen = _weakest_from_state(base)

    def _track_weakest(self, event: Event) -> None:

        origin = event.source
        if self._weakest_seen is None:
            self._weakest_seen = origin
        else:
            from continuum.provenance_map import derived_origin

            self._weakest_seen = derived_origin([self._weakest_seen, origin])

    def _provenance_for(self, event: Event) -> Provenance:
        return _provenance_clamped(event, self._weakest_seen)

    # -- handlers --------------------------------------------------------- #

    def run_started(self, event: Event) -> None:
        self.goal = Goal(
            description=_payload_str(event, "goal"),
            version=int(event.payload.get("goal_version", 1)),
            constraints=_as_str_list(event.payload.get("constraints")),
            provenance=self._provenance_for(event),
        )
        total = event.payload.get("total")
        if total is not None:
            self.progress = Progress(
                total=int(total), pending=int(total), provenance=self._provenance_for(event)
            )
        self.created_at = event.timestamp

    def task_updated(self, event: Event) -> None:
        if self.goal is None:
            raise ProjectionError(
                f"event {event.event_id}: TASK_UPDATED before the run was started"
            )
        description = event.payload.get("goal")
        if description is not None:
            self.goal = Goal(
                description=str(description),
                version=int(event.payload.get("goal_version", self.goal.version + 1)),
                constraints=_as_str_list(event.payload.get("constraints"))
                or list(self.goal.constraints),
                provenance=self._provenance_for(event),
            )
        self._apply_progress(event.payload, event)

    def _apply_progress(self, payload: Mapping[str, Any], event: Event) -> None:
        keys = ("total", "completed", "pending", "failed")
        if not any(key in payload for key in keys):
            return
        current = {k: getattr(self.progress, k) for k in keys}
        for key in keys:
            if key in payload:
                value = payload[key]
                current[key] = None if value is None else int(value)

        # Re-derive `pending` whenever the caller moved `completed`/`failed`
        # without restating it. Keeping the old value would leave the counters
        # summing past `total` and the update would be rejected — punishing a
        # caller for omitting a field that had not changed.
        total = current["total"]
        if total is not None and "pending" not in payload:
            done = (current["completed"] or 0) + (current["failed"] or 0)
            current["pending"] = max(total - done, 0)
        # Progress is cumulative, so the weakest contributor wins: once an
        # agent self-reports into the figure, the total stays self-certified
        # until something re-derives it from scratch.
        origin = event.source if event.source.self_certified else self.progress.provenance.origin
        self.progress = Progress(
            **current,
            provenance=Provenance(
                origin=origin,
                source_sequence=event.sequence,
                source_event_id=event.event_id,
                extractor="deterministic",
            ),
        )

    def work_completed(self, event: Event) -> None:
        """Advance progress by one unit and close the matching pending task."""
        count = int(event.payload.get("count", 1))
        failed = bool(event.payload.get("failed", False))
        completed = self.progress.completed + (0 if failed else count)
        failures = self.progress.failed + (count if failed else 0)
        pending = max(self.progress.pending - count, 0)
        origin = event.source if event.source.self_certified else self.progress.provenance.origin
        self.progress = Progress(
            total=self.progress.total,
            completed=completed,
            pending=pending,
            failed=failures,
            provenance=Provenance(
                origin=origin,
                source_sequence=event.sequence,
                source_event_id=event.event_id,
                extractor="deterministic",
            ),
        )
        task_id = event.payload.get("task_id")
        if task_id is not None:
            self.pending_work = [w for w in self.pending_work if w.task_id != str(task_id)]

    def decision_created(self, event: Event) -> None:
        decision = Decision(
            decision_id=_payload_str(event, "decision_id"),
            decision=_payload_str(event, "decision"),
            reason=str(event.payload.get("reason", "")),
            evidence=_as_str_list(event.payload.get("evidence")),
            created_at=event.timestamp,
            provenance=self._provenance_for(event),
        )
        if not _replace(self.decisions, "decision_id", decision.decision_id, decision):
            self.decisions.append(decision)

    def decision_invalidated(self, event: Event) -> None:
        decision_id = _payload_str(event, "decision_id")
        existing = next((d for d in self.decisions if d.decision_id == decision_id), None)
        if existing is None:
            raise ProjectionError(
                f"event {event.event_id}: cannot invalidate unknown decision {decision_id!r}"
            )
        status = StateStatus(event.payload.get("status", StateStatus.INVALID))
        _replace(
            self.decisions,
            "decision_id",
            decision_id,
            existing.model_copy(
                update={
                    "status": status,
                    "invalidated_at": event.timestamp,
                    "invalidated_reason": str(event.payload.get("reason", "")) or None,
                }
            ),
        )

    def evidence_added(self, event: Event) -> None:
        item = Evidence(
            evidence_id=_payload_str(event, "evidence_id"),
            summary=str(event.payload.get("summary", "")),
            source=(
                str(event.payload["source"]) if event.payload.get("source") is not None else None
            ),
            checksum=(
                str(event.payload["checksum"])
                if event.payload.get("checksum") is not None
                else None
            ),
            added_at=event.timestamp,
            provenance=self._provenance_for(event),
        )
        if not _replace(self.evidence, "evidence_id", item.evidence_id, item):
            self.evidence.append(item)

    def finding_added(self, event: Event) -> None:
        finding = Finding(
            finding_id=_payload_str(event, "finding_id"),
            claim=_payload_str(event, "claim"),
            evidence=_as_str_list(event.payload.get("evidence")),
            confidence=float(event.payload.get("confidence", 1.0)),
            created_at=event.timestamp,
            provenance=self._provenance_for(event),
        )
        if not _replace(self.findings, "finding_id", finding.finding_id, finding):
            self.findings.append(finding)

    def finding_invalidated(self, event: Event) -> None:
        finding_id = _payload_str(event, "finding_id")
        existing = next((f for f in self.findings if f.finding_id == finding_id), None)
        if existing is None:
            raise ProjectionError(
                f"event {event.event_id}: cannot invalidate unknown finding {finding_id!r}"
            )
        status = StateStatus(event.payload.get("status", StateStatus.INVALID))
        _replace(
            self.findings, "finding_id", finding_id, existing.model_copy(update={"status": status})
        )

    def work_added(self, event: Event) -> None:
        work = PendingWork(
            task_id=_payload_str(event, "task_id"),
            description=_payload_str(event, "description"),
            prerequisite=_as_str_list(event.payload.get("prerequisite")),
            created_at=event.timestamp,
            provenance=self._provenance_for(event),
        )
        if not _replace(self.pending_work, "task_id", work.task_id, work):
            self.pending_work.append(work)

    def dependency_declared(self, event: Event) -> None:
        dependency = ExternalDependency(
            resource=_payload_str(event, "resource"),
            kind=str(event.payload.get("kind", "resource")),
            version=(
                str(event.payload["version"]) if event.payload.get("version") is not None else None
            ),
            checksum=(
                str(event.payload["checksum"])
                if event.payload.get("checksum") is not None
                else None
            ),
            last_verified_at=event.timestamp,
            provenance=self._provenance_for(event),
        )
        if not _replace(self.dependencies, "resource", dependency.resource, dependency):
            self.dependencies.append(dependency)

    def approval_requested(self, event: Event) -> None:
        approval = Approval(
            approval_id=_payload_str(event, "approval_id"),
            subject=_payload_str(event, "subject"),
            status=ApprovalStatus.PENDING,
        )
        if not _replace(self.approvals, "approval_id", approval.approval_id, approval):
            self.approvals.append(approval)

    def approval_resolved(self, event: Event, status: ApprovalStatus) -> None:
        approval_id = _payload_str(event, "approval_id")
        existing = next((a for a in self.approvals if a.approval_id == approval_id), None)
        if existing is None:
            existing = Approval(
                approval_id=approval_id,
                subject=str(event.payload.get("subject", "")),
            )
            self.approvals.append(existing)
        expires_raw = event.payload.get("expires_at")
        updated = existing.model_copy(
            update={
                "status": status,
                "granted_at": event.timestamp if status is ApprovalStatus.GRANTED else None,
                "granted_by": (
                    str(event.payload["granted_by"])
                    if event.payload.get("granted_by") is not None
                    else None
                ),
                "expires_at": datetime.fromisoformat(str(expires_raw)) if expires_raw else None,
                "reason": (
                    str(event.payload["reason"])
                    if event.payload.get("reason") is not None
                    else None
                ),
            }
        )
        _replace(self.approvals, "approval_id", approval_id, updated)

    def model_changed(self, event: Event) -> None:
        previous = self.model
        self.model = ModelState(
            model=str(event.payload.get("model")) if event.payload.get("model") else None,
            provider=str(event.payload.get("provider")) if event.payload.get("provider") else None,
            fingerprint=(
                str(event.payload.get("fingerprint")) if event.payload.get("fingerprint") else None
            ),
            model_specific_state=list(previous.model_specific_state) if previous else [],
        )

    def model_assumption_recorded(self, event: Event) -> None:
        assumption = ModelSpecificState(
            item_id=_payload_str(event, "item_id"),
            description=_payload_str(event, "description"),
        )
        current = self.model or ModelState()
        assumptions = [a for a in current.model_specific_state if a.item_id != assumption.item_id]
        assumptions.append(assumption)
        self.model = current.model_copy(update={"model_specific_state": assumptions})

    def constraint_pinned(self, event: Event) -> None:
        payload = ConstraintPinned.model_validate(event.payload)
        pin = ConstraintPin(
            constraint_id=payload.constraint_id,
            sha256=payload.sha256,
            status="active",
            provenance=self._provenance_for(event),
            pinned_at=event.timestamp,
        )
        self.pins[payload.constraint_id] = pin

    def constraint_retracted(self, event: Event) -> None:
        payload = ConstraintRetracted.model_validate(event.payload)
        constraint_id = payload.constraint_id
        if constraint_id in self.pins:
            del self.pins[constraint_id]
        else:
            if constraint_id not in self.unmatched_pin_retractions:
                self.unmatched_pin_retractions.append(constraint_id)

    def attempt_lesson(self, event: Event) -> None:
        lesson = AttemptLesson.model_validate(event.payload)
        if any(existing.attempt_id == lesson.attempt_id for existing in self.attempt_lessons):
            return
        self.attempt_lessons.append(lesson)
        self.attempt_lessons.sort(key=lambda existing: existing.created_at)

    def trajectory_report(self, event: Event) -> None:
        report = TrajectoryReport.model_validate(event.payload)
        if any(existing.report_id == report.report_id for existing in self.trajectory_reports):
            return
        if any(existing.window_end == report.window_end for existing in self.trajectory_reports):
            return
        self.trajectory_reports.append(report)
        self.trajectory_reports.sort(key=lambda existing: existing.window_end)

    def plan_upsert(self, event: Event) -> None:
        payload = event.payload
        plan_id = payload.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ProjectionError(f"event {event.event_id}: PLAN_UPSERT missing non-empty plan_id")
        units = payload.get("units")
        if not isinstance(units, list):
            raise ProjectionError(f"event {event.event_id}: PLAN_UPSERT units must be a list")
        seen: set[str] = set()
        status_map = {
            "pending": "pending",
            "working": "in_progress",
            "done": "completed",
            "blocked": "blocked",
        }
        by_id: dict[str, Any] = {p.step_id: p for p in self.plan}
        for raw in units:
            if not isinstance(raw, dict):
                raise ProjectionError(f"event {event.event_id}: PLAN_UPSERT unit must be an object")
            unit_id = raw.get("id")
            if not isinstance(unit_id, str) or not unit_id.strip():
                raise ProjectionError(
                    f"event {event.event_id}: PLAN_UPSERT unit id must be non-empty"
                )
            if unit_id in seen:
                raise ProjectionError(
                    f"event {event.event_id}: duplicate unit id {unit_id!r} in same PLAN_UPSERT"
                )
            seen.add(unit_id)
            title = raw.get("title")
            if not isinstance(title, str):
                raise ProjectionError(
                    f"event {event.event_id}: unit {unit_id!r} title must be a string"
                )
            raw_status = raw.get("status", "pending")
            if not isinstance(raw_status, str) or raw_status not in status_map:
                raise ProjectionError(
                    f"event {event.event_id}: unit {unit_id!r} status must be one of {sorted(status_map)}"
                )
            status = status_map[raw_status]
            depends = raw.get("depends_on", [])
            if not isinstance(depends, list):
                raise ProjectionError(
                    f"event {event.event_id}: unit {unit_id!r} depends_on must be a list"
                )
            depends_list = [str(d) for d in depends]
            from continuum.models import PlanStep, PlanStepStatus

            step = PlanStep(
                step_id=unit_id,
                description=title,
                status=PlanStepStatus(status),
                depends_on=depends_list,
                provenance=self._provenance_for(event),
            )
            by_id[unit_id] = step
        self.plan = sorted(by_id.values(), key=lambda p: p.step_id)

    # -- finish ----------------------------------------------------------- #

    def build(self) -> SemanticState:
        return self._build()

    def build_degraded(self, event: Event, reason: str) -> SemanticState:
        """The last-good prefix, marked INVALID and naming where folding stopped.

        Shares ``build``'s guards deliberately: if even the prefix cannot
        produce a state (no RUN_STARTED before the break), there is nothing
        known to report, and degrade mode raises like raise mode. A degraded
        state that named no break would be worse than an error.
        """
        state = self._build()
        return state.model_copy(
            update={
                "status": StateStatus.INVALID,
                "unprojectable_at_sequence": event.sequence,
                "unprojectable_event_type": str(event.type),
                "unprojectable_reason": _condense(reason),
            }
        )

    def _build(
        self,
        *,
        status: StateStatus = StateStatus.VALID,
        unprojectable_at_sequence: int | None = None,
        unprojectable_event_type: str | None = None,
        unprojectable_reason: str | None = None,
    ) -> SemanticState:
        if self.goal is None:
            raise ProjectionError(
                f"run {self.run_id!r} has no goal: the log never recorded RUN_STARTED"
            )
        stamp = self.updated_at or self.created_at
        if stamp is None:  # pragma: no cover - every sealed event carries a timestamp
            raise ProjectionError(f"run {self.run_id!r} has no timestamped events")
        return SemanticState(
            run_id=self.run_id,
            goal=self.goal,
            progress=self.progress,
            plan=self.plan,
            decisions=self.decisions,
            findings=self.findings,
            evidence=self.evidence,
            pending_work=self.pending_work,
            approvals=self.approvals,
            external_dependencies=self.dependencies,
            pins=dict(self.pins),
            unmatched_pin_retractions=list(self.unmatched_pin_retractions),
            attempt_lessons=list(self.attempt_lessons),
            trajectory_reports=list(self.trajectory_reports),
            model=self.model,
            version=self.version,
            source_sequence=self.source_sequence,
            status=status,
            unprojectable_at_sequence=unprojectable_at_sequence,
            unprojectable_event_type=unprojectable_event_type,
            unprojectable_reason=unprojectable_reason,
            created_at=self.created_at or stamp,
            updated_at=stamp,
        )


def _dispatch(acc: _Accumulator, event: Event) -> bool:
    """Apply one event. Returns False when the type carries no state change."""
    match event.type:
        case EventType.RUN_STARTED:
            acc.run_started(event)
        case EventType.TASK_UPDATED:
            acc.task_updated(event)
        case EventType.WORK_COMPLETED:
            acc.work_completed(event)
        case EventType.WORK_ADDED:
            acc.work_added(event)
        case EventType.DECISION_CREATED:
            acc.decision_created(event)
        case EventType.DECISION_INVALIDATED:
            acc.decision_invalidated(event)
        case EventType.EVIDENCE_ADDED:
            acc.evidence_added(event)
        case EventType.FINDING_ADDED:
            acc.finding_added(event)
        case EventType.FINDING_INVALIDATED:
            acc.finding_invalidated(event)
        case EventType.DEPENDENCY_DECLARED:
            acc.dependency_declared(event)
        case EventType.APPROVAL_REQUESTED:
            acc.approval_requested(event)
        case EventType.APPROVAL_GRANTED:
            acc.approval_resolved(event, ApprovalStatus.GRANTED)
        case EventType.APPROVAL_REVOKED:
            acc.approval_resolved(event, ApprovalStatus.REVOKED)
        case EventType.MODEL_CHANGED:
            acc.model_changed(event)
        case EventType.MODEL_ASSUMPTION_RECORDED:
            acc.model_assumption_recorded(event)
        case EventType.CONSTRAINT_PINNED:
            acc.constraint_pinned(event)
        case EventType.CONSTRAINT_RETRACTED:
            acc.constraint_retracted(event)
        case EventType.ATTEMPT_LESSON:
            acc.attempt_lesson(event)
        case EventType.TRAJECTORY_REPORT:
            acc.trajectory_report(event)
        case EventType.PLAN_UPSERT:
            acc.plan_upsert(event)
        case _:
            return False
    return True


_NON_PROJECTING = frozenset(
    {
        EventType.RUN_COMPLETED,
        EventType.RUN_ABORTED,
        EventType.RUN_FORKED,
        EventType.TOOL_CALLED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_FAILED,
        EventType.STATE_CHECKPOINTED,
        EventType.STATE_VALIDATED,
        EventType.ENVIRONMENT_CHANGED,
        EventType.RECOVERY_STARTED,
        EventType.RECOVERY_COMPLETED,
        EventType.RECOVERY_BLOCKED,
        EventType.ACTION_RECORDED,
        EventType.ACTION_RECONCILED,
        EventType.ACTION_COMPENSATED,
        # authority (issue #269): refusal audit, never state
        EventType.GRANT_DENIED,
        # log maintenance (issue #239): records the compaction boundary itself
        EventType.EVENT_LOG_ANCHORED,
        # liveness (issue #302): silence detection and recovery, never state
        EventType.LIVENESS_SILENCE_DETECTED,
        EventType.LIVENESS_RECOVERED,
    }
)


def project_incremental(
    run_id: str,
    events: Iterable[Event],
    *,
    base: SemanticState | None = None,
    on_unprojectable: Literal["raise", "degrade"] = "raise",
) -> tuple[SemanticState, ProjectionReport]:
    """Fold ``events`` onto ``base`` and report what was consumed.

    Passing a ``base`` lets a long run advance its state without re-reading the
    whole log; the result must equal a full re-projection of the same prefix.

    ``on_unprojectable="degrade"`` (issue #383) stops at the earliest event the
    fold refuses and returns the last-good prefix marked ``INVALID``, naming the
    sequence, event type and condensed reason. It never skips the bad event and
    keeps going: the state after a skipped write is not a state the run was ever
    in, so folding resumes nowhere. The default stays ``"raise"`` so every
    existing caller keeps getting exactly today's behaviour; a silent partial
    state reads as authoritative, which is worse than a crash. Note that
    ``report.consumed`` counts the refused event too, since it is incremented
    before the fold is attempted; ``consumed - applied`` therefore includes it.

    The run-mismatch and sequence-order checks above deliberately stay outside
    that treatment: they mean the caller handed the fold a malformed stream, not
    that the log contains an event its model rejects.
    """
    if on_unprojectable not in ("raise", "degrade"):
        raise ValueError(f"on_unprojectable must be 'raise' or 'degrade', got {on_unprojectable!r}")
    acc = _Accumulator(run_id, base)
    report = ProjectionReport()
    previous_sequence = base.source_sequence if base else 0

    for event in events:
        if event.run_id != run_id:
            raise ProjectionError(
                f"event {event.event_id} belongs to run {event.run_id!r}, not {run_id!r}"
            )
        if event.sequence <= previous_sequence:
            raise ProjectionError(
                f"event {event.event_id}: sequence {event.sequence} is not after {previous_sequence}"
            )
        previous_sequence = event.sequence
        report.consumed += 1

        try:
            if _dispatch(acc, event):
                report.applied += 1
                acc.updated_at = event.timestamp
            elif event.type not in _NON_PROJECTING:
                name = str(event.type)
                report.ignored_types[name] = report.ignored_types.get(name, 0) + 1
        except Exception as exc:
            # Deliberately broad, same reasoning as first_unprojectable_event:
            # any failure here means the log stops folding at this event, no
            # matter whether it came from a model invariant or a payload shape
            # nothing anticipated. Narrowing the catch would trade a named
            # diagnosis for an opaque traceback precisely on the malformed
            # logs this mode exists to answer.
            if on_unprojectable != "degrade":
                raise
            return acc.build_degraded(event, str(exc)), report

        acc._track_weakest(event)
        acc.source_sequence = event.sequence
        if acc.created_at is None:
            acc.created_at = event.timestamp

    return acc.build(), report


def _condense(reason: str) -> str:
    """One readable line from a possibly multi-line validation error.

    pydantic renders "1 validation error for Progress" as its first line and puts
    the constraint that actually failed on the second, followed by a docs URL. So
    the naive first line is the least informative part. This keeps the header only
    when there is nothing better, and drops the machine-facing bracket detail and
    the URL either way.
    """
    lines = [line.strip() for line in reason.splitlines() if line.strip()]
    if not lines:
        return "unknown projection failure"
    informative = next(
        (line for line in lines[1:] if not line.startswith("For further information")),
        lines[0],
    )
    return informative.split(" [type=")[0].rstrip()


def first_unprojectable_event(
    run_id: str,
    events: Iterable[Event],
) -> tuple[int, str, str] | None:
    """Locate the earliest event whose fold fails, or ``None`` if the log folds.

    Returns ``(sequence, event_type, reason)`` with ``reason`` condensed to a
    single line. Naming the event is what turns "this run cannot be projected"
    into something an operator can act on: the raw message reports the folded
    figures without saying which write produced them, and the log may be thousands
    of events long (issue #382).

    Folds one event at a time carrying the state forward through
    ``project_incremental``'s ``base``, so this is a single linear pass rather
    than a re-projection per prefix. That matters because the logs most likely to
    need this are the long ones.

    Deliberately catches broadly. The question is *where* the log stops folding,
    and any exception means it stopped there, whether it came from a model
    invariant, the projector's own ordering checks, or a payload shape nothing
    anticipated. Re-raising a narrower set would leave the operator holding the
    same opaque traceback this exists to replace.
    """
    state: SemanticState | None = None
    for event in sorted(events, key=lambda e: e.sequence):
        try:
            state, _ = project_incremental(run_id, [event], base=state)
        except Exception as exc:  # noqa: BLE001 - see docstring
            return event.sequence, str(event.type), _condense(str(exc))
    return None


def project(
    run_id: str,
    events: Iterable[Event],
    *,
    upto: int | None = None,
    on_unprojectable: Literal["raise", "degrade"] = "raise",
) -> SemanticState:
    """Project a run's events into semantic state.

    ``upto`` truncates the fold at a sequence number — the mechanism behind
    ``continuum inspect --version`` and recovery from a partially trusted log.

    ``on_unprojectable`` forwards to :func:`project_incremental`: ``"raise"``
    (the default) preserves today's behaviour for every existing caller;
    ``"degrade"`` returns the last-good prefix marked INVALID instead of
    raising, for callers whose job is to diagnose a log rather than fold it.
    """
    selected = [e for e in events if upto is None or e.sequence <= upto]
    state, _ = project_incremental(run_id, selected, on_unprojectable=on_unprojectable)
    return state


# --------------------------------------------------------------------------- #
# Reconstruction accounting per pin (issue #418)
# --------------------------------------------------------------------------- #

# Marker format for pins in reconstructed context.
# Each active pin emits a hash-tagged marker like:
#   [pin:constraint_id:abc12345]
# where abc12345 is the first 8 chars of the sha256. The marker is the
# source of truth for accounting, not a summarizer's self-report.
_PIN_MARKER_PREFIX = "[pin:"
_PIN_MARKER_SUFFIX = "]"


def _pin_marker(pin: ConstraintPin) -> str:
    """Hash-tagged marker for a pin, emitted by reconstruction."""
    return f"{_PIN_MARKER_PREFIX}{pin.constraint_id}:{pin.sha256[:8]}{_PIN_MARKER_SUFFIX}"


def _pin_marker_for_id(constraint_id: str, sha256: str) -> str:
    """Marker for a given id and full sha256."""
    return f"{_PIN_MARKER_PREFIX}{constraint_id}:{sha256[:8]}{_PIN_MARKER_SUFFIX}"


def account_pins_in_context(
    state: SemanticState,
    context: str,
    *,
    grace_seconds: int | None = None,
    now: datetime | None = None,
    strict: bool = False,
) -> dict[str, dict[str, Any]]:
    """Classify each active pin as present, absent or unverifiable.

    The classification is computed from what the produced ``context`` actually
    contains, not from a summarizer's claim. Each active pin must have emitted
    a hash-tagged marker during reconstruction; the marker is the evidence.

    - present: marker found in context
    - absent: marker not found, and pin not in a truncated/dropped section
    - unverifiable: marker not found but context was truncated and the pin
      would have been in a dropped section (so we cannot tell)

    Grace window: if a pin is absent and the time since ``pinned_at`` exceeds
    ``grace_seconds``, it is flagged. In strict mode the flag escalates to
    ``REQUIRES_REVIEW``; otherwise it is advisory.

    Returns a dict mapping constraint_id to a dict with:
    - status: "present" | "absent" | "unverifiable"
    - sha256: full digest
    - sha256_prefix: first 8 chars
    - pinned_at: timestamp
    - age_seconds: seconds since pinned_at
    - past_grace: bool
    - flag: advisory or strict flag if past grace and absent
    """

    if now is None:
        now = utcnow()

    result: dict[str, dict[str, Any]] = {}
    # Check if context indicates truncation (for unverifiable)
    is_truncated = "[context truncated" in context or "omitted:" in context

    for pin_id, pin in state.pins.items():
        marker = _pin_marker(pin)
        present = marker in context
        age_seconds = (now - pin.pinned_at).total_seconds() if pin.pinned_at else 0
        past_grace = grace_seconds is not None and age_seconds > grace_seconds

        if present:
            status = "present"
            flag = None
        else:
            # If context was truncated, we cannot tell if the pin was in a
            # dropped section — mark as unverifiable rather than absent
            if is_truncated:
                # Heuristic: if the pin's marker would have been in a low-
                # priority section that was dropped, mark unverifiable
                # For now, we treat any absent with truncation as unverifiable
                # unless the pin is in the never-dropped set (goal, etc.)
                # Since pins are not in the never-dropped set, they are unverifiable
                status = "unverifiable"
                flag = None
                if past_grace:
                    # Even unverifiable past grace is flagged, but not as absent
                    flag = f"pin {pin_id}:{pin.sha256[:8]} unverifiable past grace ({int(age_seconds)}s > {grace_seconds}s)"
            else:
                status = "absent"
                flag = None
                if past_grace:
                    flag = f"pin {pin_id}:{pin.sha256[:8]} absent past grace ({int(age_seconds)}s > {grace_seconds}s)"
                    if strict:
                        # Strict escalation will be handled by caller
                        pass

        result[pin_id] = {
            "status": status,
            "sha256": pin.sha256,
            "sha256_prefix": pin.sha256[:8],
            "pinned_at": pin.pinned_at,
            "age_seconds": age_seconds,
            "past_grace": past_grace,
            "flag": flag,
            "marker": marker,
        }

    return result


def pin_markers_for_state(state: SemanticState) -> list[str]:
    """Emit hash-tagged markers for each active pin in the state."""
    return [_pin_marker(pin) for pin in state.pins.values()]


def check_pin_accounting(
    state: SemanticState,
    context: str,
    *,
    grace_seconds: int | None = None,
    now: datetime | None = None,
    strict: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    """Accounting wrapper that also determines if strict escalation is needed.

    Returns (accounting, flags, should_escalate) where:
    - accounting is the per-pin status dict
    - flags is a list of advisory/strict flag strings
    - should_escalate is True if strict and any absent past grace
    """
    accounting = account_pins_in_context(
        state, context, grace_seconds=grace_seconds, now=now, strict=strict
    )
    flags: list[str] = []
    should_escalate = False
    for _pin_id, info in accounting.items():
        if info["flag"]:
            flags.append(info["flag"])
            if info["status"] == "absent" and info["past_grace"] and strict:
                should_escalate = True
    return accounting, flags, should_escalate


def constraint_pins_payload(
    state: SemanticState,
    context: str,
    *,
    grace_seconds: int | None = None,
    now: datetime | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Build the JSON block surfaced in resume and validate responses.

    Read-only display over the accounting output (issue #419). No gating
    logic lives here; strict escalation is decided in #418 and reused
    only to compute per-pin deadline and past_grace, not to change mode.
    The caller supplies the already rendered recovery context, so this
    function never rebuilds markers itself, it only classifies what the
    context actually contains.
    """
    accounting = account_pins_in_context(
        state, context, grace_seconds=grace_seconds, now=now, strict=strict
    )
    pins: dict[str, dict[str, Any]] = {}
    flagged: list[str] = []
    for pin_id in sorted(accounting.keys()):
        info = accounting[pin_id]
        pinned_at = info["pinned_at"]
        grace_deadline = None
        if pinned_at is not None and grace_seconds is not None:
            try:
                deadline = pinned_at + timedelta(seconds=grace_seconds)
                grace_deadline = deadline.isoformat()
            except Exception:
                grace_deadline = None
        pinned_at_iso = pinned_at.isoformat() if pinned_at is not None else None
        pins[pin_id] = {
            "status": info["status"],
            "sha256": info["sha256"],
            "sha256_prefix": info["sha256_prefix"],
            "pinned_at": pinned_at_iso,
            "grace_deadline": grace_deadline,
            "past_grace": bool(info["past_grace"]),
            "flag": info["flag"],
        }
        if info["status"] != "present":
            flagged.append(pin_id)
    return {
        "pins": pins,
        "flagged": sorted(flagged),
        "grace_seconds": grace_seconds,
    }
