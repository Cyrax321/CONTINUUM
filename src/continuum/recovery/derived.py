"""Non-amplification invariant for derived artifacts (issue #392)."""

from __future__ import annotations

from typing import Any

from continuum.events import Event
from continuum.models import Origin
from continuum.provenance_map import derived_provenance_for_events

__all__ = ["is_derived_unverified", "derived_label", "stamp_derived"]


def stamp_derived(payload: dict[str, Any], source_events: list[Event]) -> dict[str, Any]:
    derived = derived_provenance_for_events(source_events)
    stamped = dict(payload)
    stamped["derived_origin"] = derived.value
    return stamped


def derived_label(payload: dict[str, Any]) -> str:
    raw = payload.get("derived_origin")
    if raw is None:
        return "unverified (derived from unverified sources)"
    try:
        origin = Origin(raw)
    except ValueError:
        return "unverified (derived from unverified sources)"
    if origin.self_certified:
        return f"unverified (derived from {origin.value})"
    return f"derived from {origin.value}"


def is_derived_unverified(payload: dict[str, Any]) -> bool:
    raw = payload.get("derived_origin")
    if raw is None:
        return True
    try:
        origin = Origin(raw)
    except ValueError:
        return True
    return origin.self_certified
