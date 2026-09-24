"""Shared maintenance logic for the action index (issue #216).

The action index is a derived projection: one row per ledger key, rebuilt
from ``ACTION_*`` events, maintained incrementally inside the same
transaction that appends the event. The event log remains the source of
truth; the index exists so cross-run idempotency lookups are an indexed
read instead of folding every run's full log.

Both storage engines share :data:`ACTION_EVENT_TYPES` and
:func:`index_entry_from_payload` so incremental writes, backfills and
rebuilds cannot drift apart in what they accept.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from continuum.events import EventType

__all__ = [
    "ACTION_EVENT_TYPES",
    "index_entry_from_payload",
]

#: The only event types that carry a ledger entry.
ACTION_EVENT_TYPES = (
    EventType.ACTION_RECORDED,
    EventType.ACTION_RECONCILED,
    EventType.ACTION_COMPENSATED,
)


def index_entry_from_payload(
    event_type: EventType, payload: Mapping[str, Any]
) -> tuple[str, str, str, str, str] | None:
    """Extract ``(key, run_id_in_action, action_id, status, action_json)``.

    Returns None for anything the index does not track. The embedded Action
    record is the authority for identity; a malformed payload yields None
    rather than a half-row, because the index must never disagree with the
    log silently.
    """
    if event_type not in ACTION_EVENT_TYPES:
        return None
    key = payload.get("key")
    action_payload = payload.get("action")
    if not isinstance(key, str) or not key or not isinstance(action_payload, Mapping):
        return None
    try:
        return (
            key,
            str(action_payload.get("run_id", "")),
            str(action_payload.get("action_id", "")),
            str(action_payload.get("status", "")),
            json.dumps(dict(action_payload), sort_keys=True),
        )
    except (TypeError, ValueError):
        return None
