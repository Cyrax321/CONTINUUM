"""Reviewer fatigue telemetry: audit events for the human gate (issue #1411).

Rubber-stamping is what happens when a fatigued operator approves batches of
actions without inspecting them. Before this module, CONTINUUM recorded approval
outcomes and nothing about how long a decision took, how large a batch was, or
how fast approvals were landing. Without that telemetry an organisation cannot
tell when an agent is overwhelming its human oversight, and the audit trail
cannot distinguish "a human reviewed this" from "a human clicked through this".

Three facts are recorded, all through ``Storage.append_event`` so they ride the
same per-run hash chain as every other event and are covered by ``verify()``:

* ``REVIEW_PARKED``: an action entered the review queue, carrying its risk score.
* ``REVIEW_BATCH_APPROVED``: a human cleared a batch, carrying item count,
  reviewer identity and the wall-clock the batch consumed.
* ``FATIGUE_SIGNAL_RECORDED``: a batch tripped the fatigue contract: the
  recorded per-item decision time fell below what genuine inspection takes.

The signal is advisory, by design. Blocking an approval when velocity is high
would deadlock an emergency operation, which is exactly the trade the issue
rejects; the warning is surfaced in ``continuum health`` and the evidence is the
log itself, hash-chained and re-auditable.

Thresholds live in ``.continuum/fatigue.json`` so an operator tunes them without
touching code, mirroring the liveness cadence contract (issue #302). Evaluation
is a pure function of events plus contract, and every recorder takes an injected
clock, so tests never sleep and identical history yields an identical reading.

Measuring inspection time honestly
----------------------------------
No surface can see the operator's screen, so decision time is not measured
directly. What is measurable is the interval between an action entering the
queue and the human clearing it, which upper-bounds the attention each item got:
a batch of five parked actions cleared one second after the oldest was parked
could not have been inspected, regardless of how the approval was phrased. That
is the number recorded, divided by the item count, and the contract judges it.
When nothing was parked the interval is unmeasurable rather than zero, so no
reading is recorded at all (an unmeasurable time must not be reported as a fast
one).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from continuum.events import Event, EventType
from continuum.models import Origin, utcnow

if TYPE_CHECKING:
    from continuum.storage.base import Storage

__all__ = [
    "FatigueContract",
    "FatigueReading",
    "DEFAULT_FATIGUE_PATH",
    "DEFAULT_MIN_DECISION_SECONDS",
    "DEFAULT_MIN_BATCH_SIZE",
    "DEFAULT_COMPLEXITY_FLOOR",
    "load_fatigue_contract",
    "record_review_parked",
    "record_batch_approval",
    "evaluate_batch",
    "open_dwell_seconds",
    "fatigue_advisory",
    "advisory_text",
]

DEFAULT_FATIGUE_PATH: Path = Path(".continuum/fatigue.json")
#: Below this many seconds per item, a high-complexity batch was not inspected.
#: 500ms is the issue's stated bound: it is roughly the time to read a single
#: action type, let alone its arguments and its risk score.
DEFAULT_MIN_DECISION_SECONDS: float = 0.5
#: Rubber-stamping is a batch behaviour, so a single approval can never trip
#: the contract on its own no matter how fast it lands.
DEFAULT_MIN_BATCH_SIZE: int = 2
#: Only high-complexity operations trip the contract by default; a fast approval
#: of low-complexity items is efficiency, not fatigue.
DEFAULT_COMPLEXITY_FLOOR: str = "high"

#: Subjective complexity rank, so ``complexity_floor`` compares ordinally.
#: Unknown values rank below everything, so a misspelt floor or complexity is
#: read as "do not trip" rather than silently escalating.
_COMPLEXITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _rank(complexity: Any) -> int:
    """Ordinal rank of a complexity label, -1 for anything unrecognised."""
    if isinstance(complexity, str):
        return _COMPLEXITY_RANK.get(complexity, -1)
    return -1


class FatigueContract(BaseModel):
    """Operator-set bounds on what counts as rubber-stamping.

    The file ``.continuum/fatigue.json`` may hold any subset of these fields::

        {"min_decision_seconds": 0.5, "min_batch_size": 2, "complexity_floor": "high"}
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_decision_seconds: float = Field(default=DEFAULT_MIN_DECISION_SECONDS, gt=0)
    min_batch_size: int = Field(default=DEFAULT_MIN_BATCH_SIZE, ge=1)
    complexity_floor: str = Field(default=DEFAULT_COMPLEXITY_FLOOR)

    @field_validator("complexity_floor")
    @classmethod
    def _known_complexity(cls, value: str) -> str:
        if value not in _COMPLEXITY_RANK:
            raise ValueError(
                f"complexity_floor must be one of {sorted(_COMPLEXITY_RANK)}, got {value!r}"
            )
        return value

    def trips(
        self,
        *,
        item_count: int,
        complexity: str,
        decision_seconds: float,
    ) -> bool:
        """Whether one batch crosses the bound, and so deserves a signal.

        A batch trips only when it is large enough, complex enough and fast
        enough. Every condition is a deliberate false-positive guard: the signal
        names a human behaviour, and a warning that fires on normal operation
        gets ignored when the real one arrives.
        """
        if item_count < self.min_batch_size:
            return False
        if _rank(complexity) < _rank(self.complexity_floor):
            return False
        return decision_seconds < self.min_decision_seconds


