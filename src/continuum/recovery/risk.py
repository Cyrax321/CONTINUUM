"""Risk-informed recovery policy (issue #303).

Risks arrive as RISK_OBSERVED events with Origin.EXTERNAL_MONITOR.
They are hash-chained, provenance-marked, and never self-certifying.
Ingestion is fail-open: malformed payloads are dropped and logged,
never blocking the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from continuum.events import EventType
from continuum.models import Origin, RiskObservedPayload
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
    "load_risk_policy",
    "evaluate_risk",
    "evaluate_risk_action",
    "is_more_conservative",
    "ingest_risk_json_line",
    "KNOWN_TRIGGERS",
    "BASELINE_RISK_POLICY",
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RiskPayload(RiskObservedPayload):
    """Superseded alias of :class:`RiskObservedPayload`.

    Exported before the typed schema existed (issue #1421); kept so existing
    imports keep resolving. New code should name ``RiskObservedPayload``.
    """

    pass


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
