"""SQLite storage engine.

Chosen defaults and why
-----------------------

* **WAL journal mode** — readers never block the writer, so `continuum inspect`
  can read a run while the agent is still working.
* **`synchronous=FULL`** — the whole point of this layer is surviving power
  loss. `NORMAL` can lose the last commits on a WAL crash, which would silently
  reintroduce the duplicate-work problem CONTINUUM exists to prevent. The cost
  is an fsync per append; correctness wins.
* **`foreign_keys=ON`** — events cannot reference a run that was never created.
* **`IMMEDIATE` transactions for writes** — takes the write lock up front, so a
  racing writer fails at BEGIN rather than halfway through a read-modify-write.

Sequence allocation is done inside the write transaction with a UNIQUE
constraint on ``(run_id, sequence)`` as the backstop. If two processes race,
one commits and the other hits the constraint and is reported as a
``ConcurrentWriteError`` — never a silent overwrite.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from continuum.events import Event, EventType, IntegrityReport, IntegrityViolation
from continuum.models import Origin, Run, RunStatus, SemanticState, StateCheckpoint, utcnow
from continuum.security.hashing import make_id
from continuum.state.versioning import state_fingerprint
from continuum.storage.base import (
    CheckpointNotFound,
    ConcurrentWriteError,
    CorruptedRecord,
    RunNotFound,
    SchemaVersionError,
    Storage,
)

__all__ = ["SQLiteStorage", "SCHEMA_VERSION"]

SCHEMA_VERSION = 2

_SCHEMA = """
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
"""


def _resolve_path(url_or_path: str | Path) -> str:
    """Accept ``sqlite:///file.db``, a plain path, or ``:memory:``.

    ``sqlite:///`` is the conventional form: the third slash begins an absolute
    path. Stripping it blindly would turn ``/var/db`` into ``var/db`` and open a
    file in the wrong place, so the leading slash is preserved.
    """
    raw = str(url_or_path)
    if raw.startswith("sqlite://"):
        # Only the scheme is removed. sqlite:///a.db -> /a.db (absolute),
        # sqlite://a.db -> a.db (relative). Stripping the third slash too would
        # silently turn an absolute path into a relative one.
        raw = raw[len("sqlite://") :]
    if raw in ("", "/"):
        return ":memory:"
    return raw


class SQLiteStorage(Storage):
    """Single-host durable storage. Safe for threads and for separate processes."""

    def __init__(self, url: str | Path = ":memory:", *, timeout: float = 30.0) -> None:
        self.path = _resolve_path(url)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=None,  # explicit transactions
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        cursor = self._connection
        if self.path != ":memory:":
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")

    def _migrate(self) -> None:
        # executescript() commits any open transaction, so schema creation runs
        # on its own rather than inside _write().
        with self._lock:
            self._connection.executescript(_SCHEMA)

        with self._write() as conn:
            row = conn.execute(
                "SELECT value FROM continuum_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO continuum_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                return
            found = int(row["value"])
            if found > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema v{found} was written by a newer CONTINUUM; "
                    f"this build understands v{SCHEMA_VERSION}"
                )
            if found < SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database schema v{found} was written by an older CONTINUUM; "
                    f"this build requires v{SCHEMA_VERSION}. No automatic migration "
                    f"is available: reset the database or open it with a compatible build."
                )

    # -- transactions ----------------------------------------------------- #

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction. Rolls back on any exception."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __del__(self) -> None:
        """Release the connection if the owner never closed it.

        A dropped handle should not leak an OS file descriptor. This is a
        safety net, not a substitute for ``close()`` or a ``with`` block:
        finalisation timing is not guaranteed, so durability still depends on
        commits, which are synchronous.
        """
        connection = getattr(self, "_connection", None)
        if connection is not None:
            with suppress(Exception):  # interpreter teardown can be hostile
                connection.close()

    # -- runs ------------------------------------------------------------- #

    def create_run(self, run: Run) -> Run:
        with self._write() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs(run_id, goal, status, created_at, updated_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id,
                        run.goal,
                        run.status.value,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        json.dumps(dict(run.metadata), sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrentWriteError(f"run {run.run_id!r} already exists") from exc
        return run

    def get_run(self, run_id: str) -> Run:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._row_to_run(row)

    def update_run(self, run: Run) -> Run:
        updated = run.touch()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE runs SET goal = ?, status = ?, updated_at = ?, metadata = ? "
                "WHERE run_id = ?",
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
            query += " LIMIT ?"
            params = (limit,)
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_active_run(self) -> Run | None:
        terminal = (
            RunStatus.COMPLETED.value,
            RunStatus.CRASHED.value,
            RunStatus.ABORTED.value,
            RunStatus.FAILED.value,
        )
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status NOT IN (?, ?, ?, ?) "
                "ORDER BY updated_at DESC, run_id DESC LIMIT 1",
                terminal,
            ).fetchall()
        return self._row_to_run(rows[0]) if rows else None

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        try:
            return Run(
                run_id=row["run_id"],
                goal=row["goal"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=json.loads(row["metadata"]),
            )
        except (ValidationError, json.JSONDecodeError) as exc:
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
        with self._write() as conn:
            self._require_run(conn, run_id)
            head = conn.execute(
                "SELECT sequence, hash FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            current = head["sequence"] if head else 0

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
            self._insert_event(conn, event)
        return event

    def append_sealed(self, event: Event) -> Event:
        with self._write() as conn:
            self._require_run(conn, event.run_id)
            head = conn.execute(
                "SELECT sequence, hash FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (event.run_id,),
            ).fetchone()
            expected_sequence = (head["sequence"] if head else 0) + 1
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
            self._insert_event(conn, event)
        return event

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, event: Event) -> None:
        try:
            conn.execute(
                "INSERT INTO events(run_id, sequence, event_id, type, timestamp, payload, "
                "causer_event_id, source, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        except sqlite3.IntegrityError as exc:
            raise ConcurrentWriteError(
                f"run {event.run_id!r} sequence {event.sequence} was taken by another writer"
            ) from exc

    @staticmethod
    def _require_run(conn: sqlite3.Connection, run_id: str) -> None:
        row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)

    def read_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        upto: int | None = None,
    ) -> Sequence[Event]:
        query = "SELECT * FROM events WHERE run_id = ? AND sequence > ?"
        params: list[Any] = [run_id, after_sequence]
        if upto is not None:
            query += " AND sequence <= ?"
            params.append(upto)
        query += " ORDER BY sequence ASC"
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_event(row) for row in rows]

    def last_sequence(self, run_id: str) -> int:
        with self._read() as conn:
            row = conn.execute(
                "SELECT MAX(sequence) AS seq FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["seq"]) if row and row["seq"] is not None else 0

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        try:
            return Event(
                event_id=row["event_id"],
                run_id=row["run_id"],
                sequence=row["sequence"],
                type=row["type"],
                timestamp=row["timestamp"],
                payload=json.loads(row["payload"]),
                causer_event_id=row["causer_event_id"],
                source=row["source"],
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            raise CorruptedRecord(
                f"event {row['event_id']!r} (run {row['run_id']!r}, seq {row['sequence']}) "
                f"failed to load: {exc}"
            ) from exc

    def verify_events(self, run_id: str) -> IntegrityReport:
        """Re-audit a persisted chain without loading it into an EventLog."""
        violations: list[IntegrityViolation] = []
        checked = 0
        last_good = 0
        intact = True
        prev_digest: str | None = None
        expected_sequence = 1

        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
            ).fetchall()

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
                        sequence=row["sequence"],
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

    # -- versions --------------------------------------------------------- #

    def put_version(self, state: SemanticState, *, reason: str = "") -> int:
        fingerprint = state_fingerprint(state)
        with self._write() as conn:
            self._require_run(conn, state.run_id)
            head = conn.execute(
                "SELECT version, fingerprint FROM versions WHERE run_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (state.run_id,),
            ).fetchone()

            if head is not None and head["fingerprint"] == fingerprint:
                return int(head["version"])  # unchanged: no new version

            version = (int(head["version"]) + 1) if head else 0
            stored = state.model_copy(update={"version": version})
            conn.execute(
                "INSERT INTO versions(run_id, version, fingerprint, prev_fingerprint, reason, "
                "created_at, state) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    state.run_id,
                    version,
                    fingerprint,
                    head["fingerprint"] if head else None,
                    reason,
                    utcnow().isoformat(),
                    stored.model_dump_json(),
                ),
            )
        return version

    def get_version(self, run_id: str, version: int) -> SemanticState:
        with self._read() as conn:
            row = conn.execute(
                "SELECT state, fingerprint FROM versions WHERE run_id = ? AND version = ?",
                (run_id, version),
            ).fetchone()
        if row is None:
            raise CheckpointNotFound(f"run {run_id!r} has no version {version}")
        return self._row_to_state(row, run_id, version)

    def latest_version(self, run_id: str) -> SemanticState | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT state, fingerprint, version FROM versions WHERE run_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_state(row, run_id, int(row["version"]))

    @staticmethod
    def _row_to_state(row: sqlite3.Row, run_id: str, version: int) -> SemanticState:
        try:
            state = SemanticState.model_validate_json(row["state"])
        except ValidationError as exc:
            raise CorruptedRecord(
                f"run {run_id!r} version {version} failed to load: {exc}"
            ) from exc
        if state_fingerprint(state) != row["fingerprint"]:
            raise CorruptedRecord(
                f"run {run_id!r} version {version}: stored fingerprint does not match content"
            )
        return state

    def list_versions(self, run_id: str) -> Sequence[int]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT version FROM versions WHERE run_id = ? ORDER BY version ASC", (run_id,)
            ).fetchall()
        return [int(row["version"]) for row in rows]

    # -- checkpoints ------------------------------------------------------ #

    def put_checkpoint(self, checkpoint: StateCheckpoint) -> StateCheckpoint:
        sealed = checkpoint if checkpoint.verify() else checkpoint.sealed()
        with self._write() as conn:
            self._require_run(conn, sealed.run_id)
            try:
                conn.execute(
                    "INSERT INTO checkpoints(checkpoint_id, run_id, version, trigger, "
                    "created_at, integrity_hash, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sealed.checkpoint_id,
                        sealed.run_id,
                        sealed.version,
                        sealed.trigger,
                        sealed.created_at.isoformat(),
                        sealed.integrity_hash,
                        sealed.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrentWriteError(
                    f"checkpoint {sealed.checkpoint_id!r} already exists"
                ) from exc
        return sealed

    def get_checkpoint(self, checkpoint_id: str) -> StateCheckpoint:
        with self._read() as conn:
            row = conn.execute(
                "SELECT body FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        if row is None:
            raise CheckpointNotFound(f"no such checkpoint: {checkpoint_id!r}")
        return self._row_to_checkpoint(row)

    def latest_checkpoint(self, run_id: str) -> StateCheckpoint | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT body FROM checkpoints WHERE run_id = ? "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._row_to_checkpoint(row) if row else None

    def list_checkpoints(self, run_id: str) -> Sequence[StateCheckpoint]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT body FROM checkpoints WHERE run_id = ? ORDER BY version ASC", (run_id,)
            ).fetchall()
        return [self._row_to_checkpoint(row) for row in rows]

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> StateCheckpoint:
        try:
            checkpoint = StateCheckpoint.model_validate_json(row["body"])
        except ValidationError as exc:
            raise CorruptedRecord(f"checkpoint failed to load: {exc}") from exc
        if not checkpoint.verify():
            raise CorruptedRecord(
                f"checkpoint {checkpoint.checkpoint_id!r}: integrity hash does not match content"
            )
        return checkpoint
