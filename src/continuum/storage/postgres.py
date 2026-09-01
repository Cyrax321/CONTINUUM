"""PostgreSQL storage engine (B2.3).

Shares the exact contract and hash-chain semantics of ``SQLiteStorage`` so the
two are interchangeable behind ``Storage``. It exists for the multi-process,
multi-host case SQLite cannot serve: several ``continuum serve`` sidecars or
agents pointing at one durable store.

Implementation notes
--------------------
* **Driver.** Synchronous ``psycopg`` (the plan's recommendation), matching the
  existing sync ``Storage`` surface. Pulled in via the optional ``[postgres]``
  extra so the core install stays lean.
* **JSON.** Stored as ``TEXT`` and round-tripped with ``json`` exactly like the
  SQLite engine, so payload/state/checkpoint bytes are identical across engines.
* **Concurrency.** Sequence allocation relies on the ``UNIQUE`` constraint on
  ``(run_id, sequence)`` (and ``event_id``), the same backstop SQLite uses; a
  racing writer hits the constraint and is reported as ``ConcurrentWriteError``
  rather than silently overwriting.
* **Connection.** One autocommit connection guarded by a lock, mirroring the
  single-connection SQLite design.

The engine is exercised by ``tests/test_storage_postgres.py``, which skips
cleanly when ``CONTINUUM_TEST_POSTGRES_DSN`` is unset or ``psycopg`` is absent,
so it runs for real in CI against a Postgres service.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from continuum.events import Event, EventType, IntegrityReport, IntegrityViolation
from continuum.models import (
    TERMINAL_RUN_STATUS_VALUES,
    Action,
    Origin,
    Run,
    SemanticState,
    StateCheckpoint,
    utcnow,
)
from continuum.security.hashing import make_id
from continuum.state.versioning import canonical_state_json, state_fingerprint
from continuum.storage.actionindex import index_entry_from_payload
from continuum.storage.base import (
    CheckpointNotFound,
    ConcurrentWriteError,
    CorruptedRecord,
    RunNotFound,
    Storage,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS continuum_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    parent_run_id TEXT REFERENCES runs(run_id),
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata   TEXT NOT NULL DEFAULT '{}'
);

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
    archived_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, sequence)
);

CREATE SEQUENCE IF NOT EXISTS action_index_ord_seq AS BIGINT;

CREATE TABLE IF NOT EXISTS lg_checkpoints (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_id     TEXT,
    type          TEXT NOT NULL,
    checkpoint    BYTEA NOT NULL,
    meta_type     TEXT NOT NULL DEFAULT 'json',
    metadata      BYTEA,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lg_checkpoints_thread ON lg_checkpoints(thread_id, id DESC);

CREATE TABLE IF NOT EXISTS lg_writes (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    type          TEXT NOT NULL,
    blob          BYTEA NOT NULL,
    UNIQUE (thread_id, checkpoint_id, task_id, idx)
);

CREATE TABLE IF NOT EXISTS action_index (
    key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_seq BIGINT NOT NULL DEFAULT nextval('action_index_ord_seq'),
    action_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS action_index_run ON action_index(run_id);
"""


