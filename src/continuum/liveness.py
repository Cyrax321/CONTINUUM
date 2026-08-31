"""Cadence contracts and read-path liveness evaluation (issue #302).

A cadence contract declares the maximum expected wall-clock interval between
ledger appends. Evaluation is purely comparative: ``now - last_event_ts``
against a threshold, with an injected clock so tests never sleep. Breach is
advisory only, it never moves the recovery mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CadenceContract",
    "DEFAULT_LIVENESS_PATH",
    "DEFAULT_MAX_SILENCE_SECONDS",
    "DEFAULT_PHASE_SCOPES",
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

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the JSON shape stored on disk."""
        return {
            "max_silence_seconds": self.max_silence_seconds,
            "phase_scopes": dict(self.phase_scopes),
        }
