"""Append-only event log.

The event log is the source of truth for a run. Semantic state, checkpoints and
the action ledger are all *projections* of this log, which means recovery can
always be re-derived and independently audited.

Integrity model
---------------
Events form a per-run hash chain::

    e1.prev_hash = None          e1.hash = H(content(e1))
    e2.prev_hash = e1.hash       e2.hash = H(content(e2))
    ...

``EventLog.verify()`` recomputes every digest and re-walks the chain, so a
persisted log that was edited out-of-band is detectable. This is tamper
*evidence*, not tamper *proofing*: an attacker who can rewrite the whole log can
recompute the chain. Signing is out of scope for Phase 1 and documented as such.

Ordering guarantees are per run, not global: sequence numbers start at 1 and
increase by exactly 1 within a ``run_id``. No cross-run ordering is implied.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from continuum.models import Origin, utcnow
from continuum.security.hashing import make_id, stable_hash

__all__ = [
    "EventType",
    "Event",
    "EventLog",
    "IntegrityViolation",
    "IntegrityReport",
    "AppendOnlyViolation",
]


class EventType(StrEnum):
    """The recorded vocabulary of a run.

    Types that mutate semantic state are folded by ``continuum.state.project``;
    the rest are recorded facts (audit trail, ledger, recovery history) that
    leave the projection unchanged.
    """

    # lifecycle
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_ABORTED = "RUN_ABORTED"
    TASK_UPDATED = "TASK_UPDATED"

    # lineage (issue #259): a divergent continuation was approved off this run
    RUN_FORKED = "RUN_FORKED"
    RUN_RESTORED = "RUN_RESTORED"
    RUN_MERGED = "RUN_MERGED"

    # tools
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"

    # semantic state
    # DECISION_CREATED payload may include caused_by: list[str] (1-128 chars each, max 32,
    # default []). Unknown ids raise ValueError. Field is hash-covered and old events
    # without it load as [].
    DECISION_CREATED = "DECISION_CREATED"
    DECISION_INVALIDATED = "DECISION_INVALIDATED"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    FINDING_ADDED = "FINDING_ADDED"
    FINDING_INVALIDATED = "FINDING_INVALIDATED"
    WORK_ADDED = "WORK_ADDED"
    WORK_COMPLETED = "WORK_COMPLETED"
    DEPENDENCY_DECLARED = "DEPENDENCY_DECLARED"

    # constraints (issue #416): first-class pins carrying hashes, never text
    CONSTRAINT_PINNED = "CONSTRAINT_PINNED"
    CONSTRAINT_RETRACTED = "CONSTRAINT_RETRACTED"

    # approvals
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"

    # model identity
    MODEL_CHANGED = "MODEL_CHANGED"
    MODEL_ASSUMPTION_RECORDED = "MODEL_ASSUMPTION_RECORDED"

    # checkpoints, environment, recovery
    STATE_CHECKPOINTED = "STATE_CHECKPOINTED"
    STATE_VALIDATED = "STATE_VALIDATED"
    ENVIRONMENT_CHANGED = "ENVIRONMENT_CHANGED"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
    RECOVERY_BLOCKED = "RECOVERY_BLOCKED"
    REVIEW_CONFIRMED = "REVIEW_CONFIRMED"

    # agent cognition (issue #235): compact self-authored plan state
    REASONING_SUMMARY = "REASONING_SUMMARY"

    # log maintenance (issue #239): marks the boundary of an archived prefix
    EVENT_LOG_ANCHORED = "EVENT_LOG_ANCHORED"

    # perception and planning (security extension)
    PERCEPTION_OBSERVED = "PERCEPTION_OBSERVED"
    BRANCH_RESOLVED = "BRANCH_RESOLVED"

    # action ledger
    # ACTION_RECORDED payload may include caused_by: list[str] (1-128 chars each, max 32,
    # default []). Unknown ids raise ValueError. Hash-covered, old events load as [].
    ACTION_RECORDED = "ACTION_RECORDED"
    ACTION_RECONCILED = "ACTION_RECONCILED"
    ACTION_COMPENSATED = "ACTION_COMPENSATED"

    # authority (issue #269): a claim tried to reuse a consumed single-use grant
    GRANT_DENIED = "GRANT_DENIED"

    # authority lifecycle (issue #289/#555): one-time credential was consumed
    AUTHORITY_CONSUMED = "AUTHORITY_CONSUMED"

    # structured attempt memory (issue #313): durable falsification lesson
    ATTEMPT_LESSON = "ATTEMPT_LESSON"

    # sleep-time trajectory reports (issue #393): distilled from archived history
    TRAJECTORY_REPORT = "TRAJECTORY_REPORT"

    # structured plan (issue #312): durable milestones for long-horizon recovery
    PLAN_UPSERT = "PLAN_UPSERT"


class AppendOnlyViolation(RuntimeError):
    """Raised when an operation would rewrite history."""


def _json_native(value: Any, *, path: str = "payload") -> Any:
    """Reject payload values that would not survive a storage round-trip.

    An event's identity is its hash. If a payload holds a ``datetime``, it
    hashes one way in memory and another way after being read back as a string,
    which would make a perfectly valid event fail reload. Rather than allow that
    to surface later as phantom corruption, payloads are constrained to
    JSON-native types at construction and the caller converts explicitly.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"{path}: keys must be strings, got {type(key).__name__} ({key!r})"
                )
            result[key] = _json_native(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_native(item, path=f"{path}[{i}]") for i, item in enumerate(value)]
    raise ValueError(
        f"{path}: {type(value).__name__} is not JSON-native and would not survive "
        f"a storage round-trip; convert it first (e.g. datetime -> isoformat())"
    )


