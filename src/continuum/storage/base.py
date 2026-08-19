"""Storage interface and its honest guarantees.

A storage engine persists four things: runs, events, state versions and
checkpoints. Everything else in CONTINUUM is derived from them.

Guarantees a conforming engine must provide
-------------------------------------------

* **Append-only events.** ``append_event`` assigns the next sequence for a run
  and links the hash chain. An event that already exists is never rewritten.
* **Atomic sequence allocation.** Two writers racing to append to the same run
  must not receive the same sequence number. One wins; the other retries or
  fails loudly. Silent overwrite is a correctness bug, not a performance
  trade-off.
* **Durability on commit.** Once ``append_event`` returns, the event survives
  process death.

Guarantees deliberately *not* claimed
-------------------------------------

* **Not exactly-once.** A crash between an external side effect and its ledger
  write leaves the ledger behind reality. That gap is what the action ledger
  (Phase 6) reconciles; storage cannot close it alone.
* **Not distributed.** The SQLite engine is single-host. Multi-writer
  coordination across machines needs PostgreSQL (Phase 3, optional) and is out
  of scope for the MVP.
* **Not encrypted at rest.** Checkpoints hold task state, never credentials.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any

from continuum.events import Event, EventType, IntegrityReport
from continuum.models import Origin, Run, SemanticState, StateCheckpoint

__all__ = [
    "Storage",
    "StorageError",
    "ConcurrentWriteError",
    "RunNotFound",
    "CheckpointNotFound",
    "CorruptedRecord",
    "SchemaVersionError",
]


class StorageError(RuntimeError):
    """Base class for storage failures."""


class RunNotFound(StorageError, KeyError):
    """The requested run does not exist.

    Subclasses ``KeyError`` so ``except KeyError`` still catches it, but
    overrides ``__str__``: ``KeyError.__str__`` applies ``repr()`` to its
    message, which would surface to CLI users as ``"no such run: 'ghost'"``
    — quoted twice.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(f"no such run: {run_id!r}")
        self.run_id = run_id

    def __str__(self) -> str:
        return f"no such run: {self.run_id!r}"


class CheckpointNotFound(StorageError, KeyError):
    """The requested checkpoint or version does not exist.

    Overrides ``__str__`` for the same reason as ``RunNotFound``: inherited
    ``KeyError`` formatting would double-quote the message.
    """

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self.__class__.__name__


class ConcurrentWriteError(StorageError):
    """Another writer advanced the run first.

    Raised instead of silently overwriting. The caller should re-read and retry.
    """


class CorruptedRecord(StorageError):
    """A stored record failed validation or its integrity hash.

    Reading is refused rather than returning state that cannot be trusted.
    """


class SchemaVersionError(StorageError):
    """The database was written by an incompatible version of CONTINUUM."""


class Storage(ABC):
    """Durable backing store for runs, events, versions and checkpoints."""

    # -- lifecycle -------------------------------------------------------- #

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Storage:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- runs ------------------------------------------------------------- #

    @abstractmethod
    def create_run(self, run: Run) -> Run: ...

    @abstractmethod
    def get_run(self, run_id: str) -> Run: ...

    @abstractmethod
    def update_run(self, run: Run) -> Run: ...

    @abstractmethod
    def list_runs(self, *, limit: int | None = None) -> Sequence[Run]: ...

    @abstractmethod
    def get_active_run(self) -> Run | None:
        """Return the most recently active run that is not in a terminal state.

        A terminal run (completed, crashed, aborted, failed) is finished and must
        not be offered for resume. The rest are candidates for interruption:
        the one touched most recently is the one a new session should resume
        without the caller having to remember its id. Returns ``None`` when no
        such run exists.
        """

    # -- events ----------------------------------------------------------- #

    @abstractmethod
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
        """Append an event, assigning its sequence and chain link atomically.

        ``expected_sequence`` opts into optimistic concurrency: if the run has
        already advanced past it, ``ConcurrentWriteError`` is raised instead of
        appending.
        """

    @abstractmethod
    def read_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        upto: int | None = None,
    ) -> Sequence[Event]: ...

    @abstractmethod
    def last_sequence(self, run_id: str) -> int: ...

    @abstractmethod
    def verify_events(self, run_id: str) -> IntegrityReport: ...

    # -- state versions --------------------------------------------------- #

    @abstractmethod
    def put_version(self, state: SemanticState, *, reason: str = "") -> int:
        """Persist a state version. Returns the assigned version number."""

    @abstractmethod
    def get_version(self, run_id: str, version: int) -> SemanticState: ...

    @abstractmethod
    def latest_version(self, run_id: str) -> SemanticState | None: ...

    @abstractmethod
    def list_versions(self, run_id: str) -> Sequence[int]: ...

    # -- checkpoints ------------------------------------------------------ #

    @abstractmethod
    def put_checkpoint(self, checkpoint: StateCheckpoint) -> StateCheckpoint: ...

    @abstractmethod
    def get_checkpoint(self, checkpoint_id: str) -> StateCheckpoint: ...

    @abstractmethod
    def latest_checkpoint(self, run_id: str) -> StateCheckpoint | None: ...

    @abstractmethod
    def list_checkpoints(self, run_id: str) -> Sequence[StateCheckpoint]: ...

    # -- convenience ------------------------------------------------------ #

    def extend_events(self, events: Iterable[Event]) -> int:
        """Copy sealed events into this store, preserving their chain.

        Used for import/export and for tests that build a log in memory.
        """
        count = 0
        for event in events:
            self.append_sealed(event)
            count += 1
        return count

    @abstractmethod
    def append_sealed(self, event: Event) -> Event:
        """Append an already-sealed event, verifying it continues the chain."""
