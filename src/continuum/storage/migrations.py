"""Forward-only schema migration for the SQLite storage engine.

CONTINUUM stamps every database with a ``schema_version`` in ``continuum_meta``
and refuses to open one it cannot understand. Historically that refusal was
fail-closed in *both* directions: a database written by an older build raised
``SchemaVersionError`` with no remedy but "reset the database". This module
replaces that with a forward-migration runner:

* A brand-new database is seeded with the current ``BASELINE_SCHEMA`` and
  recorded at ``SCHEMA_VERSION``.
* A database stamped with an *older* version is moved forward one version at a
  time, applying each registered migration, until it reaches
  ``SCHEMA_VERSION``. Every applied step is recorded in ``schema_migrations`` so
  the path a database took is auditable.
* A database stamped with a *newer* version (written by a build this one cannot
  downgrade to) still raises ``SchemaVersionError`` -- we never silently open a
  schema we do not understand. A gap with no registered migration also raises,
  so an unrecognized older shape fails closed rather than being guessed at.

All migrations are additive (``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE ...
ADD COLUMN``), which is what makes forward motion safe and replayable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from continuum.events import EventType
from continuum.storage.actionindex import index_entry_from_payload, index_order_for
from continuum.storage.base import SchemaVersionError

__all__ = [
    "SCHEMA_VERSION",
    "BASELINE_SCHEMA",
    "Migration",
    "MIGRATIONS",
    "migrate_schema",
    "schema_version_of",
]

#: The schema version this build produces and understands.
SCHEMA_VERSION = 6

#: The full, current schema applied to a brand-new database.
BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS continuum_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata   TEXT NOT NULL DEFAULT '{}',
    parent_run_id TEXT REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS runs_parent ON runs(parent_run_id);

CREATE TABLE IF NOT EXISTS events (
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    sequence        INTEGER NOT NULL,
    event_id        TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    payload         TEXT NOT NULL,
    causer_event_id TEXT,
    source          TEXT NOT NULL DEFAULT 'deterministic',
    prev_hash       TEXT,
    hash            TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS events_by_type ON events(run_id, type);

CREATE TABLE IF NOT EXISTS versions (
    run_id           TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    version          INTEGER NOT NULL,
    fingerprint      TEXT NOT NULL,
    prev_fingerprint TEXT,
    reason           TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    state            TEXT NOT NULL,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    trigger        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    integrity_hash TEXT NOT NULL,
    body           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS checkpoints_by_run ON checkpoints(run_id, version);
"""

_TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version   INTEGER NOT NULL,
    name      TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (version, name)
);