class Event(BaseModel):
    """An immutable, hash-chained fact about a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: make_id("event"))
    run_id: str
    sequence: int
    type: EventType
    timestamp: datetime = Field(default_factory=utcnow)
    payload: Mapping[str, Any] = Field(default_factory=dict)
    causer_event_id: str | None = None
    source: Origin = Origin.DETERMINISTIC
    """Who asserted this fact. Captured at write time and signed.

    Included in ``content()`` deliberately. A trust marker outside the hash
    could be edited without breaking verification, which would make it useless
    for the one job it has. The cost is that chains written before this field
    existed no longer verify — accepted as a clean break rather than carrying a
    permanently-untrusted legacy tier.
    """

    prev_hash: str | None = None
    hash: str | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_json_native(cls, value: Any) -> Any:
        if value is None:
            return {}
        return _json_native(value)

    def content(self) -> dict[str, Any]:
        """The hashed portion of the event (everything except ``hash``)."""
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "type": self.type.value,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
            "causer_event_id": self.causer_event_id,
            "source": self.source.value,
            "prev_hash": self.prev_hash,
        }

    def digest(self) -> str:
        """Recompute this event's content hash."""
        return stable_hash(self.content())

    def sealed(self) -> Event:
        """Return a copy with ``hash`` set to the recomputed digest."""
        return self.model_copy(update={"hash": self.digest()})


class IntegrityViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    run_id: str
    sequence: int | None = None
    event_id: str | None = None
    detail: str = ""


