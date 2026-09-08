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
from typing import Any, ClassVar

from continuum.events import Event, EventType, IntegrityReport
from continuum.models import Action, Origin, Run, SemanticState, StateCheckpoint

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
    message, which would surface to CLI users as ``"no such run: 'ghost'"``,
    quoted twice.
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

    #: True when the engine maintains the derived action index (issue #216)
    #: and implements :meth:`foreign_action`. Callers fall back to event-scan
    #: lookups when False, so the flag must reflect real capability.
    supports_action_index: ClassVar[bool] = False

    #: True when the engine maintains ``events_archive`` and implements
    #: :meth:`compact_run` (issue #239). Callers gate on this flag rather than
    #: catching NotImplementedError, mirroring :attr:`supports_action_index`.
    supports_compaction: ClassVar[bool] = False

    def compact_run(self, run_id: str, *, through_sequence: int | None = None) -> dict[str, int]:
        """Archive the pre-anchor prefix of a run's log (issue #239).

        Only meaningful on engines with ``events_archive``; callers check
        :attr:`supports_compaction` first, which is the capability contract.
        """
        raise NotImplementedError

    def read_archived_events(self, run_id: str) -> Sequence[Event]:
        """Read events moved into ``events_archive``, oldest first.

        Engines without an archive return an empty sequence, so a caller that
        wants "the whole recorded history" can concatenate this with
        :meth:`read_events` unconditionally. This is what keeps exactly-once
        action claims (and any other fold over history) intact across
        compaction: an archived fact is still a recorded fact.
        """
        del run_id
        return []

    def read_all_events(self, run_id: str) -> Sequence[Event]:
        """Full history including archived prefix, sorted by sequence.

        After compaction the live log holds only the anchor and tail; any
        provenance calculation that reads only live events would miss an
        archived ``EXTERNAL_AGENT`` fact and launder it to ``DETERMINISTIC``.
        Callers that compute ``derived_origin`` over a run's history must use
        this helper so min is honest. Authority enforcement, memory enumeration,
        forensic joins, and cross-run action scans likewise require full history:
        compaction moves facts but does not revoke their consequences. Checkpoint
        projection may intentionally read only the live tail instead.
        Callers folding the same history more than once should reuse the returned
        sequence within that operation instead of rescanning the archive.
        Sorted to keep hash chain order stable.
        """
        archived = list(self.read_archived_events(run_id))
        live = list(self.read_events(run_id))
        if not archived:
            return live
        if not live:
            return archived
        return tuple(sorted([*archived, *live], key=lambda e: e.sequence))

    def foreign_action(self, key: str, *, exclude_run: str) -> Action | None:
        """Newest action recorded under ``key`` outside ``exclude_run``.

        Only meaningful when ``supports_action_index`` is True; engines
        without an index leave the default, and callers scan event logs
        instead. Returns None both for "not found" and "no index", which is
        why callers must check the flag first.
        """
        del key, exclude_run
        return None

    def action_index_drift(self) -> int:
        """Count index rows disagreeing with the log. Index engines only.

        Callers must check :attr:`supports_action_index` first; engines
        without an index deliberately have no meaningful answer.
        """
        raise NotImplementedError

    def rebuild_action_index(self) -> int:
        """Recompute the index from the log; returns corrected rows.

        Same capability contract as :meth:`action_index_drift`.
        """
        raise NotImplementedError

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

    @staticmethod
    def require_usable_run_id(run: Run) -> None:
        """Refuse a blank run id where work enters storage.

        Enforced on the write path rather than on :class:`Run` itself, because a
        model-level constraint also runs on deserialization: one bad legacy row
        would then make ``list_runs`` and ``get_active_run`` raise, leaving an
        operator unable to even see the row in order to clean it up. Guarding the
        entry point stops new bad data without bricking existing databases.

        A blank id is not cosmetic. It is indistinguishable from "no run" at every
        boundary that takes one, it wins ``get_active_run`` and so silently
        becomes the run a fresh session is told to resume, and it renders guidance
        like ``continuum confirm `` with nothing after it.
        """
        if not run.run_id.strip():
            raise ValueError(
                "run_id must not be blank: a blank id is indistinguishable from "
                "'no run', and it wins get_active_run so a fresh session would be "
                "told to resume it instead of real work"
            )

    @abstractmethod
    def create_run(self, run: Run) -> Run: ...

    @abstractmethod
    def create_run_started(self, run: Run, *, source: Origin = Origin.DETERMINISTIC) -> Run:
        """Create a run and its ``RUN_STARTED`` event as one atomic write.

        The run row and its first event are two inserts but one fact: without
        the event the run cannot be projected, and without the row the event
        violates its foreign key. Writing them separately admits a half-created
        run that can be neither resumed nor deleted whenever the process dies
        between the two statements. Engines must commit both or neither.
        """

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
    def put_version(self, state: SemanticState, *, reason: str = "", force: bool = False) -> int:
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

    @abstractmethod
    def delete_checkpoint(self, checkpoint_id: str) -> None:
        """Remove a checkpoint by id.

        Callers (for example CheckpointManager.prune) are responsible for not
        deleting a checkpoint that a recovery decision still depends on; the
        store itself only refuses referential impossibilities.
        """

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
