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
from continuum.storage.migrations import SCHEMA_VERSION, migrate_schema

__all__ = ["SQLiteStorage", "SCHEMA_VERSION"]


def _maintain_action_index(conn: sqlite3.Connection, event: Event, order_seq: int) -> None:
    """Upsert the index row for an action event, inside the caller's txn.

    Runs in the same IMMEDIATE transaction as the event insert, so the index
    can never commit ahead of or behind the log. Malformed payloads are
    skipped: the fold ignores them too, so rebuild agrees.
    """
    entry = index_entry_from_payload(event.type, dict(event.payload))
    if entry is None:
        return
    key, run_id, action_id, status, action_json = entry
    conn.execute(
        "INSERT OR REPLACE INTO action_index(key, run_id, action_id, status, "
        "updated_seq, action_json) VALUES (?, ?, ?, ?, ?, ?)",
        (key, run_id, action_id, status, order_seq, action_json),
    )


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
    supports_action_index = True
    supports_compaction = True
    """Single-host durable storage. Safe for threads and for separate processes."""

    def __init__(self, url: str | Path = ":memory:", *, timeout: float = 30.0) -> None:
        self.path = _resolve_path(url)
        if self.path != ":memory:":
            raw = str(url)
            if raw.startswith("sqlite://"):
                with suppress(OSError):
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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
        # Forward-migrate (or seed) the schema under the write lock. The runner
        # assumes autocommit, which _configure selects, and commits any open
        # transaction itself via executescript.
        with self._lock:
            migrate_schema(self._connection)

    # -- transactions ----------------------------------------------------- #

    def _live_connection(self) -> sqlite3.Connection:
        """The open connection, or a clear error saying it was closed.

        ``close`` sets ``_connection`` to None so it can be called more than once
        (#320). Without this check every later operation failed with
        ``AttributeError: 'NoneType' object has no attribute 'execute'``, which
        names a symptom rather than the mistake. sqlite3's own wording is the
        thing worth preserving: use-after-close is a caller bug, and the message
        should say which bug.
        """
        # Annotated because getattr with a default is typed Any, and returning
        # Any from here would erase the connection type for every caller.
        connection: sqlite3.Connection | None = getattr(self, "_connection", None)
        if connection is None:
            raise sqlite3.ProgrammingError(
                "Cannot operate on a closed database. This SQLiteStorage was "
                "closed; open a new handle rather than reusing a closed one."
            )
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction. Rolls back on any exception."""
        with self._lock:
            connection = self._live_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._live_connection()

    def close(self) -> None:
        with self._lock:
            conn = getattr(self, "_connection", None)
            if conn is None:
                return
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                # Already closed — safe to call close() more than once.
                pass
            finally:
                self._connection = None  # type: ignore[assignment]

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
        self.require_usable_run_id(run)
        with self._write() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs(run_id, goal, status, created_at, updated_at, metadata, parent_run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id,
                        run.goal,
                        run.status.value,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        json.dumps(dict(run.metadata), sort_keys=True),
                        run.parent_run_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrentWriteError(f"run {run.run_id!r} already exists") from exc
        return run

    def create_run_started(self, run: Run, *, source: Origin = Origin.DETERMINISTIC) -> Run:
        """Create the run row and its first event in one transaction."""
        self.require_usable_run_id(run)
        with self._write() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs(run_id, goal, status, created_at, updated_at, metadata, parent_run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id,
                        run.goal,
                        run.status.value,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        json.dumps(dict(run.metadata), sort_keys=True),
                        run.parent_run_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
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
            self._insert_event(conn, event)
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
        """The non-terminal run touched most recently.

        Ordered by real last activity, not by ``runs.updated_at`` alone:
        ``append_event`` does not bump that column, so a run that has taken
        fifty events since it was created still carries its creation
        timestamp. Ordering on it returned whichever non-terminal run was
        *created* last, which on a machine that had run the all-features tour
        was the empty ``-fork`` demo artifact rather than the session's real
        work. Every caller that resolves "the active run" for the operator
        (``briefing``, ``gate``, ``observe``) inherited that answer, so a
        resumed session was briefed on the wrong run and reported the real one
        as missing.

        The event timestamp is coalesced to ``updated_at`` so a run with no
        events yet still sorts by when it was created, and ``updated_at``
        still participates via ``MAX`` so an explicit ``update_run`` (a status
        change, say) counts as activity even when it appends nothing.
        """
        terminal = TERMINAL_RUN_STATUS_VALUES
        with self._read() as conn:
            rows = conn.execute(
                "SELECT r.* FROM runs r WHERE r.status NOT IN (?, ?, ?, ?) "
                "ORDER BY MAX(r.updated_at, COALESCE("
                "(SELECT MAX(e.timestamp) FROM events e WHERE e.run_id = r.run_id), "
                "r.updated_at)) DESC, r.run_id DESC LIMIT 1",
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
                parent_run_id=row["parent_run_id"],
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
            event = self._append_chained(
                conn,
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
        conn: sqlite3.Connection,
        run_id: str,
        type: EventType,
        payload: Mapping[str, Any] | None,
        *,
        causer_event_id: str | None,
        expected_sequence: int | None,
        source: Origin,
    ) -> Event:
        """Build and insert the next chained event on an open write txn.

        Callers that need an event appended atomically with other statements
        (compaction) reuse this instead of :meth:`append_event`, which would
        try to nest a second transaction.
        """
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
            cursor = conn.execute(
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
        _maintain_action_index(conn, event, int(cursor.lastrowid or 0))

    def compact_run(self, run_id: str, *, through_sequence: int | None = None) -> dict[str, int]:
        """Archive the pre-anchor prefix of a run's log (issue #239).

        A forced anchor checkpoint first records state at the boundary (its
        STATE_CHECKPOINTED marker joins the log like any event). Then one
        transaction appends the EVENT_LOG_ANCHORED marker and moves the prefix
        up to the boundary into ``events_archive`` verbatim. The single
        transaction matters: committing the marker separately would let a
        crash in between leave an anchored live log whose prefix never
        reached the archive, so verify would trust a genesis that was never
        earned.
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

        with self._write() as conn:
            self._append_chained(
                conn,
                run_id,
                EventType.EVENT_LOG_ANCHORED,
                {"anchored_through": through, "version": storage_version.version},
                causer_event_id=None,
                expected_sequence=None,
                source=Origin.DETERMINISTIC,
            )
            cur = conn.execute(
                "INSERT INTO events_archive"
                " (run_id, sequence, event_id, type, timestamp, payload,"
                "  causer_event_id, source, prev_hash, hash)"
                " SELECT run_id, sequence, event_id, type, timestamp, payload,"
                "        causer_event_id, source, prev_hash, hash"
                " FROM events WHERE run_id = ? AND sequence <= ?",
                (run_id, through),
            )
            archived = cur.rowcount
            conn.execute(
                "DELETE FROM events WHERE run_id = ? AND sequence <= ?",
                (run_id, through),
            )

        return {"archived": max(archived, 0)}

    def read_archived_events(self, run_id: str) -> Sequence[Event]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM events_archive WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def foreign_action(self, key: str, *, exclude_run: str) -> Action | None:
        """Indexed cross-run ledger lookup (issue #216).

        O(log n) via the primary key instead of folding every run's events.
        The newest row wins, matching the fold's last-write-per-key rule.
        """
        with self._read() as conn:
            row = conn.execute(
                "SELECT action_json FROM action_index WHERE key = ? AND run_id != ? "
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

    def action_index_drift(self) -> int:
        """Count index rows that disagree with the event log. Read-only.

        The projection is keyed globally, so drift is a store-wide property:
        a run-scoped comparison would falsely flag rows owned by another
        run's later write of the same key.
        """
        expected = {
            key: (seq, entry[3]) for key, (entry, seq) in self._canonical_index_rows().items()
        }
        with self._read() as conn:
            stored = {
                r["key"]: (r["updated_seq"], r["status"])
                for r in conn.execute("SELECT key, updated_seq, status FROM action_index")
            }
        extra = set(stored) - set(expected)
        changed = sum(1 for k, val in expected.items() if stored.get(k) != val)
        return len(extra) + changed

    def rebuild_action_index(self) -> int:
        """Recompute the whole index from the log; returns corrected rows.

        Always global by design: keys live in one store-wide namespace, so a
        per-run rewrite could collide with another run's legitimate row of
        the same key. A correction is any key whose stored row was missing,
        stale or spurious.
        """
        canonical = self._canonical_index_rows()
        with self._write() as conn:
            before = {
                r["key"]: (r["updated_seq"], r["status"])
                for r in conn.execute("SELECT key, updated_seq, status FROM action_index")
            }
            conn.execute("DELETE FROM action_index")
            conn.executemany(
                "INSERT OR REPLACE INTO action_index(key, run_id, action_id, status, "
                "updated_seq, action_json) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (key, entry[1], entry[2], entry[3], seq, entry[4])
                    for key, (entry, seq) in canonical.items()
                ],
            )
        corrections = sum(
            1
            for k, val in ((k, (seq, entry[3])) for k, (entry, seq) in canonical.items())
            if before.get(k) != val
        )
        corrections += len(set(before) - set(canonical))
        return corrections

    def _canonical_index_rows(self) -> dict[str, tuple[tuple[str, str, str, str, str], int]]:
        """Fold the log into ``{key: ((entry...), order_seq)}``, last write wins.

        Compacted history (#239) folds too, archive first and live second:
        everything in ``events_archive`` predates every live row of its run,
        so folding the two tables in one shared stream would let an archived
        action claimed long ago outrank a newer live write of the same key
        (they number their rows independently). Live rows keep their
        insertion rowid, matching incremental index maintenance exactly;
        archived rows receive negative order positions below every possible
        rowid, oldest first, so last-write-per-key stays true after
        compaction while uncompacted stores fold identically to before.
        """
        with self._read() as conn:
            archived = conn.execute(
                "SELECT type, payload FROM events_archive ORDER BY rowid"
            ).fetchall()
            rows = conn.execute(
                "SELECT rowid AS rid, type, payload FROM events ORDER BY rowid"
            ).fetchall()
        canonical: dict[str, tuple[tuple[str, str, str, str, str], int]] = {}
        offset = len(archived)
        for position, row in enumerate([*archived, *rows]):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            entry = index_entry_from_payload(EventType(row["type"]), payload)
            if entry is not None:
                order = int(row["rid"]) if position >= offset else position - offset
                canonical[entry[0]] = (entry, order)
        return canonical

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

        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
            ).fetchall()
            # Gate on either signal: a surviving anchor marks a compacted run,
            # but if that row itself was deleted the archive must still be
            # audited rather than silently escaping the walk.
            has_archive = (
                conn.execute(
                    "SELECT 1 FROM events_archive WHERE run_id = ? LIMIT 1", (run_id,)
                ).fetchone()
                is not None
            )
            if has_archive or any(r["type"] == "EVENT_LOG_ANCHORED" for r in rows):
                archive_violations, archive_edge = self._audit_archive(conn, run_id)
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

    @classmethod
    def _audit_archive(
        cls, conn: sqlite3.Connection, run_id: str
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

        rows = conn.execute(
            "SELECT * FROM events_archive WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
        ).fetchall()
        for row in rows:
            try:
                event = cls._row_to_event(row)
            except CorruptedRecord as exc:
                violations.append(
                    IntegrityViolation(
                        kind="UNREADABLE_RECORD",
                        run_id=run_id,
                        sequence=row["sequence"],
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

    # -- versions --------------------------------------------------------- #

    def put_version(self, state: SemanticState, *, reason: str = "", force: bool = False) -> int:
        fingerprint = state_fingerprint(state)
        with self._write() as conn:
            self._require_run(conn, state.run_id)
            head = conn.execute(
                "SELECT version, fingerprint FROM versions WHERE run_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (state.run_id,),
            ).fetchone()

            if head is not None and head["fingerprint"] == fingerprint and not force:
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
                    canonical_state_json(stored),
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
                        sealed.canonical_json(),
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

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))

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
