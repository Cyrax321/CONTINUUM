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
from datetime import datetime
from typing import Any, Literal

from continuum.events import Event, EventType
from continuum.models import (
    Approval,
    ApprovalStatus,
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
    """
    return Provenance(
        origin=event.source,
        source_sequence=event.sequence,
        source_event_id=event.event_id,
        extractor="deterministic",
    )


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
        self.model: ModelState | None = base.model if base else None
        self.created_at: datetime | None = base.created_at if base else None
        self.updated_at: datetime | None = base.updated_at if base else None
        self.source_sequence: int = base.source_sequence if base else 0
        self.version: int = base.version if base else 0

    # -- handlers --------------------------------------------------------- #

    def run_started(self, event: Event) -> None:
        self.goal = Goal(
            description=_payload_str(event, "goal"),
            version=int(event.payload.get("goal_version", 1)),
            constraints=_as_str_list(event.payload.get("constraints")),
            provenance=_provenance(event),
        )
        total = event.payload.get("total")
        if total is not None:
            self.progress = Progress(
                total=int(total), pending=int(total), provenance=_provenance(event)
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
                provenance=_provenance(event),
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
            provenance=_provenance(event),
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
            provenance=_provenance(event),
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
            provenance=_provenance(event),
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
            provenance=_provenance(event),
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
            provenance=_provenance(event),
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
