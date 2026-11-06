"""Risk-informed recovery policy (issue #303).

Risks arrive as RISK_OBSERVED events with Origin.EXTERNAL_MONITOR.
They are hash-chained, provenance-marked, and never self-certifying.
Ingestion is fail-open: malformed payloads are dropped and logged,
never blocking the run.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from continuum.events import EventType
from continuum.models import (
    Origin,
    PlanStep,
    PlanStepStatus,
    RiskObservedPayload,
    SemanticState,
    StateStatus,
)
from continuum.recovery.risk_policy import (
    BASELINE_RISK_POLICY,
    DEFAULT_RISK_POLICY,
    DEFAULT_RISK_POLICY_PATH,
    KNOWN_TRIGGERS,
    RiskPolicy,
    RiskPolicyError,
    evaluate_risk,
    evaluate_risk_action,
    is_more_conservative,
    load_risk_policy,
)
from continuum.storage.base import Storage

__all__ = [
    "ingest_risk",
    "RiskObservedPayload",
    "RiskPayload",
    "DEFAULT_RISK_POLICY",
    "DEFAULT_RISK_POLICY_PATH",
    "RiskPolicy",
    "RiskPolicyError",
    "RiskSignal",
    "load_risk_policy",
    "evaluate_risk",
    "evaluate_risk_action",
    "is_more_conservative",
    "ingest_risk_json_line",
    "signal_from_event",
    "signals_from_events",
    "locate_last_fact_gathering_step",
    "KNOWN_TRIGGERS",
    "BASELINE_RISK_POLICY",
    "ingest_risk_stream",
    "DEFAULT_RISK_BATCH_LIMIT",
    "MAX_RISK_RECORD_BYTES",
    "RiskRecordError",
]

#: Where dropped records are reported. Nothing else in core logs, so the logger
#: is unconfigured by default and silent until an operator points logging at it;
#: the drop summary on the returned record is what stays visible by default.
_logger = logging.getLogger("continuum.recovery.risk")
_logger.addHandler(logging.NullHandler())

# --------------------------------------------------------------------------- #
# Batch ingestion (issue #1425)
# --------------------------------------------------------------------------- #
#
# A monitoring feed is an external witness, not a gatekeeper: SNAGLINE, a
# webhook stream, a background watchdog. It speaks newline-delimited JSON at
# whatever volume it likes, and the one property the ingestion path has to
# guarantee is that a sick feed can never make the run it is monitoring sick.
# Torn lines, binary garbage, truncated connections and malformed unicode are
# dropped and counted; the batch is bounded so a feed faster than the fold
# cannot grow memory without bound; and every record lands or does not on its
# own, so a crash mid-batch leaves a durable prefix rather than an all-or-nothing
# hole. Risk events do not project (#303), so no amount of them can leave the
# run unprojectable or wedge the transaction the agent's own writer is holding.

#: How many records one call will take before reporting the rest as held over.
#: A feed that batches faster than it folds would otherwise hold an unbounded
#: list of pending payloads in memory, and a feed whose consumer died would
#: hold it forever. ``None`` disables the cap; the caller owns that choice.
DEFAULT_RISK_BATCH_LIMIT = 10_000

#: A record longer than this is a document, not a monitor line. The cap is what
#: keeps one hostile or runaway record from filling the whole batch on its own,
#: and skips the parse of something that cannot be a risk observation.
MAX_RISK_RECORD_BYTES = 64 * 1024

#: How many per-record failure samples the summary keeps. The full count is
#: always reported; the samples are the traceback-free preview an operator
#: reads first, and they are bounded for the same reason the batch is.
MAX_DROP_SAMPLES = 8

#: Preview width for a sample, wide enough to name the offending fragment and
#: narrow enough that one malformed record cannot pad the response.
_SAMPLE_PREVIEW_CHARS = 160


class RiskRecordError(Exception):
    """One batch record could not be ingested; the subclass names the reason.

    Every drop is classified so an operator can tell a feed sending garbage
    (``invalid_json``) from a feed sending well-formed records the schema
    refuses (``schema_validation``) from a feed whose bytes are not text at all
    (``undecodable_utf8``). Those three have different remedies, and a single
    bucket would hide which one a sick monitor needs.
    """

    reason = "invalid_record"


class UndecodableRecord(RiskRecordError):
    """Bytes that are not valid UTF-8."""

    reason = "undecodable_utf8"


class InvalidJsonRecord(RiskRecordError):
    """Text that is not valid JSON."""

    reason = "invalid_json"


class NotAnObjectRecord(RiskRecordError):
    """JSON that parses but is not an object."""

    reason = "not_an_object"


class OversizedRecord(RiskRecordError):
    """A single record past the per-record byte cap."""

    reason = "oversized_record"


def _preview(record: Any) -> str:
    """A short one-line rendering of a record, for the drop sample.

    Whitespace collapses so a record with embedded newlines stays one line in
    the summary, and non-string records are repr'd rather than stringified: a
    mapping's ``str`` is indistinguishable from a JSON line's at a glance, and
    the type is what says which one arrived.
    """
    if isinstance(record, (bytes, bytearray, memoryview)):
        text = bytes(record)[:_SAMPLE_PREVIEW_CHARS].decode("utf-8", errors="replace")
    elif isinstance(record, str):
        text = record[:_SAMPLE_PREVIEW_CHARS]
    else:
        text = repr(record)[:_SAMPLE_PREVIEW_CHARS]
    return " ".join(text.split())


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 string, used when a monitor omits its own clock."""
    return datetime.now(UTC).isoformat()


