"""The recovery ledger.

A recovery ledger is the durable, auditable record of every recovery decision
CONTINUUM made for a run. The event log already proves what happened; the ledger
proves what was *decided* and *permitted*, and lets a later reader check that the
live state has not drifted from those decisions.

Three properties matter and the design defends all three:

* Append-only between compactions. Entries are never edited in place; corrections
  are new entries, not overwrites. The one sanctioned rewrite is ``compact``,
  which drops a bounded prefix and re-seals the surviving chain from
  ``GENESIS``: entry content is preserved but hashes change, so tamper-evidence
  holds only from the most recent compaction forward. An auditor holding a
  pre-compaction copy cannot reconcile it against the compacted file.
* Tamper-evident. Each entry carries the previous entry's hash, so rewriting any
  historical entry breaks the chain from that point on. ``verify`` reports the
  index of the last entry it still trusts.
* Compaction with anchor preservation. A long run accumulates entries. ``compact``
  drops the oldest non-anchor entries but re-seals the remaining chain, so the
  ledger stays bounded without losing its audit anchors or its tamper-evidence.
  Safety signals that must outlive compaction (such as the human-escalation
  marker written by ``record_attempt``) are recorded as anchors.

The ledger is storage-agnostic: it talks to a small ``LedgerBackend`` (in-memory
for tests, JSONL file for real use). For cross-process safety it can take a
``LeaseCoordinator`` (the same one that guards single-agent resume) so two
processes cannot append concurrently.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from continuum.concurrency.lease import LeaseCoordinator
from continuum.models import RecoveryContract, utcnow
from continuum.security.hashing import make_id, stable_hash

__all__ = [
    "LedgerEntryKind",
    "RecoveryLedgerEntry",
    "ReconcileReport",
    "RecoveryLedger",
    "LedgerBackend",
    "MemoryLedgerBackend",
    "FileLedgerBackend",
    "LedgerError",
    "LedgerLockError",
]

GENESIS = "genesis"
HUMAN_REQUIRED = "human_required"


class LedgerError(RuntimeError):
    """Base class for ledger failures."""


class LedgerLockError(LedgerError):
    """Raised when the cross-process ledger lock cannot be acquired."""


class LedgerEntryKind(StrEnum):
    """Categorisation of recovery ledger entries.

    Differentiates sealed contracts (``DECISION``), human approval gating
    events (``GATE``), and individual recovery execution attempts (``ATTEMPT``).
    """

    DECISION = "decision"
    GATE = "gate"
    ATTEMPT = "attempt"


@dataclass(frozen=True)
class RecoveryLedgerEntry:
    """One append-only, hash-chained ledger entry."""

    entry_id: str
    run_id: str
    sequence: int
    prev_hash: str
    content_hash: str
    kind: str
    contract: RecoveryContract | None
    gate: str | None
    anchor: bool
    created_at: datetime
    note: str = ""

    def content(self) -> dict[str, Any]:
        """The sealed portion of the entry, excluding its own hash."""
        return {
            "entry_id": self.entry_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "prev_hash": self.prev_hash,
            "kind": self.kind,
            "contract": self.contract.model_dump(mode="json") if self.contract else None,
            "gate": self.gate,
            "anchor": self.anchor,
            "created_at": self.created_at.isoformat(),
            "note": self.note,
        }

    def verify(self) -> bool:
        """Whether the entry's content hash still matches its content."""
        return self.content_hash == stable_hash(self.content())

    def to_record(self) -> dict[str, Any]:
        """Convert the entry to a JSON-serializable dictionary including its hash."""
        return self.content() | {"content_hash": self.content_hash}

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> RecoveryLedgerEntry:
        """Construct a ledger entry from a serialized record dictionary.

        Reconstitutes the optional :class:`~continuum.models.RecoveryContract`
        and parses timestamps from ISO 8601 strings.
        """
        contract = rec.get("contract")
        return cls(
            entry_id=rec["entry_id"],
            run_id=rec["run_id"],
            sequence=rec["sequence"],
            prev_hash=rec["prev_hash"],
            content_hash=rec["content_hash"],
            kind=rec["kind"],
            contract=RecoveryContract.model_validate(contract) if contract else None,
            gate=rec.get("gate"),
            anchor=rec.get("anchor", False),
            created_at=datetime.fromisoformat(rec["created_at"]),
            note=rec.get("note", ""),
        )


