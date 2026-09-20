"""Version pinning for observations and claims (issue #241).

Replay correctness needs environment pinning: which prompt version, tool
schema and policy produced a decision (Zylos durable-execution survey;
prompt-migration hazard in arXiv:2507.05573). CONTINUUM never computes these
itself - it stores caller-asserted hashes with EXTERNAL_AGENT provenance,
like every other claim, and surfaces drift on resume.

Allowed keys are deliberately a closed set so downstream consumers know what
they can diff. Values are short strings (hashes or ids), capped at 256 chars.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "ALLOWED_PINNING_KEYS",
    "latest_pinning",
    "normalize_pinning",
    "pinning_drift",
]

ALLOWED_PINNING_KEYS = (
    "prompt_sha256",
    "tool_schema_sha256",
    "model_id",
    "policy_version",
)

_MAX_VALUE_LEN = 256


def normalize_pinning(pinning: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate and canonicalise a caller-supplied pinning dict."""
    if pinning is None:
        return {}
    if not isinstance(pinning, Mapping):
        raise ValueError("pinning must be an object of key -> short string")
    out: dict[str, str] = {}
    for key, value in pinning.items():
        if key not in ALLOWED_PINNING_KEYS:
            raise ValueError(f"unknown pinning key {key!r}; allowed: {list(ALLOWED_PINNING_KEYS)}")
        if value is None:
            continue
        text = str(value)
        if not text:
            continue
        if len(text) > _MAX_VALUE_LEN:
            raise ValueError(
                f"pinning value for {key!r} exceeds {_MAX_VALUE_LEN} chars; "
                "store the hash, not the artefact"
            )
        out[key] = text
    return out


def pinning_drift(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> list[str]:
    """Human-readable lines describing what moved between two pinnings."""
    prev = normalize_pinning(previous)
    cur = normalize_pinning(current)
    lines: list[str] = []
    for key in sorted(set(prev) | set(cur)):
        old, new = prev.get(key), cur.get(key)
        if old is not None and new is not None and old != new:
            lines.append(f"pinning drift: {key} changed ({old[:16]}... -> {new[:16]}...)")
        elif new is not None and old is None:
            lines.append(f"pinning drift: {key} newly pinned ({new[:16]}...)")
        elif old is not None and new is None:
            lines.append(f"pinning drift: {key} unpinned (was {old[:16]}...)")
    return lines


def latest_pinning(events: Any) -> dict[str, str]:
    """Newest non-empty pinning recorded on ACTION_RECORDED events."""
    from continuum.events import EventType

    latest: dict[str, str] = {}
    for event in events:
        if event.type is not EventType.ACTION_RECORDED:
            continue
        raw = event.payload.get("pinning")
        if isinstance(raw, Mapping) and raw:
            latest = {k: str(v) for k, v in raw.items()}
    return latest