CREATE TABLE IF NOT EXISTS events_archive (
    run_id       TEXT NOT NULL,
    sequence     INTEGER NOT NULL,
    event_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    causer_event_id TEXT,
    source       TEXT NOT NULL,
    prev_hash    TEXT,
    hash         TEXT NOT NULL,
    archived_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS lg_checkpoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id     TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_id     TEXT,
    type          TEXT NOT NULL,
    checkpoint    BLOB NOT NULL,
    meta_type     TEXT NOT NULL DEFAULT 'json',
    metadata      BLOB,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (thread_id, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS lg_checkpoints_thread ON lg_checkpoints(thread_id, id DESC);

CREATE TABLE IF NOT EXISTS lg_writes (
    thread_id     TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    type          TEXT NOT NULL,
    blob          BLOB NOT NULL,
    UNIQUE (thread_id, checkpoint_id, task_id, idx)
);

CREATE TABLE IF NOT EXISTS action_index (
    key         TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    action_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_seq INTEGER NOT NULL,
    action_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS action_index_run ON action_index(run_id);
"""


class Migration:
    """A single forward step from version ``version - 1`` to ``version``.

    ``up`` runs against an already-open connection in autocommit mode and must be
    additive so it is safe to apply and to reason about. It is either a script
    string or a callable receiving the connection; the callable form is for a
    step that cannot be expressed in SQL, which today means any step that has
    to produce the action index's ordering number (see
    :func:`_backfill_action_index_v3`).
    """

    def __init__(
        self, version: int, name: str, up: str | Callable[[sqlite3.Connection], None]
    ) -> None:
        self.version = version
        self.name = name
        self.up = up


def _up_v2() -> str:
    """Introduce the ``versions`` table and per-event provenance columns.

    v2 split durable state projection from the event log: state versions became
    their own table, and events gained a ``source`` (who asserted the fact) and
    ``prev_hash`` (chain linkage) so provenance and tamper-evidence are first
    class. Both are additive on a v1 database.
    """
    return """
    CREATE TABLE IF NOT EXISTS versions (
        run_id           TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        version          INTEGER NOT NULL,
        fingerprint      TEXT NOT NULL,
        prev_fingerprint TEXT,
        reason           TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL,
        state            TEXT NOT NULL,
        PRIMARY KEY (run_id, version)
    );

    ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'deterministic';
    ALTER TABLE events ADD COLUMN prev_hash TEXT;

    CREATE INDEX IF NOT EXISTS checkpoints_by_run ON checkpoints(run_id, version);
    """


def _up_v3() -> str:
    """The DDL half of the ``action_index`` projection (issue #216).

    Cross-run idempotency lookups previously folded every run's full event
    log, O(total logged events) per unscoped claim miss. The index is a
    derived projection of the ``ACTION_*`` events: one row per ledger key,
    last write per key wins (matching the fold), maintained incrementally by
    the storage engines and rebuildable at any time because the log remains
    the source of truth. The backfill seeds it from existing events so a v2
    database opens with correct lookups.

    Only the DDL lives here: seeding the rows needs
    :func:`_backfill_action_index_v3`, because SQLite cannot compute the
    projection's ordering number in SQL (``strftime('%s', ts)`` truncates to
    the second, ``strftime('%f', ts)`` to the millisecond).
    """
    return """
    CREATE TABLE IF NOT EXISTS action_index (
        key         TEXT PRIMARY KEY,
        run_id      TEXT NOT NULL,
        action_id   TEXT NOT NULL,
        status      TEXT NOT NULL,
        updated_seq INTEGER NOT NULL,
        action_json TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS action_index_run ON action_index(run_id);
    """


def _up_v3_apply(conn: sqlite3.Connection) -> None:
    """Create the projection, then seed it from the log (issue #216)."""
    conn.executescript(_up_v3())
    _backfill_action_index_v3(conn)


def _backfill_action_index_v3(conn: sqlite3.Connection) -> None:
    """Seed ``action_index`` from the existing log on the fold's own scale.

    The ordering number is a property of each event, not of its row (see
    :func:`continuum.storage.actionindex.index_order_for`), and it must be the
    same number the fold would derive from that row's ``timestamp`` -- that is
    what makes ``action_index_drift`` read zero on a freshly upgraded store.
    Walking the merged stream in ``(timestamp, run_id, sequence)`` order and
    letting later writes overwrite earlier ones reproduces the fold's
    last-write-per-key exactly.
    """
    rows = conn.execute(
        "SELECT timestamp, type, payload FROM events "
        "WHERE type IN ('ACTION_RECORDED', 'ACTION_RECONCILED', 'ACTION_COMPENSATED') "
        "ORDER BY timestamp, run_id, sequence"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        entry = index_entry_from_payload(EventType(row["type"]), payload)
        if entry is None:
            continue
        key, run_id, action_id, status, action_json = entry
        conn.execute(
            "INSERT OR REPLACE INTO action_index(key, run_id, action_id, status, "
            "updated_seq, action_json) VALUES (?, ?, ?, ?, ?, ?)",
            (key, run_id, action_id, status, index_order_for(row["timestamp"]), action_json),
        )


def _up_v4() -> str:
    """Add LangGraph checkpointer tables (issue #236).

    CONTINUUM implements LangGraph's BaseCheckpointSaver over this store, so
    production LangGraph apps keep their native persistence API while gaining
    provenance-tagged events. Two tables: snapshot rows per checkpoint and
    pending-write rows per task. Additive and empty on upgrade.
    """
    return """
    CREATE TABLE IF NOT EXISTS lg_checkpoints (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id     TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        parent_id     TEXT,
        type          TEXT NOT NULL,
        checkpoint    BLOB NOT NULL,
        meta_type     TEXT NOT NULL DEFAULT 'json',
        metadata      BLOB,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (thread_id, checkpoint_id)
    );

    CREATE INDEX IF NOT EXISTS lg_checkpoints_thread ON lg_checkpoints(thread_id, id DESC);

    CREATE TABLE IF NOT EXISTS lg_writes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id     TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        task_id       TEXT NOT NULL,
        idx           INTEGER NOT NULL,
        channel       TEXT NOT NULL,
        type          TEXT NOT NULL,
        blob          BLOB NOT NULL,
        UNIQUE (thread_id, checkpoint_id, task_id, idx)
    );
"""


def _up_v5() -> str:
    """Add the event-archive table for compaction (issue #239).

    `continuum compact` moves the pre-anchor prefix of a run's event log into
    ``events_archive`` verbatim (sequence numbers preserved) and appends an
    EVENT_LOG_ANCHORED marker to the live chain. The live log stays
    append-only; the archive is the historical record.
    """
    return """
    CREATE TABLE IF NOT EXISTS events_archive (
        run_id       TEXT NOT NULL,
        sequence     INTEGER NOT NULL,
        event_id     TEXT NOT NULL,
        type         TEXT NOT NULL,
        timestamp    TEXT NOT NULL,
        payload      TEXT NOT NULL,
        causer_event_id TEXT,
        source       TEXT NOT NULL,
        prev_hash    TEXT,
        hash         TEXT NOT NULL,
        archived_at  TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (run_id, sequence)
    );
"""


def _up_v6() -> str:
    """Add runs.parent_run_id for multi-agent hierarchies (issue #243)."""
    return """
    ALTER TABLE runs ADD COLUMN parent_run_id TEXT REFERENCES runs(run_id);

    CREATE INDEX IF NOT EXISTS runs_parent ON runs(parent_run_id);
"""


#: Forward migrations, keyed by the version they *produce*.
MIGRATIONS: dict[int, Migration] = {
    2: Migration(version=2, name="add_versions_table_and_event_provenance", up=_up_v2()),
    3: Migration(version=3, name="add_action_index_projection", up=_up_v3_apply),
    4: Migration(version=4, name="add_langgraph_checkpoint_tables", up=_up_v4()),
    5: Migration(version=5, name="add_events_archive", up=_up_v5()),
    6: Migration(version=6, name="add_runs_parent_column", up=_up_v6()),
}


def _ensure_tracking(conn: sqlite3.Connection) -> None:
    """Create the bookkeeping tables without touching the user schema."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS continuum_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.executescript(_TRACKING_SCHEMA)


def schema_version_of(conn: sqlite3.Connection) -> int | None:
    """Read the stamped schema version, or ``None`` for a fresh database."""
    row = conn.execute("SELECT value FROM continuum_meta WHERE key = 'schema_version'").fetchone()
    return None if row is None else int(row[0])


def _stamp_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO continuum_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def _record_migration(conn: sqlite3.Connection, version: int, name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, datetime.now(UTC).isoformat()),
    )


def migrate_schema(conn: sqlite3.Connection) -> int:
    """Bring ``conn``'s schema up to ``SCHEMA_VERSION``.

    Returns the resulting version. Raises ``SchemaVersionError`` for a database
    written by a newer build, or for an older shape with no registered path
    forward. The caller is responsible for any locking; this function assumes
    autocommit (``isolation_level=None``), which ``SQLiteStorage`` configures.
    """
    _ensure_tracking(conn)

    found = schema_version_of(conn)
    if found is None:
        # Greenfield: seed with the current schema and record the baseline.
        conn.executescript(BASELINE_SCHEMA)
        _stamp_version(conn, SCHEMA_VERSION)
        _record_migration(conn, SCHEMA_VERSION, "baseline")
        return SCHEMA_VERSION

    if found > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema v{found} was written by a newer CONTINUUM; "
            f"this build understands v{SCHEMA_VERSION}"
        )

    if found == SCHEMA_VERSION:
        return SCHEMA_VERSION

    # Forward-migrate one version at a time, recording each step.
    version = found
    while version < SCHEMA_VERSION:
        target = version + 1
        migration = MIGRATIONS.get(target)
        if migration is None:
            raise SchemaVersionError(
                f"database schema v{version} is older than supported "
                f"v{SCHEMA_VERSION}, and no automatic migration to v{target} "
                f"is available; open it with a compatible build or reset it."
            )
        if callable(migration.up):
            migration.up(conn)
        else:
            conn.executescript(migration.up)
        _stamp_version(conn, target)
        _record_migration(conn, target, migration.name)
        version = target

    return version
