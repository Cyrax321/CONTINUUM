"""Risk-informed recovery policy (issue #303).

Risks arrive as RISK_OBSERVED events with Origin.EXTERNAL_MONITOR.
They are hash-chained, provenance-marked, and never self-certifying.
Ingestion is fail-open: malformed payloads are dropped and logged,
never blocking the run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from continuum.events import EventType
from continuum.models import Origin
from continuum.storage.base import Storage

__all__ = ["ingest_risk", "RiskPayload"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RiskPayload(dict):  # type: ignore[type-arg]
    """Validated RISK_OBSERVED payload, fail-open on bad input."""

    pass


def ingest_risk(
    storage: Storage,
    run_id: str,
    payload: dict[str, Any],
) -> bool:
    """Ingest a risk signal as a RISK_OBSERVED event, fail-open.

    Returns True if the event was recorded, False if the payload was
    malformed and dropped. Never raises for bad input, so a dead monitor
    feed cannot become a denial of service.
    """
    try:
        # Validate required fields
        trigger = payload.get("trigger")
        if not isinstance(trigger, str) or not trigger.strip():
            return False
        # Normalize trigger to lowercase
        trigger = trigger.strip().lower()
        # Optional fields with defaults
        score = payload.get("score", 0.0)
        try:
            score_f = float(score) if score is not None else 0.0
        except Exception:
            score_f = 0.0
        episode_id = payload.get("episode_id")
        step_id = payload.get("step_id")
        detail = payload.get("detail", "")
        # Build payload, keep only known keys
        event_payload: dict[str, Any] = {
            "trigger": trigger,
            "score": score_f,
            "detail": str(detail)[:512] if detail else "",
        }
        if episode_id is not None:
            event_payload["episode_id"] = str(episode_id)[:128]
        if step_id is not None:
            event_payload["step_id"] = str(step_id)[:128]
        # Add timestamp if not present
        if "ts" not in event_payload and "timestamp" not in payload:
            event_payload["ts"] = _now_iso()
        elif "ts" in payload:
            event_payload["ts"] = str(payload["ts"])[:64]
        # Append as EXTERNAL_MONITOR, hash-chained
        storage.append_event(
            run_id, EventType.RISK_OBSERVED, event_payload, source=Origin.EXTERNAL_MONITOR
        )
        return True
    except Exception:
        # Fail-open: log and drop, never block
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
