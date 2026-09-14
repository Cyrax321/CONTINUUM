"""Tool-to-action correlation identifiers (issue #785).

A correlation ID is optional, bounded, caller-supplied linkage between a
ledger action attempt and the host-observed tool events that performed it.
It is evidence linkage, not proof an external effect occurred: a match must
never settle an uncertain action, bypass reconciliation, or override the
most-cautious recovery result.

It is distinct from three nearby concepts:

- idempotency keys identify the external effect for exactly-once
  enforcement and carry scope and privacy semantics of their own,
- ``caused_by`` edges declare causal derivation between decisions,
  findings, evidence, and actions,
- external-effect confirmation comes from probes and reconciliation,
  never from the presence of a matching string.

Rules:

- absent (None or missing) means the client could not emit metadata;
  current behavior is preserved,
- present must be 1-64 chars of ``[A-Za-z0-9_-]``; anything else raises
  ValueError (fail closed for any decision that would rely on it),
- uniqueness is caller responsibility (UUID recommended); duplicates are
  reported as ambiguous rather than guessed,
- the identifier is part of the event payload and therefore hash-covered,
  surviving projection, compaction, replay, and interchange.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CORRELATION_ID_MAX_LEN",
    "CORRELATION_ID_PATTERN",
    "normalize_correlation_id",
    "correlate_events",
]

#: Upper bound on correlation identifier length.
CORRELATION_ID_MAX_LEN = 64

#: Allowed characters: letters, digits, dash, underscore.
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_correlation_id(value: Any) -> str | None:
    """Validate an optional correlation identifier.

    Returns None when value is None (absent). Raises ValueError when present
    but malformed, so callers fail closed instead of recording an ambiguous
    link.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"correlation_id must be a string, got {type(value).__name__}")
    if not value:
        return None
    if len(value) > CORRELATION_ID_MAX_LEN:
        raise ValueError(
            f"correlation_id must be 1-{CORRELATION_ID_MAX_LEN} chars, got {len(value)}"
        )
    if not CORRELATION_ID_PATTERN.match(value):
        raise ValueError(
            "correlation_id must match [A-Za-z0-9_-]{1,64}, "
            f"got {value!r}"
        )
    return value


_TOOL_TYPES = frozenset({"TOOL_CALLED", "TOOL_COMPLETED", "TOOL_FAILED"})
_ACTION_TYPES = frozenset({"ACTION_RECORDED", "ACTION_RECONCILED", "ACTION_COMPENSATED"})


def correlate_events(events: Any) -> dict[str, Any]:
    """Group action and tool events by correlation identifier, read-only.

    Returns a dict with ``chains`` (correlation_id to ordered event refs),
    ``unmatched_tool`` (tool events with no identifier or no matching
    action), and ``ambiguous`` (identifiers claimed by more than one
    action attempt). Never mutates, never settles anything.
    """
    chains: dict[str, list[dict[str, Any]]] = {}
    action_ids: dict[str, int] = {}
    unmatched: list[dict[str, Any]] = []

    for event in events:
        event_type = getattr(event, "type", None)
        type_name = getattr(event_type, "value", str(event_type))
        payload = getattr(event, "payload", {}) or {}
        correlation = payload.get("correlation_id")
        ref = {
            "sequence": getattr(event, "sequence", None),
            "event_id": getattr(event, "event_id", None),
            "type": type_name,
            "correlation_id": correlation,
        }
        if type_name in _ACTION_TYPES:
            if correlation is None:
                continue
            action_ids[correlation] = action_ids.get(correlation, 0) + 1
            chains.setdefault(str(correlation), []).append(ref)
        elif type_name in _TOOL_TYPES:
            if correlation is None:
                unmatched.append(ref)
            else:
                chains.setdefault(str(correlation), []).append(ref)
    for refs in chains.values():
        refs.sort(key=lambda r: (r["sequence"] is None, r["sequence"]))

    ambiguous = sorted([cid for cid, count in action_ids.items() if count > 1])
    matched_ids = set(action_ids)
    for cid in list(chains):
        if cid not in matched_ids:
            for ref in chains.pop(cid):
                unmatched.append(ref)
    unmatched.sort(key=lambda r: (r["sequence"] is None, r["sequence"]))
    return {"chains": chains, "unmatched_tool": unmatched, "ambiguous": ambiguous}