def _require_psycopg() -> Any:
    """Import ``psycopg`` on demand; fail with a helpful message if absent."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "PostgreSQL storage requires the 'psycopg' extra: pip install continuum[postgres]"
        ) from exc
    return psycopg


class PostgresStorage(Storage):
    supports_action_index = True
    supports_compaction = True
    """Multi-process durable storage backed by PostgreSQL."""

    def __init__(self, url: str | Any, *, timeout: float = 30.0) -> None:
        psycopg = _require_psycopg()
        self._psycopg = psycopg
        from psycopg.rows import dict_row

        dsn = self._normalize_dsn(str(url))
        try:
            self._connection = psycopg.connect(
                dsn, autocommit=True, row_factory=dict_row, connect_timeout=int(timeout)
            )
        except Exception as exc:  # connection refused, auth, missing driver, etc.
            raise RuntimeError(f"could not connect to PostgreSQL at {dsn!r}: {exc}") from exc
        self._lock = threading.RLock()
        self._configure()
        self._create_schema()

    @staticmethod
    def _normalize_dsn(url: str) -> str:
        """Accept ``postgres://`` as an alias for ``postgresql://``."""
        if url.startswith("postgres://"):
            return "postgresql://" + url[len("postgres://") :]
        return url

    def _configure(self) -> None:
        # Foreign keys are honoured by default in Postgres; nothing to toggle.
        pass

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.execute(_SCHEMA)
            self._backfill_action_index()

    def _backfill_action_index(self) -> None:
        """Seed the projection from the log when it is empty (issue #216).

        The table is a derived projection: an empty index over existing
        ACTION_* events means the database predates the index or lost its
        rows, and rebuilding from events is always safe. Payload is stored as
        TEXT, so JSON functions apply directly.
        """
        has_events = self._connection.execute(
            "SELECT 1 FROM events WHERE type IN "
            "('ACTION_RECORDED', 'ACTION_RECONCILED', 'ACTION_COMPENSATED') LIMIT 1"
        ).fetchone()
        if has_events is None:
            return
        empty = self._connection.execute("SELECT 1 FROM action_index LIMIT 1").fetchone()
        if empty is not None:
            return
        self._connection.execute(
            """
            INSERT INTO action_index(key, run_id, action_id, status, updated_seq, action_json)
            SELECT e.payload::jsonb->>'key',
                   e.payload::jsonb->'action'->>'run_id',
                   e.payload::jsonb->'action'->>'action_id',
                   e.payload::jsonb->'action'->>'status',
                   nextval('action_index_ord_seq'),
                   (e.payload::jsonb->'action')::text
            FROM events e
            WHERE e.type IN ('ACTION_RECORDED', 'ACTION_RECONCILED', 'ACTION_COMPENSATED')
              AND json_extract(e.payload, '$.key') IS NOT NULL
              AND json_extract(e.payload, '$.action') IS NOT NULL
            ORDER BY ctid
            """
        )

    # -- transactions ----------------------------------------------------- #

    def _write(self) -> Any:
        return self._lock

    def _read(self) -> Any:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            with suppress(Exception):
                connection.close()

    # -- runs ------------------------------------------------------------- #

    def create_run(self, run: Run) -> Run:
        self.require_usable_run_id(run)
        with self._write():
            try:
                self._connection.execute(
                    "INSERT INTO runs(run_id, goal, status, created_at, updated_at, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        run.run_id,
                        run.goal,
                        run.status.value,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        json.dumps(dict(run.metadata), sort_keys=True),
                    ),
                )
            except self._psycopg.IntegrityError as exc:
                raise ConcurrentWriteError(f"run {run.run_id!r} already exists") from exc
        return run

    def create_run_started(self, run: Run, *, source: Origin = Origin.DETERMINISTIC) -> Run:
        """Create the run row and its first event in one transaction.

        The connection runs autocommit, so an explicit ``transaction()`` block
        is what makes the two inserts atomic here.
        """
        self.require_usable_run_id(run)
        with self._write(), self._connection.transaction():
            try:
                self._connection.execute(
                    "INSERT INTO runs(run_id, goal, status, created_at, updated_at, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        run.run_id,
                        run.goal,
                        run.status.value,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        json.dumps(dict(run.metadata), sort_keys=True),
                    ),
                )
            except self._psycopg.IntegrityError as exc:
                raise ConcurrentWriteError(f"run {run.run_id!r} already exists") from exc
            event = Event(
                event_id=make_id("event"),
                run_id=run.run_id,
                sequence=1,
                type=EventType.RUN_STARTED,
                timestamp=utcnow(),
                payload={"goal": run.goal},
                source=source,
                prev_hash=None,
            ).sealed()
            self._insert_event(event)
        return run

    def get_run(self, run_id: str) -> Run:
        with self._read():
            row = self._connection.execute(
                "SELECT * FROM runs WHERE run_id = %s", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._row_to_run(row)

    def update_run(self, run: Run) -> Run:
        updated = run.touch()
        with self._write():
            cursor = self._connection.execute(
                "UPDATE runs SET goal = %s, status = %s, updated_at = %s, metadata = %s "
                "WHERE run_id = %s",
                (
                    updated.goal,
                    updated.status.value,
                    updated.updated_at.isoformat(),
                    json.dumps(dict(updated.metadata), sort_keys=True),
                    updated.run_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RunNotFound(run.run_id)
        return updated

    def list_runs(self, *, limit: int | None = None) -> Sequence[Run]:
        query = "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT %s"
            params = (limit,)
        with self._read():
            rows = self._connection.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_active_run(self) -> Run | None:
        """The non-terminal run touched most recently.

        Mirrors :meth:`SQLiteStorage.get_active_run`, including the reason it
        cannot order on ``updated_at`` alone: ``append_event`` never bumps that
        column. ``GREATEST`` stands in for SQLite's two-argument ``MAX``, which
        Postgres reads as the aggregate.
        """
        terminal = TERMINAL_RUN_STATUS_VALUES
        with self._read():
            rows = self._connection.execute(
                "SELECT r.* FROM runs r WHERE r.status NOT IN (%s, %s, %s, %s) "
                "ORDER BY GREATEST(r.updated_at, COALESCE("
                "(SELECT MAX(e.timestamp) FROM events e WHERE e.run_id = r.run_id), "
                "r.updated_at)) DESC, r.run_id DESC LIMIT 1",
                terminal,
            ).fetchall()
        return self._row_to_run(rows[0]) if rows else None

    @staticmethod
    def _row_to_run(row: Any) -> Run:
        try:
            return Run(
                run_id=row["run_id"],
                goal=row["goal"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=json.loads(row["metadata"]),
            )
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            raise CorruptedRecord(f"run {row['run_id']!r} failed to load: {exc}") from exc

    # -- events ----------------------------------------------------------- #

    def append_event(
        self,
        run_id: str,
        type: EventType,
        payload: Mapping[str, Any] | None = None,
        *,
        causer_event_id: str | None = None,
        expected_sequence: int | None = None,
        source: Origin = Origin.DETERMINISTIC,
    ) -> Event:
        with self._write():
            event = self._append_chained(
                run_id,
                type,
                payload,
                causer_event_id=causer_event_id,
                expected_sequence=expected_sequence,
                source=source,
            )
        return event

    def _append_chained(
        self,
        run_id: str,
        type: EventType,
        payload: Mapping[str, Any] | None,
        *,
        causer_event_id: str | None,
        expected_sequence: int | None,
        source: Origin,
    ) -> Event:
        """Build and insert the next chained event on the open transaction.

        Callers that need an event appended atomically with other statements
        (compaction) reuse this instead of :meth:`append_event`, which would
        commit the marker in its own autocommit transaction.
        """
        self._require_run(self._connection, run_id)
        head = self._connection.execute(
            "SELECT sequence, hash FROM events WHERE run_id = %s ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        current = int(head["sequence"]) if head else 0

        if expected_sequence is not None and expected_sequence != current:
            raise ConcurrentWriteError(
                f"run {run_id!r} is at sequence {current}, caller expected {expected_sequence}"
            )

        event = Event(
            event_id=make_id("event"),
            run_id=run_id,
            sequence=current + 1,
            type=type,
            timestamp=utcnow(),
            payload=dict(payload or {}),
            causer_event_id=causer_event_id,
            source=source,
            prev_hash=head["hash"] if head else None,
        ).sealed()
        self._insert_event(event)
        return event

    def append_sealed(self, event: Event) -> Event:
        with self._write():
            self._require_run(self._connection, event.run_id)
            head = self._connection.execute(
                "SELECT sequence, hash FROM events WHERE run_id = %s "
                "ORDER BY sequence DESC LIMIT 1",
                (event.run_id,),
            ).fetchone()
            expected_sequence = (int(head["sequence"]) if head else 0) + 1
            expected_prev = head["hash"] if head else None

            if event.sequence != expected_sequence:
                raise ConcurrentWriteError(
                    f"run {event.run_id!r}: expected sequence {expected_sequence}, "
                    f"got {event.sequence}"
                )
            if event.prev_hash != expected_prev:
                raise CorruptedRecord(f"run {event.run_id!r} seq {event.sequence}: broken chain")
            if event.hash != event.digest():
                raise CorruptedRecord(
                    f"run {event.run_id!r} seq {event.sequence}: hash does not match content"
                )
            self._insert_event(event)
        return event

    def _insert_event(self, event: Event) -> None:
        try:
            self._connection.execute(
                "INSERT INTO events(run_id, sequence, event_id, type, timestamp, payload, "
                "causer_event_id, source, prev_hash, hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.run_id,
                    event.sequence,
                    event.event_id,
                    event.type.value,
                    event.timestamp.isoformat(),
                    json.dumps(dict(event.payload), sort_keys=True),
                    event.causer_event_id,
                    event.source.value,
                    event.prev_hash,
                    event.hash,
                ),
            )
        except self._psycopg.IntegrityError as exc:
            raise ConcurrentWriteError(
                f"run {event.run_id!r} sequence {event.sequence} was taken by another writer"
            ) from exc
        self._maintain_action_index(event)

    def _maintain_action_index(self, event: Event) -> None:
        """Upsert the projection row for an ACTION_* event, same txn (#216).

        updated_seq comes from a sequence so recency is global insertion
        order, matching the global last-write-per-key fold.
        """
        entry = index_entry_from_payload(event.type, dict(event.payload))
        if entry is None:
            return
        key, run_id, action_id, status, action_json = entry
        try:
            self._connection.execute(
                "INSERT INTO action_index(key, run_id, action_id, status, "
                "updated_seq, action_json) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET run_id = EXCLUDED.run_id, "
                "action_id = EXCLUDED.action_id, status = EXCLUDED.status, "
                "updated_seq = EXCLUDED.updated_seq, action_json = EXCLUDED.action_json",
                (key, run_id, action_id, status, self._next_index_ord(), action_json),
            )
        except self._psycopg.IntegrityError as exc:
            raise CorruptedRecord(
                f"action index maintenance failed for key {key[:12]}...: {exc}"
            ) from exc

    def _next_index_ord(self) -> int:
        row = self._connection.execute("SELECT nextval('action_index_ord_seq') AS v").fetchone()
        return int(row["v"])

    def compact_run(self, run_id: str, *, through_sequence: int | None = None) -> dict[str, int]:
        """Archive the pre-anchor prefix of a run's log (issue #239).

        A forced anchor checkpoint first records state at the boundary (its
        STATE_CHECKPOINTED marker joins the log like any event). Then one
        transaction appends the EVENT_LOG_ANCHORED marker and moves the prefix
        up to the boundary into ``events_archive`` verbatim. The single
        transaction matters: committing the marker separately would let a
        crash in between leave an anchored live log whose prefix never
        reached the archive, so verify would trust a genesis that was never
        earned. The connection runs in autocommit mode, so the explicit
        ``transaction()`` block is what makes the three writes atomic.
        """
        from continuum.checkpoint.manager import CheckpointManager

        lv = self.latest_version(run_id)
        head = self.last_sequence(run_id)
        needs_fresh_anchor = lv is None or through_sequence is not None or lv.source_sequence < head
        if needs_fresh_anchor:
            try:
                CheckpointManager(self).checkpoint(run_id, force_version=True)
            except Exception as exc:
                raise ValueError(f"run {run_id!r} could not be anchored: {exc}") from exc
            lv = self.latest_version(run_id)
        storage_version = lv
        if storage_version is None:
            raise ValueError(f"run {run_id!r} could not be anchored: no projectable state")
        through = (
            through_sequence
            if through_sequence is not None
            else min(storage_version.source_sequence, self.last_sequence(run_id))
        )
        if through < 1:
            raise ValueError("nothing to compact: anchor would be empty")

        with self._write(), self._connection.transaction():
            self._append_chained(
                run_id,
                EventType.EVENT_LOG_ANCHORED,
                {"anchored_through": through, "version": storage_version.version},
                causer_event_id=None,
                expected_sequence=None,
                source=Origin.DETERMINISTIC,
            )
            cur = self._connection.execute(
                "INSERT INTO events_archive"
                " (run_id, sequence, event_id, type, timestamp, payload,"
                "  causer_event_id, source, prev_hash, hash)"
                " SELECT run_id, sequence, event_id, type, timestamp, payload,"
                "        causer_event_id, source, prev_hash, hash"
                " FROM events WHERE run_id = %s AND sequence <= %s",
                (run_id, through),
            )
            archived = cur.rowcount
            self._connection.execute(
                "DELETE FROM events WHERE run_id = %s AND sequence <= %s",
                (run_id, through),
            )

        return {"archived": max(archived, 0)}

    def read_archived_events(self, run_id: str) -> Sequence[Event]:
        with self._read():
            rows = self._connection.execute(
                "SELECT * FROM events_archive WHERE run_id = %s ORDER BY sequence ASC", (run_id,)
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def foreign_action(self, key: str, *, exclude_run: str) -> Action | None:
        """Indexed cross-run ledger lookup (issue #216)."""
        with self._read():
            row = self._connection.execute(
                "SELECT action_json FROM action_index WHERE key = %s AND run_id != %s "
                "ORDER BY updated_seq DESC LIMIT 1",
                (key, exclude_run),
            ).fetchone()
        if row is None:
            return None
        try:
            return Action.model_validate(json.loads(row["action_json"]))
        except (ValueError, TypeError) as exc:
            raise CorruptedRecord(
                f"action index row for key {key[:12]}... failed to load: {exc}"
            ) from exc

    def rebuild_action_index(self) -> int:
        """Recompute the whole index from the log (global key space)."""
        canonical = self._canonical_index_rows()
        with self._write():
            self._connection.execute("DELETE FROM action_index")
            # psycopg's Connection has no executemany; the cursor does.
            with self._connection.cursor() as cur:
                cur.executemany(
                    "INSERT INTO action_index(key, run_id, action_id, status, "
                    "updated_seq, action_json) VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (key, entry[1], entry[2], entry[3], seq, entry[4])
                        for key, (entry, seq) in canonical.items()
                    ],
                )
        return 0

    def action_index_drift(self) -> int:
        """Count index rows that disagree with the log. Read-only.

        Store-wide by design, mirroring the SQLite engine: keys live in one
        namespace, so a run-scoped comparison would falsely flag rows owned
        by another run's later write of the same key.
        """
        expected = {
            key: (seq, entry[3]) for key, (entry, seq) in self._canonical_index_rows().items()
        }
        with self._read():
            stored = {
                r["key"]: (int(r["updated_seq"]), r["status"])
                for r in self._connection.execute(
                    "SELECT key, updated_seq, status FROM action_index"
                ).fetchall()
            }
        extra = set(stored) - set(expected)
        changed = sum(1 for k, val in expected.items() if stored.get(k) != val)
        return len(extra) + changed

    def _canonical_index_rows(self) -> dict[str, tuple[tuple[str, str, str, str, str], int]]:
        """Fold every run's action events; global last-write-per-key wins.

        Compacted history (#239) folds too, archive first and live second:
        everything in ``events_archive`` predates every live row of its run,
        so folding the two tables in one shared stream would let an archived
        action claimed long ago outrank a newer live write of the same key
        (they number their rows independently). Archived rows receive
        negative order positions below every possible live value, oldest
        first, so last-write-per-key stays true after compaction.
        """
        with self._read():
            archived = self._connection.execute(
                "SELECT type, payload FROM events_archive ORDER BY ctid"
            ).fetchall()
            rows = self._connection.execute(
                "SELECT type, payload FROM events ORDER BY ctid"
            ).fetchall()
        canonical: dict[str, tuple[tuple[str, str, str, str, str], int]] = {}
        offset = len(archived)
        for i, row in enumerate([*archived, *rows]):
            payload = (
                row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            )
            entry = index_entry_from_payload(EventType(row["type"]), payload)
            if entry is not None:
                canonical[entry[0]] = (entry, i if i >= offset else i - offset)
        return canonical

    @staticmethod
    def _require_run(conn: Any, run_id: str) -> None:
        row = conn.execute("SELECT 1 FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)

    def read_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        upto: int | None = None,
    ) -> Sequence[Event]:
        query = "SELECT * FROM events WHERE run_id = %s AND sequence > %s"
        params: list[Any] = [run_id, after_sequence]
        if upto is not None:
            query += " AND sequence <= %s"
            params.append(upto)
        query += " ORDER BY sequence ASC"
        with self._read():
            rows = self._connection.execute(query, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def last_sequence(self, run_id: str) -> int:
        with self._read():
            row = self._connection.execute(
                "SELECT MAX(sequence) AS seq FROM events WHERE run_id = %s", (run_id,)
            ).fetchone()
        return int(row["seq"]) if row and row["seq"] is not None else 0

    @staticmethod
    def _row_to_event(row: Any) -> Event:
        try:
            return Event(
                event_id=row["event_id"],
                run_id=row["run_id"],
                sequence=int(row["sequence"]),
                type=row["type"],
                timestamp=row["timestamp"],
                payload=json.loads(row["payload"]),
                causer_event_id=row["causer_event_id"],
                source=row["source"],
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            raise CorruptedRecord(
                f"event {row['event_id']!r} (run {row['run_id']!r}, seq {row['sequence']}) "
                f"failed to load: {exc}"
            ) from exc

    def verify_events(self, run_id: str) -> IntegrityReport:
        """Re-audit a persisted chain without loading it into an EventLog.

        For a compacted run (#239) the walk resumes at the archive boundary:
        the newest ``events_archive`` row supplies the expected ``prev_hash``
        and sequence of the first live event, and every archived row itself
        is re-digested and chain-linked. An anchored log therefore verifies
        only while its archived prefix is intact; removing the boundary
        events or editing history in the archive fails here instead of
        minting a fresh genesis out of whatever live rows survive.
        """
        violations: list[IntegrityViolation] = []
        checked = 0
        last_good = 0
        intact = True
        prev_digest: str | None = None
        expected_sequence = 1
        archive_edge: tuple[int, str] | None = None

        with self._read():
            rows = self._connection.execute(
                "SELECT * FROM events WHERE run_id = %s ORDER BY sequence ASC", (run_id,)
            ).fetchall()
            # Gate on either signal: a surviving anchor marks a compacted run,
            # but if that row itself was deleted the archive must still be
            # audited rather than silently escaping the walk.
            has_archive = (
                self._connection.execute(
                    "SELECT 1 FROM events_archive WHERE run_id = %s LIMIT 1", (run_id,)
                ).fetchone()
                is not None
            )
            if has_archive or any(r["type"] == "EVENT_LOG_ANCHORED" for r in rows):
                archive_violations, archive_edge = self._audit_archive(run_id)
                violations.extend(archive_violations)
                if archive_violations:
                    intact = False

        if archive_edge is not None:
            # The live chain must pick up exactly where the archive ends.
            prev_digest = archive_edge[1]
            expected_sequence = archive_edge[0] + 1

        for row in rows:
            checked += 1
            healthy = True
            try:
                event = self._row_to_event(row)
            except CorruptedRecord as exc:
                violations.append(
                    IntegrityViolation(
                        kind="UNREADABLE_RECORD",
                        run_id=run_id,
                        sequence=int(row["sequence"]),
                        event_id=row["event_id"],
                        detail=str(exc),
                    )
                )
                intact = False
                prev_digest = None
                expected_sequence = int(row["sequence"]) + 1
                continue

            digest = event.digest()
            if event.sequence != expected_sequence:
                healthy = False
                violations.append(
                    IntegrityViolation(
                        kind="SEQUENCE_GAP",
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        detail=f"expected sequence {expected_sequence}",
                    )
                )
            if event.hash != digest:
                healthy = False
                violations.append(
                    IntegrityViolation(
                        kind="TAMPERED_CONTENT",
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        detail="stored hash does not match recomputed digest",
                    )
                )
            if event.prev_hash != prev_digest:
                healthy = False
                violations.append(
                    IntegrityViolation(
                        kind="BROKEN_CHAIN",
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        detail=(
                            f"prev_hash {event.prev_hash!r} does not match "
                            f"predecessor digest {prev_digest!r}"
                        ),
                    )
                )

            if healthy and intact:
                last_good = event.sequence
            else:
                intact = False
            prev_digest = digest
            expected_sequence = event.sequence + 1

        return IntegrityReport(
            ok=not violations,
            checked=checked,
            violations=violations,
            trusted_through={run_id: last_good},
        )

    def _audit_archive(
        self, run_id: str
    ) -> tuple[list[IntegrityViolation], tuple[int, str] | None]:
        """Deep-audit one run's archived prefix (issue #239).

        The archive holds the run's verbatim beginning, so it can be held to
        the full genesis standard: sequence 1 with no predecessor, unbroken
        sequencing and hash linkage throughout, and every stored hash equal
        to the recomputed digest. Returns the violations found plus the
        ``(sequence, hash)`` edge the live chain must continue from, or
        ``None`` when nothing is archived.
        """
        violations: list[IntegrityViolation] = []
        edge: tuple[int, str] | None = None
        prev_hash: str | None = None
        expected_sequence = 1

        rows = self._connection.execute(
            "SELECT * FROM events_archive WHERE run_id = %s ORDER BY sequence ASC", (run_id,)
        ).fetchall()
        for row in rows:
            try:
                event = self._row_to_event(row)
            except CorruptedRecord as exc:
                violations.append(
                    IntegrityViolation(
                        kind="UNREADABLE_RECORD",
                        run_id=run_id,
                        sequence=int(row["sequence"]),
                        event_id=row["event_id"],
                        detail=f"archived row unreadable: {exc}",
                    )
                )
                prev_hash = None
                expected_sequence = int(row["sequence"]) + 1
                edge = None
                continue

            if event.sequence != expected_sequence:
                violations.append(
                    IntegrityViolation(
                        kind="SEQUENCE_GAP",
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        detail=f"archived: expected sequence {expected_sequence}",
                    )
                )
            if event.hash != event.digest():
                violations.append(
                    IntegrityViolation(
                        kind="TAMPERED_CONTENT",
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        detail="archived: stored hash does not match recomputed digest",
                    )
                )
            if event.prev_hash != prev_hash:
                violations.append(
                    IntegrityViolation(
                        kind="BROKEN_CHAIN",
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        detail=(
                            f"archived: prev_hash {event.prev_hash!r} does not match "
                            f"predecessor digest {prev_hash!r}"
                        ),
                    )
                )
            prev_hash = event.hash
            expected_sequence = event.sequence + 1
            edge = (event.sequence, event.hash) if event.hash is not None else None

        return violations, edge

    # -- state versions --------------------------------------------------- #

    def put_version(self, state: SemanticState, *, reason: str = "", force: bool = False) -> int:
        fingerprint = state_fingerprint(state)
        with self._write():
            self._require_run(self._connection, state.run_id)
            head = self._connection.execute(
                "SELECT version, fingerprint FROM versions WHERE run_id = %s "
                "ORDER BY version DESC LIMIT 1",
                (state.run_id,),
            ).fetchone()

            if head is not None and head["fingerprint"] == fingerprint and not force:
                return int(head["version"])  # unchanged: no new version

            version = (int(head["version"]) + 1) if head else 0
            stored = state.model_copy(update={"version": version})
            self._connection.execute(
                "INSERT INTO versions(run_id, version, fingerprint, prev_fingerprint, reason, "
                "created_at, state) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    state.run_id,
                    version,
                    fingerprint,
                    head["fingerprint"] if head else None,
                    reason,
                    utcnow().isoformat(),
                    canonical_state_json(stored),
                ),
            )
        return version

    def get_version(self, run_id: str, version: int) -> SemanticState:
        with self._read():
            row = self._connection.execute(
                "SELECT state, fingerprint FROM versions WHERE run_id = %s AND version = %s",
                (run_id, version),
            ).fetchone()
        if row is None:
            raise CheckpointNotFound(f"run {run_id!r} has no version {version}")
        return self._row_to_state(row, run_id, version)

    def latest_version(self, run_id: str) -> SemanticState | None:
        with self._read():
            row = self._connection.execute(
                "SELECT state, fingerprint, version FROM versions WHERE run_id = %s "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_state(row, run_id, int(row["version"]))

    @staticmethod
    def _row_to_state(row: Any, run_id: str, version: int) -> SemanticState:
        try:
            state = SemanticState.model_validate_json(row["state"])
        except ValueError as exc:
            raise CorruptedRecord(
                f"run {run_id!r} version {version} failed to load: {exc}"
            ) from exc
        if state_fingerprint(state) != row["fingerprint"]:
            raise CorruptedRecord(
                f"run {run_id!r} version {version}: stored fingerprint does not match content"
            )
        return state

    def list_versions(self, run_id: str) -> Sequence[int]:
        with self._read():
            rows = self._connection.execute(
                "SELECT version FROM versions WHERE run_id = %s ORDER BY version ASC", (run_id,)
            ).fetchall()
        return [int(row["version"]) for row in rows]

    # -- checkpoints ------------------------------------------------------ #

    def put_checkpoint(self, checkpoint: StateCheckpoint) -> StateCheckpoint:
        sealed = checkpoint if checkpoint.verify() else checkpoint.sealed()
        with self._write():
            self._require_run(self._connection, sealed.run_id)
            try:
                self._connection.execute(
                    "INSERT INTO checkpoints(checkpoint_id, run_id, version, trigger, "
                    "created_at, integrity_hash, body) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        sealed.checkpoint_id,
                        sealed.run_id,
                        sealed.version,
                        sealed.trigger,
                        sealed.created_at.isoformat(),
                        sealed.integrity_hash,
                        sealed.canonical_json(),
                    ),
                )
            except self._psycopg.IntegrityError as exc:
                raise ConcurrentWriteError(
                    f"checkpoint {sealed.checkpoint_id!r} already exists"
                ) from exc
        return sealed

    def get_checkpoint(self, checkpoint_id: str) -> StateCheckpoint:
        with self._read():
            row = self._connection.execute(
                "SELECT body FROM checkpoints WHERE checkpoint_id = %s", (checkpoint_id,)
            ).fetchone()
        if row is None:
            raise CheckpointNotFound(f"no such checkpoint: {checkpoint_id!r}")
        return self._row_to_checkpoint(row)

    def latest_checkpoint(self, run_id: str) -> StateCheckpoint | None:
        with self._read():
            row = self._connection.execute(
                "SELECT body FROM checkpoints WHERE run_id = %s "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._row_to_checkpoint(row) if row else None

    def list_checkpoints(self, run_id: str) -> Sequence[StateCheckpoint]:
        with self._read():
            rows = self._connection.execute(
                "SELECT body FROM checkpoints WHERE run_id = %s ORDER BY version ASC", (run_id,)
            ).fetchall()
        return [self._row_to_checkpoint(row) for row in rows]

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        with self._write():
            self._connection.execute(
                "DELETE FROM checkpoints WHERE checkpoint_id = %s", (checkpoint_id,)
            )

    @staticmethod
    def _row_to_checkpoint(row: Any) -> StateCheckpoint:
        try:
            checkpoint = StateCheckpoint.model_validate_json(row["body"])
        except ValueError as exc:
            raise CorruptedRecord(f"checkpoint failed to load: {exc}") from exc
        if not checkpoint.verify():
            raise CorruptedRecord(
                f"checkpoint {checkpoint.checkpoint_id!r}: integrity hash does not match content"
            )
        return checkpoint
