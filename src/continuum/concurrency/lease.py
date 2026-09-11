"""Distributed run-locking so exactly one agent resumes a run.

CONTINUUM's recovery is safe to compute, but two agents pointed at the same run
could both decide they are the one to resume it and step on each other. A lease
coordinator hands out a short-lived, renewable claim on a run: whoever holds the
lease may proceed, and the claim expires on its own if the holder dies without
releasing it (the hard-kill scenario CONTINUUM is built for).

Two implementations share one contract:

* ``InMemoryLeaseCoordinator`` -- for a single process / tests.
* ``SQLiteLeaseCoordinator`` -- a dedicated sidecar database so separate
  processes (e.g. several ``continuum serve`` sidecars) coordinate through the
  filesystem the same way the event log does.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from continuum.models import utcnow

__all__ = [
    "DEFAULT_TTL",
    "LeaseError",
    "LeaseCoordinator",
    "InMemoryLeaseCoordinator",
    "SQLiteLeaseCoordinator",
]

#: Default lease lifetime. Short enough that a dead holder is noticed quickly,
#: long enough that a normal resume turn does not trip over its own expiry.
DEFAULT_TTL = timedelta(seconds=60)

#: A clock returning the current UTC time. Injectable so tests control expiry
#: without sleeping.
LeaseClock = Callable[[], datetime]


class LeaseError(RuntimeError):
    """A lease operation could not be completed."""


def _to_epoch(when: datetime) -> float:
    """Seconds since the Unix epoch, the comparable form we store."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


class LeaseCoordinator(ABC):
    """The contract every coordinator obeys.

    All times are UTC. A lease is held until its TTL elapses; an expired lease is
    treated as free and may be taken over by another holder.
    """

    @abstractmethod
    def acquire(self, run_id: str, holder_id: str, ttl: timedelta | None = None) -> bool:
        """Take the lease if it is free or expired, else return ``False``."""

    @abstractmethod
    def renew(self, run_id: str, holder_id: str, ttl: timedelta | None = None) -> bool:
        """Extend a lease held by ``holder_id``; return ``False`` if not held."""

    @abstractmethod
    def release(self, run_id: str, holder_id: str) -> None:
        """Drop the lease, but only if ``holder_id`` currently holds it."""

    @abstractmethod
    def holder(self, run_id: str) -> str | None:
        """The current holder, or ``None`` if free or expired."""

    def is_held(self, run_id: str, holder_id: str) -> bool:
        """Whether ``holder_id`` currently owns the live lease."""
        return self.holder(run_id) == holder_id

    def is_available(self, run_id: str) -> bool:
        """Whether the lease could be acquired right now."""
        return self.holder(run_id) is None


class InMemoryLeaseCoordinator(LeaseCoordinator):
    """Single-process coordinator backed by a dict.

    ``clock`` is injectable so tests can advance time without sleeping.
    """

    def __init__(self, clock: LeaseClock = utcnow, default_ttl: timedelta = DEFAULT_TTL) -> None:
        self._clock = clock
        self._default_ttl = default_ttl
        self._leases: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.RLock()

    def _expires(self, ttl: timedelta | None) -> datetime:
        return self._clock() + (ttl or self._default_ttl)

    def acquire(self, run_id: str, holder_id: str, ttl: timedelta | None = None) -> bool:
        """Acquire the run lease if it is unheld or expired.

        Returns ``True`` if the lease was granted to ``holder_id``, or ``False``
        if another holder currently retains a live lease. An omitted ``ttl`` falls
        back to the coordinator's default TTL. The expiry deadline is calculated
        from the injectable clock. Thread-safe via internal reentrant lock.
        """
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is not None and existing[1] > self._clock():
                return False
            self._leases[run_id] = (holder_id, self._expires(ttl))
            return True

    def renew(self, run_id: str, holder_id: str, ttl: timedelta | None = None) -> bool:
        """Extend the lifetime of an active lease held by ``holder_id``.

        Returns ``True`` if the lease was successfully extended, or ``False`` if
        the lease does not exist, is held by a different agent, or has already
        passed its expiry deadline. When successful, advances expiration to
        ``clock() + (ttl or default_ttl)``.
        """
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is None or existing[0] != holder_id or existing[1] <= self._clock():
                return False
            self._leases[run_id] = (holder_id, self._expires(ttl))
            return True

    def release(self, run_id: str, holder_id: str) -> None:
        """Release the lease on ``run_id`` if owned by ``holder_id``.

        A no-op if the lease is already free, expired, or currently held by
        another agent, preventing accidental eviction across competing holders.
        """
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is not None and existing[0] == holder_id:
                del self._leases[run_id]

    def holder(self, run_id: str) -> str | None:
        """Return the current active holder of ``run_id``, or ``None``.

        Evaluates the stored expiry timestamp against the current clock time;
        an expired lease is considered unheld and returns ``None``.
        """
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is None or existing[1] <= self._clock():
                return None
            return existing[0]