class LedgerBackend:
    """Where ledger entries live. Storage-agnostic by design."""

    def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """Load all persisted ledger entries for a run in storage order."""
        raise NotImplementedError

    def save(self, entry: RecoveryLedgerEntry) -> None:
        """Append a single sealed ledger entry to durable storage."""
        raise NotImplementedError

    def replace(self, run_id: str, entries: Sequence[RecoveryLedgerEntry]) -> None:
        """Atomically overwrite all ledger entries for a run with new entries."""
        raise NotImplementedError


class MemoryLedgerBackend(LedgerBackend):
    """In-memory ledger backend for testing and ephemeral runs.

    Stores lists of :class:`RecoveryLedgerEntry` objects in an internal
    dictionary keyed by run ID.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[RecoveryLedgerEntry]] = {}

    def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """Return a copy of the in-memory ledger entries for a run."""
        return list(self._store.get(run_id, []))

    def save(self, entry: RecoveryLedgerEntry) -> None:
        """Append a ledger entry to the in-memory list for its run."""
        self._store.setdefault(entry.run_id, []).append(entry)

    def replace(self, run_id: str, entries: Sequence[RecoveryLedgerEntry]) -> None:
        """Replace the in-memory entry list for a run with a new sequence."""
        self._store[run_id] = list(entries)


class FileLedgerBackend(LedgerBackend):
    """One JSONL file per run. Each line is one entry record."""

    def __init__(self, directory: str) -> None:
        self._directory = directory

    def _path(self, run_id: str) -> str:
        safe = run_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._directory, f"ledger-{safe}.jsonl")

    def _ensure_directory(self) -> None:
        os.makedirs(self._directory, exist_ok=True)

    def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """Read and deserialize all JSONL records for a run from disk.

        Returns an empty list if the ledger file does not yet exist.
        """
        path = self._path(run_id)
        if not os.path.exists(path):
            return []
        out: list[RecoveryLedgerEntry] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                out.append(RecoveryLedgerEntry.from_record(json.loads(line)))
        return out

    def save(self, entry: RecoveryLedgerEntry) -> None:
        """Append a serialized JSON record for the entry to the run's JSONL file."""
        self._ensure_directory()
        with open(self._path(entry.run_id), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_record()) + "\n")

    def replace(self, run_id: str, entries: Sequence[RecoveryLedgerEntry]) -> None:
        """Rewrite the run's JSONL file with the provided sequence of entries."""
        self._ensure_directory()
        with open(self._path(run_id), "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry.to_record()) + "\n")


@dataclass
class ReconcileReport:
    """Result of comparing the ledger against the live state."""

    drift: bool
    details: list[str]


class RecoveryLedger:
    """Append-only, tamper-evident record of recovery decisions for a run."""

    def __init__(
        self,
        backend: LedgerBackend,
        *,
        lock: LeaseCoordinator | None = None,
        holder_id: str = "recovery-ledger",
        ttl: timedelta | None = None,
    ) -> None:
        self._backend = backend
        self._lock = lock
        self._holder_id = holder_id
        self._ttl = ttl

    # -- locking ---------------------------------------------------------- #

    @contextmanager
    def _locked(self, run_id: str) -> Iterator[None]:
        if self._lock is None:
            yield
            return
        if not self._lock.acquire(run_id, self._holder_id, self._ttl):
            raise LedgerLockError(f"could not acquire ledger lock for run {run_id!r}")
        try:
            yield
        finally:
            self._lock.release(run_id, self._holder_id)

    # -- writing ---------------------------------------------------------- #

    def _seal_and_save(
        self,
        run_id: str,
        entries: Sequence[RecoveryLedgerEntry],
        *,
        kind: LedgerEntryKind,
        contract: RecoveryContract | None = None,
        gate: str | None = None,
        anchor: bool = False,
        note: str = "",
    ) -> RecoveryLedgerEntry:
        # Sequence and prev_hash must follow the highest-sequence entry, not
        # the last position of the backend's load order: after compact() the
        # survivors keep their original (sparse) sequences, so len(entries)
        # would mint a colliding sequence that sorts before them, breaking
        # the chain walk in verify() and the approval ordering in
        # pending_gate().
        head = max(entries, key=lambda e: e.sequence) if entries else None
        partial = RecoveryLedgerEntry(
            entry_id=make_id("ledger"),
            run_id=run_id,
            sequence=head.sequence + 1 if head else 0,
            prev_hash=head.content_hash if head else GENESIS,
            content_hash="",
            kind=kind.value,
            contract=contract,
            gate=gate,
            anchor=anchor,
            created_at=utcnow(),
            note=note,
        )
        sealed = replace(partial, content_hash=stable_hash(partial.content()))
        self._backend.save(sealed)
        return sealed

    def _make_entry(
        self,
        run_id: str,
        *,
        kind: LedgerEntryKind,
        contract: RecoveryContract | None = None,
        gate: str | None = None,
        anchor: bool = False,
        note: str = "",
    ) -> RecoveryLedgerEntry:
        return self._seal_and_save(
            run_id,
            self._backend.load(run_id),
            kind=kind,
            contract=contract,
            gate=gate,
            anchor=anchor,
            note=note,
        )

    def append_decision(
        self,
        run_id: str,
        contract: RecoveryContract,
        *,
        anchor: bool = False,
        gate: str | None = None,
        note: str = "",
    ) -> RecoveryLedgerEntry:
        """Record a recovery decision (a sealed contract)."""
        with self._locked(run_id):
            return self._make_entry(
                run_id,
                kind=LedgerEntryKind.DECISION,
                contract=contract,
                anchor=anchor,
                gate=gate,
                note=note,
            )

    def record_attempt(
        self, run_id: str, *, note: str = "", max_attempts: int | None = None
    ) -> int:
        """Record one recovery attempt and return the new attempt count.

        When ``max_attempts`` is given and the new count reaches it, an anchored
        ``human_required`` gate entry is written (once), so the escalation
        survives later compaction of the ATTEMPT entries.
        """
        with self._locked(run_id):
            entries = self._backend.load(run_id)
            sealed = self._seal_and_save(run_id, entries, kind=LedgerEntryKind.ATTEMPT, note=note)
            count = sum(1 for e in entries if e.kind == LedgerEntryKind.ATTEMPT.value) + 1
            escalated = any(
                e.kind == LedgerEntryKind.GATE.value and e.gate == HUMAN_REQUIRED for e in entries
            )
            if max_attempts is not None and count >= max_attempts and not escalated:
                self._seal_and_save(
                    run_id,
                    [*entries, sealed],
                    kind=LedgerEntryKind.GATE,
                    gate=HUMAN_REQUIRED,
                    anchor=True,
                    note=f"attempt {count} reached the escalation threshold {max_attempts}",
                )
            return count

    def attempts(self, run_id: str) -> int:
        """Return the count of recorded recovery attempts for a run."""
        return sum(1 for e in self.entries(run_id) if e.kind == LedgerEntryKind.ATTEMPT.value)

    def requires_human(self, run_id: str, *, max_attempts: int = 3) -> bool:
        """True once attempts have reached the human-in-the-loop threshold.

        Also True if a persisted ``human_required`` marker exists, so a prior
        escalation is not forgotten when compaction drops old ATTEMPT entries.
        """
        entries = self.entries(run_id)
        if any(e.kind == LedgerEntryKind.GATE.value and e.gate == HUMAN_REQUIRED for e in entries):
            return True
        return sum(1 for e in entries if e.kind == LedgerEntryKind.ATTEMPT.value) >= max_attempts

    def record_gate(self, run_id: str, status: str, *, note: str = "") -> RecoveryLedgerEntry:
        """Persist a human-in-the-loop gate event (required/approved/rejected)."""
        with self._locked(run_id):
            return self._make_entry(run_id, kind=LedgerEntryKind.GATE, gate=status, note=note)

    def pending_gate(self, run_id: str) -> RecoveryLedgerEntry | None:
        """Return the latest decision still awaiting human approval, if any.

        An approval clears only the decision it follows: a later gate-required
        decision is pending again even if an earlier one was approved.
        """
        entries = self.entries(run_id)
        for entry in reversed(entries):
            if entry.kind == LedgerEntryKind.DECISION.value and entry.gate == "required":
                cleared = any(
                    e.kind == LedgerEntryKind.GATE.value
                    and e.gate == "approved"
                    and e.sequence > entry.sequence
                    for e in entries
                )
                return None if cleared else entry
        return None

    # -- reading ---------------------------------------------------------- #

    def entries(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """Return all ledger entries for a run sorted by ascending sequence."""
        return sorted(self._backend.load(run_id), key=lambda e: e.sequence)

    def last_decision(self, run_id: str) -> RecoveryLedgerEntry | None:
        """Return the latest sealed decision entry for a run, or ``None`` if none exists."""
        decisions = [e for e in self.entries(run_id) if e.kind == LedgerEntryKind.DECISION.value]
        return decisions[-1] if decisions else None

    def verify(self, run_id: str) -> tuple[bool, int]:
        """Return (chain_ok, trusted_through_index).

        ``trusted_through_index`` is the number of contiguous entries that still
        verify from the start; if ``chain_ok`` is False it is the first broken
        index, so a reader knows exactly where trust ends.
        """
        prev = GENESIS
        for index, entry in enumerate(self.entries(run_id)):
            if entry.prev_hash != prev or not entry.verify():
                return (False, index)
            prev = entry.content_hash
        return (True, len(self.entries(run_id)))

    # -- compaction ------------------------------------------------------- #

    def compact(self, run_id: str, *, keep: int = 50, keep_anchors: bool = True) -> int:
        """Drop old entries but keep the newest ``keep`` and any anchors.

        The surviving entries are re-sealed into a fresh chain (the first kept
        entry links to ``GENESIS``), so the ledger stays tamper-evident after
        compaction. Returns the number of entries removed.
        """
        if keep < 1:
            keep = 1
        with self._locked(run_id):
            entries = self.entries(run_id)
            if len(entries) <= keep:
                return 0
            newest = entries[-keep:]
            kept = list(newest)
            for entry in entries[:-keep]:
                if keep_anchors and entry.anchor:
                    kept.append(entry)
            kept.sort(key=lambda e: e.sequence)

            prev = GENESIS
            rechained: list[RecoveryLedgerEntry] = []
            for entry in kept:
                rebuilt = replace(entry, prev_hash=prev, content_hash="")
                rebuilt = replace(rebuilt, content_hash=stable_hash(rebuilt.content()))
                rechained.append(rebuilt)
                prev = rebuilt.content_hash

            self._backend.replace(run_id, rechained)
            return len(entries) - len(rechained)

    # -- reconciliation --------------------------------------------------- #

    def reconcile(self, run_id: str, state: object) -> ReconcileReport:
        """Detect drift between the ledger and the live state.

        Two checks: the ledger chain must verify, and the live state must not be
        behind the highest checkpoint version any surviving decision was sealed
        against (a high-water mark, so a later decision sealed from a stale or
        rolled-back state cannot silently lower the bar).
        """
        details: list[str] = []
        ok, trusted = self.verify(run_id)
        if not ok:
            details.append(f"ledger chain broken at index {trusted}")

        versions = [
            e.contract.checkpoint_version
            for e in self.entries(run_id)
            if e.kind == LedgerEntryKind.DECISION.value and e.contract is not None
        ]
        if not versions:
            details.append("no ledger decision to reconcile against")
            return ReconcileReport(drift=bool(details), details=details)

        watermark = max(versions)
        state_version = getattr(state, "version", None)
        if state_version is None:
            details.append("live state exposes no version to compare")
        elif state_version < watermark:
            details.append(
                f"live state version {state_version} is behind the contract checkpoint v{watermark}"
            )
        return ReconcileReport(drift=bool(details), details=details)