class RiskPayload(RiskObservedPayload):
    """Superseded alias of :class:`RiskObservedPayload`.

    Exported before the typed schema existed (issue #1421); kept so existing
    imports keep resolving. New code should name ``RiskObservedPayload``.
    """

    pass


@dataclass(frozen=True, slots=True)
class RiskSignal:
    """A risk observation reduced to the fields repair planning reads.

    ``RiskObservedPayload`` is the wire schema a monitor writes; this is the
    internal projection the planner consumes. Normalising event payloads,
    plain mappings and attribute objects to one typed shape before any step
    is planned from them is what keeps an untyped event from reaching the
    contract and producing a wrong repair target (issue #1423 review).
    """

    trigger: str
    step_id: str | None = None
    episode_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _clean(value: Any) -> str | None:
    """A non-blank string, stripped, or None for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def signal_from_event(event: Any) -> RiskSignal | None:
    """Reduce one risk event to a :class:`RiskSignal`, or None if malformed.

    Accepts the three shapes the recovery stack hands around: an event whose
    ``payload`` is a mapping, a plain mapping, and an object exposing the
    fields as attributes (older probes, tests). An entry without a usable
    trigger is dropped, mirroring ``ingest_risk``'s fail-open stance: a
    malformed signal contributes no repair step rather than a fabricated one.
    """
    payload: Mapping[str, Any] | None = None
    if isinstance(event, Mapping):
        payload = event
    else:
        raw = getattr(event, "payload", None)
        if isinstance(raw, Mapping):
            payload = raw
    if payload is not None:
        trigger = _clean(payload.get("trigger"))
        step_id = _clean(payload.get("step_id"))
        episode_id = _clean(payload.get("episode_id"))
        detail = payload.get("detail")
    else:
        trigger = _clean(getattr(event, "trigger", None))
        step_id = _clean(getattr(event, "step_id", None))
        episode_id = _clean(getattr(event, "episode_id", None))
        detail = getattr(event, "detail", None)
    if trigger is None:
        return None
    detail_map: dict[str, Any] = dict(detail) if isinstance(detail, Mapping) else {}
    if episode_id is None:
        # A monitor that nests everything in detail still carries the episode
        # there; the repair reason names it, so losing it to a nesting
        # difference would drop the provenance an operator needs.
        episode_id = _clean(detail_map.get("episode_id"))
    return RiskSignal(
        trigger=trigger,
        step_id=step_id,
        episode_id=episode_id,
        detail=detail_map,
    )


def signals_from_events(events: Iterable[Any]) -> list[RiskSignal]:
    """Reduce an event sequence to signals, dropping malformed entries."""
    signals: list[RiskSignal] = []
    for event in events:
        signal = signal_from_event(event)
        if signal is not None:
            signals.append(signal)
    return signals


#: Fact-gathering vocabulary for locating a rollback anchor (issue #1423).
#: Matched on whole tokens, never substrings: "already" contains "read" and
#: "thread" contains "read" exactly the way a real fact step does under a
#: substring test, which is how an unrelated verified step ("already
#: refactored the retry budget") beat one carrying real evidence. The
#: optional ``(?:s|ing)?`` keeps "searching", "findings" and "gathering"
#: legible without re-opening the substring hazard.
_FACT_GATHERING_RE = re.compile(
    r"\b(gather|fact|facts|evidence|collect|query|fetch|search|read|check"
    r"|inspect|observe|probe|find|finding|findings|investigate|lookup)"
    r"(?:s|ing)?\b",
    re.IGNORECASE,
)

_EXPLICIT_ANCHOR_KEYS = ("last_verified_step", "fact_gathering_step", "rollback_target")


def _verified_kinds() -> frozenset[PlanStepStatus]:
    return frozenset((PlanStepStatus.VERIFIED, PlanStepStatus.COMPLETED))


def _ancestors(plan_by_id: Mapping[str, PlanStep], trigger_step_id: str) -> set[str]:
    """Ids the trigger step depends on, directly or transitively.

    ``plan_upsert`` stores ``SemanticState.plan`` sorted by ``step_id``, so a
    position in that list is not execution order: a completed ``z_fetch``
    before a failing ``a_cascade`` is stored with ``a_cascade`` first, and a
    positional slice drops the only real anchor (issue #1423 review). The
    dependency graph is the ordering the plan itself records.
    """
    seen: set[str] = set()
    frontier = [trigger_step_id]
    while frontier:
        step = plan_by_id.get(frontier.pop())
        if step is None:
            continue
        for dep in step.depends_on:
            if dep not in seen:
                seen.add(dep)
                frontier.append(dep)
    return seen


def _candidates_before_cascade(plan: list[PlanStep], trigger_step_id: str | None) -> list[PlanStep]:
    """Plan steps that may have run before the cascade, in stored order.

    Without a trigger there is no cascade boundary to respect, so every step
    is a candidate. With one, a step qualifies by being recorded before the
    trigger step's last update (``provenance.source_sequence``), by depending
    into it through the ``depends_on`` graph, or by sitting before it in the
    stored list. The list alone is the weakest of the three: it is a
    ``step_id`` sort, not execution order, and survives only for plans whose
    events carry no sequence and no dependencies.
    """
    if trigger_step_id is None:
        return list(plan)
    trigger_index: int | None = None
    for index, step in enumerate(plan):
        if step.step_id == trigger_step_id:
            trigger_index = index
            break
    if trigger_index is None:
        return list(plan)
    boundary = plan[trigger_index].provenance.source_sequence
    ancestors = _ancestors({s.step_id: s for s in plan}, trigger_step_id)
    candidates: list[PlanStep] = []
    for index, step in enumerate(plan):
        if index == trigger_index:
            continue
        sequence = step.provenance.source_sequence
        recorded_earlier = sequence is not None and boundary is not None and sequence < boundary
        if index < trigger_index or step.step_id in ancestors or recorded_earlier:
            candidates.append(step)
    return candidates


def _last_valid_entry_id(entries: Any, attr: str) -> str | None:
    """The id of the last VALID entry in a state collection, or None."""
    valid = [e for e in (entries or ()) if e.status is StateStatus.VALID]
    if not valid:
        return None
    value = getattr(valid[-1], attr, None)
    return value if isinstance(value, str) and value else None


def locate_last_fact_gathering_step(
    state: SemanticState | None,
    *,
    trigger_step_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> str | None:
    """Locate the step a meltdown rollback should target (issue #1423).

    Explicit anchors the monitor names in ``detail`` win first; the plan is
    then scanned. A plan step qualifies only when it is verified or completed
    and matches the fact-gathering vocabulary on whole tokens, ranked by
    (carries evidence, recorded later, stored later) so the *last* verified
    fact-gathering step wins. Anything weaker is not a safe rollback point and
    is deliberately not named: unverified or non-factual steps, arbitrary
    stored positions, and evidence or findings the projection marked other
    than VALID are all skipped. The caller must then say no anchor could be
    located instead of back-filling one of them: the trigger step is the one
    that was melting down, and an arbitrary position is not a fact.
    """
    if isinstance(detail, Mapping):
        for key in _EXPLICIT_ANCHOR_KEYS:
            explicit = _clean(detail.get(key))
            # An anchor that names the trigger step points at the failure
            # itself, not at work before it: ignore it and keep looking.
            if explicit is not None and explicit != trigger_step_id:
                return explicit
    if state is None:
        return None

    candidates = _candidates_before_cascade(list(state.plan or ()), trigger_step_id)
    verified_kinds = _verified_kinds()
    factual = [
        s
        for s in candidates
        if s.status in verified_kinds
        and _FACT_GATHERING_RE.search(f"{s.step_id} {s.description}".lower())
    ]
    if factual:
        best = max(
            factual,
            key=lambda s: (
                bool(s.evidence),
                s.provenance.source_sequence or -1,
                candidates.index(s),
            ),
        )
        return best.step_id

    # No usable plan step: the evidence and finding stores are the run's
    # verified facts, and only entries the projection marked VALID name one.
    return _last_valid_entry_id(state.evidence, "evidence_id") or _last_valid_entry_id(
        state.findings, "finding_id"
    )


def _validated_risk_payload(payload: Any) -> dict[str, Any]:
    """Normalise a caller payload into the wire payload, or raise.

    Raises :class:`TypeError` for something that is not an object at all and
    :class:`~pydantic.ValidationError` for an object the schema refuses, so
    each caller can decide which of those is a dropped record and which is a
    protocol error. Anything raised here never reaches the event log.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"risk payload must be a JSON object, got {type(payload).__name__}")
    schema = RiskObservedPayload(
        trigger=payload.get("trigger"),
        score=payload.get("score", 0.0),
        episode_id=payload.get("episode_id"),
        step_id=payload.get("step_id"),
        detail=payload.get("detail", ""),
        # Prefer the caller's observation time over the ingestion clock: a
        # probe reporting when it saw the anomaly must not have its timestamp
        # replaced by server now, and neither spelling should be dropped
        # (issue #1075).
        ts=(
            payload["ts"]
            if payload.get("ts") is not None
            else payload["timestamp"]
            if payload.get("timestamp") is not None
            else _now_iso()
        ),
    )
    event_payload: dict[str, Any] = {
        "trigger": schema.trigger,
        "score": schema.score,
        "detail": schema.detail,
        "ts": schema.ts.isoformat(),
    }
    if schema.episode_id is not None:
        event_payload["episode_id"] = schema.episode_id
    if schema.step_id is not None:
        event_payload["step_id"] = schema.step_id
    return event_payload


def ingest_risk(
    storage: Storage,
    run_id: str,
    payload: dict[str, Any],
) -> bool:
    """Ingest a risk signal as a RISK_OBSERVED event, fail-open.

    Provenance is not caller-controllable: the event is always stamped
    ``Origin.EXTERNAL_MONITOR`` (issue #1421), so a monitor's observation can
    never be fabricated or self-certified by the agent it describes. Payload
    shape is validated and normalised through ``RiskObservedPayload`` first, so
    what lands on the wire is exactly what the schema describes.
    """
    try:
        storage.append_event(
            run_id,
            EventType.RISK_OBSERVED,
            _validated_risk_payload(payload),
            source=Origin.EXTERNAL_MONITOR,
        )
    except Exception:
        return False
    return True


def ingest_risk_json_line(storage: Storage, run_id: str, line: str) -> bool:
    """Ingest a single JSON line from a SNAGLINE stream, fail-open."""
    line = line.strip()
    if not line:
        return False
    try:
        data = json.loads(line)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return ingest_risk(storage, run_id, data)


def _decode_record(record: Any) -> dict[str, Any] | None:
    """Turn one batch record into a payload mapping, classify the failure, or skip.

    Returns ``None`` for a record that is nothing but whitespace: the trailing
    newline at the end of a file and the blank line a monitor emits between
    batches are separators, not corrupt records, so they are skipped without
    being counted as drops.

    Raises a :class:`RiskRecordError` subclass otherwise, one reason per failure
    mode, so the summary an operator reads names what the feed did wrong rather
    than that something did.
    """
    if isinstance(record, Mapping):
        size = len(json.dumps(dict(record), default=str).encode("utf-8"))
        if size > MAX_RISK_RECORD_BYTES:
            raise OversizedRecord(f"{size} bytes exceeds {MAX_RISK_RECORD_BYTES}")
        return dict(record)
    if isinstance(record, (bytes, bytearray, memoryview)):
        raw = bytes(record)
        if not raw.strip():
            return None
        if len(raw) > MAX_RISK_RECORD_BYTES:
            raise OversizedRecord(f"{len(raw)} bytes exceeds {MAX_RISK_RECORD_BYTES}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UndecodableRecord(str(exc)) from exc
    elif isinstance(record, str):
        if not record.strip():
            return None
        if len(record) > MAX_RISK_RECORD_BYTES:
            raise OversizedRecord(f"{len(record)} characters exceeds {MAX_RISK_RECORD_BYTES}")
        text = record
    else:
        raise NotAnObjectRecord(
            f"expected an object, a JSON line, or bytes, got {type(record).__name__}"
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidJsonRecord(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise NotAnObjectRecord(f"JSON record is a {type(parsed).__name__}, not an object")
    return parsed


def _bump_metric(name: str, by: int) -> None:
    """Count an ingestion outcome on the process metrics, best-effort.

    Metrics are observability: a collector that is missing or raises must never
    be the reason a risk record stopped landing, so the failure is swallowed
    the same way a sick feed's is.
    """
    try:
        from continuum.observability import get_metrics

        get_metrics().increment(name, by=by)
    except Exception:  # pragma: no cover - the metric is the only casualty
        pass


def ingest_risk_stream(
    storage: Storage,
    run_id: str,
    records: Iterable[Any],
    *,
    limit: int | None = DEFAULT_RISK_BATCH_LIMIT,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Ingest a batch of risk-signal records, fail-open and bounded (issue #1425).

    Each record is decoded, parsed, schema-validated and written on its own, and
    each of those four can fail without affecting the others or the run: a torn
    line is dropped, the well-formed line after it still lands. Nothing is held
    in a buffer across records, so a crash mid-batch leaves every record that was
    already written durable rather than rolling back the whole observation.

    ``limit`` caps how many records the call takes from the iterable before
    reporting the remainder as held over (``truncated``). It is the bound on
    memory for a high-volume feed, and it is a property of the caller's batch,
    not of the feed: the caller re-batches from where the summary's
    ``last_sequence`` left off.

    The return value always carries the full accounting -- accepted, dropped,
    skipped, per-reason drop counts and bounded samples -- because a fail-open
    path that dropped records silently is the alternative the issue rejects:
    operators need to see what the feed is costing them.
    """
    log = logger if logger is not None else _logger
    counters: dict[str, int] = defaultdict(int)
    samples: list[dict[str, Any]] = []
    accepted = 0
    skipped = 0
    dropped = 0
    last_sequence: int | None = None
    truncated = False
    taken = 0

    def drop(reason: str, exc: BaseException, record: Any) -> None:
        nonlocal dropped
        dropped += 1
        counters[reason] += 1
        # The first failures of each kind are the ones an operator acts on;
        # later identical drops are still counted, just not re-sampled.
        if len(samples) < MAX_DROP_SAMPLES:
            samples.append(
                {"reason": reason, "message": " ".join(str(exc).split())[:_SAMPLE_PREVIEW_CHARS]}
            )
        log.warning(
            "dropped risk record for run %s (%s): %s",
            run_id,
            reason,
            _preview(record),
        )

    for record in records:
        taken += 1
        if limit is not None and taken > limit:
            truncated = True
            break
        try:
            payload = _decode_record(record)
        except RiskRecordError as exc:
            drop(exc.reason, exc, record)
            continue
        except Exception as exc:  # a decoder bug, not a feed fault, but still fail-open
            drop("invalid_record", exc, record)
            continue
        if payload is None:
            skipped += 1
            continue
        try:
            event = storage.append_event(
                run_id,
                EventType.RISK_OBSERVED,
                _validated_risk_payload(payload),
                source=Origin.EXTERNAL_MONITOR,
            )
        except ValidationError as exc:
            drop("schema_validation", exc, payload)
        except Exception as exc:
            # A write that failed is still a dropped record, not a raised one:
            # the run the feed is watching keeps its transaction and its lock,
            # and the rest of the batch is still attempted.
            drop("write_failed", exc, payload)
        else:
            accepted += 1
            last_sequence = event.sequence

    if accepted:
        _bump_metric("risks.ingested", accepted)
    if dropped:
        _bump_metric("risks.dropped", dropped)

    return {
        "run_id": run_id,
        "accepted": accepted,
        "dropped": dropped,
        "skipped": skipped,
        "truncated": truncated,
        "limit": limit,
        "last_sequence": last_sequence,
        "dropped_by_reason": dict(counters),
        "dropped_samples": samples,
    }
