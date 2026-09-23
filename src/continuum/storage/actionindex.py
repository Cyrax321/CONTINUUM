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
from datetime import UTC, datetime
from typing import Any

from continuum.events import EventType

__all__ = [
    "ACTION_EVENT_TYPES",
    "INDEX_DDL_SQLITE",
    "INDEX_DDL_POSTGRES",
    "index_entry_from_payload",
    "index_order_for",
]

#: The only event types that carry a ledger entry.
ACTION_EVENT_TYPES = (
    EventType.ACTION_RECORDED,
    EventType.ACTION_RECONCILED,
    EventType.ACTION_COMPENSATED,
)

INDEX_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS action_index (
    key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_seq INTEGER NOT NULL,
    action_json TEXT NOT NULL
)
"""

INDEX_DDL_SQLITE = INDEX_DDL_POSTGRES.replace("TEXT PRIMARY KEY", "TEXT PRIMARY KEY") + (
    "\nCREATE INDEX IF NOT EXISTS action_index_run ON action_index(run_id)\n"
)


def index_order_for(timestamp: str | datetime) -> int:
    """The number stored in ``action_index.updated_seq`` for an event (#1322).

    It is epoch microseconds of the event's own append time, so the value is a
    property of the event rather than of its position in a table. That is what
    makes the projection survive log maintenance: ``compact_run`` moves rows
    from ``events`` into ``events_archive`` and deletes the originals, which
    reassigns every rowid and ctid, and a re-opened store hands out fresh
    ``nextval`` numbers that no longer line up with the rows that consumed
    them. A number derived from the event's own timestamp is unchanged by any
    of it, because ``timestamp`` is copied into the archive verbatim, so the
    fold reproduces the exact number the incremental writer stored in every
    state of the store -- live, compacted, one run or many.

    Ties are possible at microsecond resolution: two appends in the same
    microsecond get the same number. That is fine, because the projection is
    one row per key and the fold breaks the tie by walking the merged stream
    in ``(timestamp, run_id, sequence)`` order, so the later sequence wins.
    """
    when = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(when.timestamp() * 1_000_000)


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