class FatigueReading(BaseModel):
    """The verdict on one batch, derived purely from its event and a contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fatigued: bool
    batch_event_id: str
    item_count: int
    complexity: str
    dwell_seconds: float
    decision_seconds: float
    threshold_seconds: float
    reason: str


def load_fatigue_contract(path: Path | None = None) -> FatigueContract:
    """Load the contract from ``path`` or the default location.

    A missing file yields the defaults. A present file must be a valid object
    with known values: an operator who sets a nonsense threshold is told now,
    rather than the first time a batch is approved against it.
    """
    target = path or DEFAULT_FATIGUE_PATH
    if not target.exists():
        return FatigueContract()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid fatigue contract {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"fatigue contract {target} must be a JSON object")
    return FatigueContract.model_validate(data)


def _payload(event: Event) -> dict[str, Any]:
    """The event payload as a plain dict, tolerant of storage rehydration."""
    raw = event.payload
    return dict(raw) if isinstance(raw, Mapping) else {}


def record_review_parked(
    storage: Storage,
    run_id: str,
    *,
    key: str,
    action_type: str,
    reason: str,
    risk_score: float | None = None,
    source: Origin = Origin.DETERMINISTIC,
) -> Event:
    """Record that an action entered the review queue (``REVIEW_PARKED``).

    ``risk_score`` is the score the parking decision used, when the caller has
    one, so an auditor can later correlate what was parked against how risky it
    actually was. It is clamped to ``[0, 1]`` and is optional: a caller without
    a score parks honestly rather than inventing one.
    """
    payload: dict[str, Any] = {
        "key": key,
        "action_type": action_type,
        "reason": reason,
    }
    if risk_score is not None:
        if not isinstance(risk_score, (int, float)) or not 0.0 <= float(risk_score) <= 1.0:
            raise ValueError(f"risk_score must be within [0, 1], got {risk_score!r}")
        payload["risk_score"] = float(risk_score)
    return storage.append_event(
        run_id,
        EventType.REVIEW_PARKED,
        payload,
        source=source,
    )


def open_dwell_seconds(
    storage: Storage,
    run_id: str,
    *,
    now: datetime | None = None,
) -> float | None:
    """Seconds since the oldest still-open parked item, or ``None``.

    Open means parked after the last ``REVIEW_BATCH_APPROVED``: a batch clears
    the queue, so anything parked before it is closed and must not be re-counted
    against the next batch. Returns ``None`` when nothing is open, which is the
    caller's signal that dwell is unmeasurable for this batch.
    """
    now_ts = now or utcnow()
    events = storage.read_events(run_id)
    last_batch_index = -1
    for index, event in enumerate(events):
        if event.type is EventType.REVIEW_BATCH_APPROVED:
            last_batch_index = index
    parked = [
        event for event in events[last_batch_index + 1 :] if event.type is EventType.REVIEW_PARKED
    ]
    if not parked:
        return None
    oldest = min(event.timestamp for event in parked)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=UTC)
    elapsed = (now_ts - oldest).total_seconds()
    return float(elapsed) if elapsed >= 0 else 0.0


def evaluate_batch(
    event: Event,
    *,
    contract: FatigueContract | None = None,
) -> FatigueReading | None:
    """Pure verdict on one ``REVIEW_BATCH_APPROVED`` event.

    Returns ``None`` for any other event type, and for a batch whose dwell was
    never measured: an unmeasurable interval is not evidence of a fast one, so
    it produces no reading rather than a green one.
    """
    if event.type is not EventType.REVIEW_BATCH_APPROVED:
        return None
    payload = _payload(event)
    dwell = payload.get("dwell_seconds")
    if dwell is None:
        return None
    cfg = contract or FatigueContract()
    item_count = int(payload.get("item_count") or 0)
    complexity = str(payload.get("complexity") or "medium")
    decision_seconds = float(payload.get("decision_seconds") or 0.0)
    fatigued = cfg.trips(
        item_count=item_count,
        complexity=complexity,
        decision_seconds=decision_seconds,
    )
    reason = (
        f"batch of {item_count} high-complexity item(s) cleared in "
        f"{decision_seconds:.3f}s per item, under the {cfg.min_decision_seconds}s "
        f"inspection bound"
        if fatigued
        else f"batch of {item_count} item(s) cleared in {decision_seconds:.3f}s per item, "
        f"within the {cfg.min_decision_seconds}s bound"
    )
    return FatigueReading(
        fatigued=fatigued,
        batch_event_id=event.event_id,
        item_count=item_count,
        complexity=complexity,
        dwell_seconds=float(dwell),
        decision_seconds=decision_seconds,
        threshold_seconds=cfg.min_decision_seconds,
        reason=reason,
    )


def _already_signalled(storage: Storage, run_id: str, batch_event_id: str) -> bool:
    """Whether a fatigue signal already exists for this batch.

    Re-evaluating a batch must not append a second signal for it: the audit trail
    would then claim the same rubber-stamp was detected twice.
    """
    for event in storage.read_events(run_id):
        if (
            event.type is EventType.FATIGUE_SIGNAL_RECORDED
            and _payload(event).get("batch_event_id") == batch_event_id
        ):
            return True
    return False


def record_batch_approval(
    storage: Storage,
    run_id: str,
    *,
    item_count: int,
    reviewer: str,
    complexity: str = "high",
    dwell_seconds: float | None = None,
    contract: FatigueContract | None = None,
    now: datetime | None = None,
    source: Origin = Origin.HUMAN,
) -> tuple[Event, Event | None]:
    """Record a human clearing a batch, and the signal when it trips the contract.

    ``dwell_seconds`` is the wall-clock the batch spent awaiting a decision. When
    it is ``None`` the dwell is read from the run's still-open parked items, which
    is the interval an approval actually closed. Still-``None`` means nothing was
    parked, so no per-item time can be derived and no signal is recorded.

    Returns the batch event and the signal event, or ``None`` for the signal when
    the batch was within bounds, unmeasurable, or already signalled.
    """
    if item_count < 1:
        raise ValueError(f"item_count must be at least 1, got {item_count!r}")
    if not reviewer or not isinstance(reviewer, str):
        raise ValueError("reviewer must be a non-empty string")
    cfg = contract or load_fatigue_contract()
    now_ts = now or utcnow()
    dwell = dwell_seconds
    if dwell is None:
        dwell = open_dwell_seconds(storage, run_id, now=now_ts)
    decision_seconds: float | None
    if dwell is None:
        decision_seconds = None
    else:
        elapsed = max(float(dwell), 0.0)
        decision_seconds = elapsed / item_count
    batch = storage.append_event(
        run_id,
        EventType.REVIEW_BATCH_APPROVED,
        {
            "item_count": item_count,
            "reviewer": reviewer,
            "complexity": complexity,
            "dwell_seconds": dwell,
            "decision_seconds": decision_seconds,
        },
        source=source,
    )
    reading = evaluate_batch(batch, contract=cfg)
    if reading is None or not reading.fatigued:
        return batch, None
    if _already_signalled(storage, run_id, batch.event_id):
        return batch, None
    signal = storage.append_event(
        run_id,
        EventType.FATIGUE_SIGNAL_RECORDED,
        {
            "batch_event_id": batch.event_id,
            "item_count": reading.item_count,
            "complexity": reading.complexity,
            "reviewer": reviewer,
            "dwell_seconds": reading.dwell_seconds,
            "decision_seconds": reading.decision_seconds,
            "threshold_seconds": reading.threshold_seconds,
            "reason": reading.reason,
        },
        source=source,
    )
    return batch, signal


def fatigue_advisory(
    storage: Storage,
    run_id: str,
    *,
    contract: FatigueContract | None = None,
) -> dict[str, Any]:
    """Read-only fatigue reading for a run, safe for every read path.

    Counts parked items, batches and signals over the live and archived log, and
    reports the median per-item decision time alongside the configured bound.
    Never raises into a caller that only wants a health line: a storage failure
    degrades to "unknown", not to a crash that hides the rest of the report.
    """
    try:
        events = list(storage.read_all_events(run_id))
    except Exception:
        events = []
    try:
        cfg = contract or load_fatigue_contract()
    except Exception:
        cfg = FatigueContract()
    parked = [e for e in events if e.type is EventType.REVIEW_PARKED]
    batches = [e for e in events if e.type is EventType.REVIEW_BATCH_APPROVED]
    signals = [e for e in events if e.type is EventType.FATIGUE_SIGNAL_RECORDED]
    items_approved = sum(int(_payload(e).get("item_count") or 0) for e in batches)
    decisions = [
        float(_payload(e)["decision_seconds"])
        for e in batches
        if _payload(e).get("decision_seconds") is not None
    ]
    latest = signals[-1] if signals else None
    latest_payload = _payload(latest) if latest is not None else None
    return {
        "fatigued": bool(signals),
        "items_parked": len(parked),
        "batches_approved": len(batches),
        "items_approved": items_approved,
        "fatigue_signals": len(signals),
        "median_decision_seconds": float(median(decisions)) if decisions else None,
        "threshold_seconds": cfg.min_decision_seconds,
        "latest_signal": latest_payload,
    }


def advisory_text(advisory: dict[str, Any]) -> str:
    """Render the advisory as one human line, never affects an exit code."""
    batches = advisory.get("batches_approved") or 0
    if not batches:
        return "Reviewer fatigue: no batch approvals recorded yet."
    median_seconds = advisory.get("median_decision_seconds")
    threshold = advisory.get("threshold_seconds")
    signals = advisory.get("fatigue_signals") or 0
    if advisory.get("fatigued"):
        median_text = f"{median_seconds:.3f}s" if median_seconds is not None else "unmeasured"
        return (
            f"Reviewer fatigue: WARNING, {signals} signal(s) recorded; median decision "
            f"{median_text} per item against a {threshold}s inspection bound. Advisory only."
        )
    median_text = f"{median_seconds:.3f}s" if median_seconds is not None else "unmeasured"
    return (
        f"Reviewer fatigue: ok, {batches} batch approval(s), median decision {median_text} "
        f"per item within the {threshold}s bound."
    )
