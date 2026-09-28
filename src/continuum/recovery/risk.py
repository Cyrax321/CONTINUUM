"""Risk-informed recovery policy (issue #303).

Risks arrive as RISK_OBSERVED events with Origin.EXTERNAL_MONITOR.
They are hash-chained, provenance-marked, and never self-certifying.
Ingestion is fail-open: malformed payloads are dropped and logged,
never blocking the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from continuum.events import EventType
from continuum.models import Origin, RecoveryMode, RiskObservedPayload
from continuum.storage.base import Storage

__all__ = [
    "ingest_risk",
    "RiskObservedPayload",
    "RiskPayload",
    "DEFAULT_RISK_POLICY",
    "DEFAULT_RISK_POLICY_PATH",
    "RiskPolicy",
    "load_risk_policy",
    "evaluate_risk",
    "is_more_conservative",
    "ingest_risk_json_line",
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
