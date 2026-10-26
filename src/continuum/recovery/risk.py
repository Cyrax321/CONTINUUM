"""Risk-informed recovery policy (issue #303).

Risks arrive as RISK_OBSERVED events with Origin.EXTERNAL_MONITOR.
They are hash-chained, provenance-marked, and never self-certifying.
Ingestion is fail-open: malformed payloads are dropped and logged,
never blocking the run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from continuum.events import EventType
from continuum.models import (
    Origin,
    PlanStep,
    PlanStepStatus,
    RecoveryMode,
    RiskObservedPayload,
    SemanticState,
    StateStatus,
)
from continuum.storage.base import Storage

__all__ = [
    "ingest_risk",
    "RiskObservedPayload",
    "RiskPayload",
    "DEFAULT_RISK_POLICY",
    "DEFAULT_RISK_POLICY_PATH",
    "RiskPolicy",
    "RiskSignal",
    "load_risk_policy",
    "evaluate_risk",
    "is_more_conservative",
    "ingest_risk_json_line",
    "signal_from_event",
    "signals_from_events",
    "locate_last_fact_gathering_step",
]

DEFAULT_RISK_POLICY_PATH = Path(".continuum/risk-policy.json")

DEFAULT_RISK_POLICY: dict[str, str] = {
    "loop": RecoveryMode.REPLAN.value,
    "error_cascade": RecoveryMode.WAIT.value,
    "latency_anomaly": "annotate",
    "token_runaway": RecoveryMode.WAIT.value,
    "silent_abort": RecoveryMode.REPAIR_AND_RESUME.value,
    "meltdown": RecoveryMode.ROLLBACK.value,
    "side_effect_duplicate": RecoveryMode.ABORT.value,
    "governance_decay": RecoveryMode.REQUEST_HUMAN.value,
}

_VALID_POLICY_MODES = {m.value for m in RecoveryMode} | {"annotate"}


class RiskPolicy(dict):  # type: ignore[type-arg]
    """Validated risk policy mapping."""

    pass


def is_more_conservative(new_mode: str, old_mode: str) -> bool:
    """Whether new_mode is at least as severe as old_mode."""
    if old_mode == "annotate":
        return True
    if new_mode == "annotate":
        return False
    try:
        from continuum.recovery.engine import SEVERITY

        new_m = RecoveryMode(new_mode)
        old_m = RecoveryMode(old_mode)
        return SEVERITY.get(new_m, 0) >= SEVERITY.get(old_m, 0)
    except Exception:
        return False


def load_risk_policy(path: Path | None = None) -> dict[str, str]:
    """Load policy from path or default, with validation."""
    import json

    target = Path(path) if path is not None else DEFAULT_RISK_POLICY_PATH
    if not target.exists():
        return dict(DEFAULT_RISK_POLICY)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid risk policy {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"risk policy {target} must be a JSON object")
    result: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError("risk policy keys must be non-empty strings")
        if not isinstance(v, str) or v not in _VALID_POLICY_MODES:
            raise ValueError(
                f"risk policy[{k!r}] must be one of {sorted(_VALID_POLICY_MODES)}, got {v!r}"
            )
        default_mode = DEFAULT_RISK_POLICY.get(k.strip().lower())
        if default_mode is not None and not is_more_conservative(v, default_mode):
            raise ValueError(
                f"risk policy[{k!r}] downgrades {default_mode!r} to {v!r}; "
                f"operators may only make actions more conservative"
            )
        result[k.strip().lower()] = v
    for dk, dv in DEFAULT_RISK_POLICY.items():
        if dk not in result:
            result[dk] = dv
    return result


def evaluate_risk(trigger: str, policy: dict[str, str] | None = None) -> str | None:
    """Return the mode for a trigger, or None for annotate/no-action."""
    if not isinstance(trigger, str) or not trigger.strip():
        return None
    pol = policy if policy is not None else DEFAULT_RISK_POLICY
    mode = pol.get(trigger.strip().lower())
    if mode is None or mode == "annotate":
        return None
    return mode


def _now_iso() -> str:
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
        if not isinstance(payload, Mapping):
            return False
        try:
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
                ts=payload.get("ts") or payload.get("timestamp") or _now_iso(),
            )
        except ValidationError:
            return False
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
        storage.append_event(
            run_id, EventType.RISK_OBSERVED, event_payload, source=Origin.EXTERNAL_MONITOR
        )
        return True
    except Exception:
        return False


def ingest_risk_json_line(storage: Storage, run_id: str, line: str) -> bool:
    """Ingest a single JSON line from a SNAGLINE stream, fail-open."""
    import json

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