def _resolve(url_or_path: str | Path) -> str:
    raw = str(url_or_path)
    if raw.startswith("sqlite://"):
        raw = raw[len("sqlite://") :]
    if raw in ("", "/"):
        return ":memory:"
    return raw


class SQLiteLeaseCoordinator(LeaseCoordinator):
    """Process-crossing coordinator over a dedicated SQLite sidecar.

    A single database file is the rendezvous point for every process that might
    resume a run, exactly like the event store. Expiry is compared as a Unix
    epoch so the check is a plain numeric comparison regardless of timezone
    formatting.
    """

    def __init__(
        self,
        url: str | Path = ":memory:",
        clock: LeaseClock = utcnow,
        default_ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._clock = clock
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            _resolve(url), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leases (
                run_id    TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )

    def _now_epoch(self) -> float:
        return _to_epoch(self._clock())

    def acquire(self, run_id: str, holder_id: str, ttl: timedelta | None = None) -> bool:
        """Atomically take the lease in SQLite if free or expired.

        Executes an atomic insert-on-conflict update conditioned on the existing
        lease being expired (``expires_at <= now``). Returns ``True`` if
        ``holder_id`` acquired or took over the lease, or ``False`` if a
        competing process holds a live lease. An omitted ``ttl`` falls back to the
        default TTL.
        """
        ttl = ttl or self._default_ttl
        expires = self._now_epoch() + ttl.total_seconds()
        with self._lock:
            # Take over only if the row is absent or already expired.
            self._connection.execute(
                "INSERT INTO leases(run_id, holder_id, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE "
                "SET holder_id = excluded.holder_id, expires_at = excluded.expires_at "
                "WHERE leases.expires_at <= ?",
                (run_id, holder_id, expires, self._now_epoch()),
            )
            row = self._connection.execute(
                "SELECT holder_id FROM leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            return row is not None and row["holder_id"] == holder_id

    def _holder_from(self, row: Any) -> str | None:
        if row is None or row["expires_at"] <= self._now_epoch():
            return None
        return cast(str, row["holder_id"])

    def renew(self, run_id: str, holder_id: str, ttl: timedelta | None = None) -> bool:
        """Extend an unexpired lease in SQLite for ``holder_id``.

        Executes a conditional update requiring matching ``holder_id`` and
        ``expires_at > now``. Returns ``True`` if the row was updated, or
        ``False`` if the lease was missing, expired, or claimed by another holder.
        Advances expiry to ``now + (ttl or default_ttl)``.
        """
        ttl = ttl or self._default_ttl
        expires = self._now_epoch() + ttl.total_seconds()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE leases SET expires_at = ? "
                "WHERE run_id = ? AND holder_id = ? AND expires_at > ?",
                (expires, run_id, holder_id, self._now_epoch()),
            )
            return cursor.rowcount > 0

    def release(self, run_id: str, holder_id: str) -> None:
        """Delete the lease row in SQLite if owned by ``holder_id``.

        Safe no-op if the lease is already deleted, expired, or registered to a
        different holder, ensuring competing processes cannot clear each
        other's claims.
        """
        with self._lock:
            self._connection.execute(
                "DELETE FROM leases WHERE run_id = ? AND holder_id = ?",
                (run_id, holder_id),
            )

    def holder(self, run_id: str) -> str | None:
        """Query the current active holder for ``run_id`` from SQLite.

        Reads the lease row and checks the numeric Unix epoch timestamp against
        the current clock. Returns the holder identifier if active, or ``None``
        if no row exists or the lease has expired.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT holder_id, expires_at FROM leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._holder_from(row)

    def close(self) -> None:
        """Close the underlying SQLite database connection.

        Releases the open database handle. Subsequent operations on this
        coordinator instance will raise :class:`sqlite3.ProgrammingError`.
        """
        with self._lock:
            self._connection.close()

    def __del__(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            with suppress(Exception):
                connection.close()