class IntegrityReport(BaseModel):
    """Result of re-auditing one or more chains.

    ``trusted_through`` is the actionable field: for each run it gives the
    highest sequence number whose prefix verified completely. A recovery engine
    can rebuild state from that prefix and treat everything after it as
    unverifiable rather than discarding the whole run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked: int
    violations: list[IntegrityViolation] = Field(default_factory=list)
    trusted_through: dict[str, int] = Field(default_factory=dict)
    truncated: bool = False


class EventLog:
    """In-memory append-only log, partitioned by ``run_id``.

    Phase 3 backs this with SQLite; the interface stays the same so callers
    never depend on the storage engine.
    """

    def __init__(self) -> None:
        self._by_run: dict[str, list[Event]] = {}

    # -- writing ---------------------------------------------------------- #

    def append(
        self,
        run_id: str,
        type: EventType,
        payload: Mapping[str, Any] | None = None,
        *,
        causer_event_id: str | None = None,
        timestamp: datetime | None = None,
        event_id: str | None = None,
        source: Origin = Origin.DETERMINISTIC,
    ) -> Event:
        """Append an event and return the sealed (hashed) record."""
        chain = self._by_run.setdefault(run_id, [])
        if type in (EventType.DECISION_CREATED, EventType.ACTION_RECORDED) and payload is not None:
            caused_by = payload.get("caused_by") if isinstance(payload, Mapping) else None
            if caused_by is not None:
                if not isinstance(caused_by, list):
                    raise ValueError("caused_by must be a list")
                if len(caused_by) > 32:
                    raise ValueError("caused_by must contain at most 32 ids")
                for cid in caused_by:
                    if not isinstance(cid, str) or not 1 <= len(cid) <= 128:
                        raise ValueError("caused_by entries must be 1-128 chars")
                    if not any(e.event_id == cid for e in chain):
                        raise ValueError(f"unknown caused_by id {cid!r}")
        head = chain[-1] if chain else None
        event = Event(
            event_id=event_id or make_id("event"),
            run_id=run_id,
            sequence=len(chain) + 1,
            type=type,
            timestamp=timestamp or utcnow(),
            payload=dict(payload or {}),
            causer_event_id=causer_event_id,
            source=source,
            prev_hash=head.hash if head else None,
        ).sealed()
        chain.append(event)
        return event

    def extend(self, events: Iterable[Event]) -> None:
        """Load already-sealed events (e.g. from storage), verifying the chain.

        Rejects anything that would rewrite or fork existing history.
        """
        for event in events:
            chain = self._by_run.setdefault(event.run_id, [])
            expected_sequence = len(chain) + 1
            if event.sequence != expected_sequence:
                raise AppendOnlyViolation(
                    f"run {event.run_id}: expected sequence {expected_sequence}, got {event.sequence}"
                )
            expected_prev = chain[-1].hash if chain else None
            if event.prev_hash != expected_prev:
                raise AppendOnlyViolation(
                    f"run {event.run_id} seq {event.sequence}: broken hash chain"
                )
            if event.hash != event.digest():
                raise AppendOnlyViolation(
                    f"run {event.run_id} seq {event.sequence}: event hash does not match content"
                )
            chain.append(event)

    # -- reading ---------------------------------------------------------- #

    def runs(self) -> tuple[str, ...]:
        return tuple(self._by_run)

    def events(self, run_id: str, *, after_sequence: int = 0) -> tuple[Event, ...]:
        chain = self._by_run.get(run_id, ())
        return tuple(e for e in chain if e.sequence > after_sequence)

    def by_type(self, run_id: str, type: EventType) -> tuple[Event, ...]:
        return tuple(e for e in self._by_run.get(run_id, ()) if e.type is type)

    def head(self, run_id: str) -> Event | None:
        chain = self._by_run.get(run_id)
        return chain[-1] if chain else None

    def last_sequence(self, run_id: str) -> int:
        return len(self._by_run.get(run_id, ()))

    def __iter__(self) -> Iterator[Event]:
        for chain in self._by_run.values():
            yield from chain

    def __len__(self) -> int:
        return sum(len(chain) for chain in self._by_run.values())

    # -- integrity -------------------------------------------------------- #

    def verify(self, run_id: str | None = None, *, max_violations: int = 1000) -> IntegrityReport:
        """Recompute every digest and re-walk the chain(s).

        The walk propagates the *recomputed* digest rather than the stored one,
        so an edited event is reported twice: ``TAMPERED_CONTENT`` on the event
        itself and ``BROKEN_CHAIN`` on its successor, whose link no longer
        matches. The walk then re-syncs, because untampered events remain
        internally consistent — so the violation list localises damage instead
        of flooding.

        Trust is expressed separately by ``trusted_through``: only the prefix
        before the first violation is considered verified. Events after it are
        readable but unverified, and callers must not treat them as authority.
        """
        run_ids = [run_id] if run_id is not None else list(self._by_run)
        violations: list[IntegrityViolation] = []
        trusted_through: dict[str, int] = {}
        checked = 0
        truncated = False

        def record(kind: str, rid: str, event: Event, detail: str) -> None:
            nonlocal truncated
            if len(violations) >= max_violations:
                truncated = True
                return
            violations.append(
                IntegrityViolation(
                    kind=kind,
                    run_id=rid,
                    sequence=event.sequence,
                    event_id=event.event_id,
                    detail=detail,
                )
            )

        for rid in run_ids:
            prev_digest: str | None = None
            expected_sequence = 1
            last_good = 0
            intact = True

            for event in self._by_run.get(rid, ()):
                checked += 1
                digest = event.digest()
                healthy = True

                if event.sequence != expected_sequence:
                    healthy = False
                    record("SEQUENCE_GAP", rid, event, f"expected sequence {expected_sequence}")
                if event.hash != digest:
                    healthy = False
                    record(
                        "TAMPERED_CONTENT",
                        rid,
                        event,
                        "stored hash does not match recomputed digest",
                    )
                if event.prev_hash != prev_digest:
                    healthy = False
                    record(
                        "BROKEN_CHAIN",
                        rid,
                        event,
                        f"prev_hash {event.prev_hash!r} does not match predecessor digest {prev_digest!r}",
                    )

                if healthy and intact:
                    last_good = event.sequence
                else:
                    intact = False

                prev_digest = digest
                expected_sequence = event.sequence + 1

            trusted_through[rid] = last_good

        return IntegrityReport(
            ok=not violations,
            checked=checked,
            violations=violations,
            trusted_through=trusted_through,
            truncated=truncated,
        )
