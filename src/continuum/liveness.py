"""Cadence contracts and read-path liveness evaluation (issue #302).

A cadence contract declares the maximum expected wall-clock interval between
ledger appends. Evaluation is purely comparative: ``now - last_event_ts``
against a threshold, with an injected clock so tests never sleep. Breach is
advisory only, it never moves the recovery mode.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CadenceContract",
    "LivenessResult",
    "DEFAULT_LIVENESS_PATH",
    "DEFAULT_MAX_SILENCE_SECONDS",
    "DEFAULT_PHASE_SCOPES",
    "evaluate",
    "load_cadence_contract",
    "silence_seconds",
]

DEFAULT_MAX_SILENCE_SECONDS: int = 3600
DEFAULT_PHASE_SCOPES: dict[str, int] = {"open_claim": 600, "otherwise": 3600}
DEFAULT_LIVENESS_PATH: Path = Path(".continuum/liveness.json")


class CadenceContract(BaseModel):
    """Per-run cadence contract, operator-editable.

    ``max_silence_seconds`` is the default threshold when phase does not
    apply. ``phase_scopes`` overrides it per phase, for example while a tool
    claim is open.

    The file ``.continuum/liveness.json`` may contain either form:

    ``{"max_silence_seconds": 3600, "phase_scopes": {"open_claim": 600, "otherwise": 3600}}``
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_silence_seconds: int = Field(default=DEFAULT_MAX_SILENCE_SECONDS, ge=1)
    phase_scopes: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_PHASE_SCOPES))

    @field_validator("phase_scopes")
    @classmethod
    def _scopes_positive(cls, value: dict[str, int]) -> dict[str, int]:
        for key, seconds in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("phase_scopes keys must be non-empty strings")
            if not isinstance(seconds, int) or seconds < 1:
                raise ValueError(f"phase_scopes[{key!r}] must be an int >= 1")
        return dict(value)

    def threshold_for(self, has_open_claim: bool) -> int:
        """Return the threshold for the current phase.

        When ``has_open_claim`` is True the ``open_claim`` scope applies,
        otherwise the ``otherwise`` scope. Falls back to ``max_silence_seconds``
        when the expected scope key is absent, so a partial file remains valid.
        """
        if has_open_claim:
            return int(self.phase_scopes.get("open_claim", self.max_silence_seconds))
        return int(self.phase_scopes.get("otherwise", self.max_silence_seconds))

    def evaluate(
        self,
        now: datetime,
        last_event_ts: datetime | None,
        *,
        has_open_claim: bool = False,
    ) -> LivenessResult:
        """Evaluate liveness with an injected clock.

        ``now`` and ``last_event_ts`` must be timezone-aware UTC when present.
        When ``last_event_ts`` is None the run has no events and is not
        considered breached. ``has_open_claim`` selects the phase scope.
        """
        return evaluate(now, last_event_ts, contract=self, has_open_claim=has_open_claim)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the JSON shape stored on disk."""
        return {
            "max_silence_seconds": self.max_silence_seconds,
            "phase_scopes": dict(self.phase_scopes),
        }


class LivenessResult(BaseModel):
    """Result of a read-path liveness check, advisory only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    breached: bool
    silence_seconds: float | None
    threshold_seconds: int
    phase: str
    now: datetime
    last_event_ts: datetime | None
    has_open_claim: bool


def evaluate(
    now: datetime,
    last_event_ts: datetime | None,
    *,
    contract: CadenceContract | None = None,
    has_open_claim: bool = False,
) -> LivenessResult:
    """Compute ``now - last_event_ts`` vs contract threshold.

    Pure function with injected clock, no sleeps. Breach is advisory.
    """
    cfg = contract or CadenceContract()
    threshold = cfg.threshold_for(has_open_claim)
    phase = "open_claim" if has_open_claim else "otherwise"
    if last_event_ts is None:
        return LivenessResult(
            breached=False,
            silence_seconds=None,
            threshold_seconds=threshold,
            phase=phase,
            now=now,
            last_event_ts=None,
            has_open_claim=has_open_claim,
        )
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if last_event_ts.tzinfo is None:
        last_event_ts = last_event_ts.replace(tzinfo=UTC)
    silence = (now - last_event_ts).total_seconds()
    breached = silence > threshold if silence >= 0 else False
    return LivenessResult(
        breached=breached,
        silence_seconds=float(silence),
        threshold_seconds=threshold,
        phase=phase,
        now=now,
        last_event_ts=last_event_ts,
        has_open_claim=has_open_claim,
    )


def silence_seconds(now: datetime, last_event_ts: datetime | None) -> float | None:
    """Return ``now - last_event_ts`` in seconds, or None when no events."""
    if last_event_ts is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if last_event_ts.tzinfo is None:
        last_event_ts = last_event_ts.replace(tzinfo=UTC)
    return (now - last_event_ts).total_seconds()


def load_cadence_contract(path: Path | None = None) -> CadenceContract:
    """Load contract from ``path`` or the default location.

    When the file does not exist the defaults are returned. When it exists
    it must be valid JSON matching the CadenceContract shape.
    """
    target = path or DEFAULT_LIVENESS_PATH
    if not target.exists():
        return CadenceContract()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid liveness contract {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"liveness contract {target} must be a JSON object")
    return CadenceContract.model_validate(data)
