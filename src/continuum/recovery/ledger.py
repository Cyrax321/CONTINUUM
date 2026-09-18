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
* Budgets scoped to a dependency (issue #744). An attempt charges the allowance
  of the dependency that owns it, not the run's, so one repeatedly failing
  integration cannot exhaust the attempts a *different* dependency needs to
  repair. Ownership that is unknown, conflicting or malformed never opens a
  private budget: it falls back to the shared run-wide bucket, which can only
  escalate sooner.

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
    "BudgetStatus",
    "LedgerBackend",
    "MemoryLedgerBackend",
    "FileLedgerBackend",
    "LedgerError",
    "LedgerLockError",
]

GENESIS = "genesis"
HUMAN_REQUIRED = "human_required"


def _normalize_scope(value: object) -> str | None:
    """Reduce one ownership signal to a canonical dependency key.

    Declared dependency names are lower-cased (``PyYAML>=6`` and ``pyyaml`` are
    one dependency, see :func:`continuum.analysis.depends._normalize_dep`), so a
    scope differing only in case must not become two budgets. Anything that is
    not a non-empty string is rejected: the caller gets the run-wide bucket
    rather than a private one.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def resolve_scope(*candidates: object) -> str | None:
    """Pick the dependency scope a recovery attempt is charged to.

    Each candidate is one ownership signal: a dependency name, an action's
    ``dep_scope``, or the resource set a scoped assessment was confined to (an
    iterable of names, in which case every name it names counts as a candidate).

    Exactly one distinct name wins. Zero candidates (ownership unknown) or two
    or more (ownership conflicting) both return ``None``, which is the run-wide
    bucket: the attempt then costs the shared allowance instead of opening a
    fresh private one, so ambiguity can only escalate sooner and can never grant
    capacity the run did not have (issue #744, fail-closed by design).
    """
    found: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, str):
            names: tuple[object, ...] = (candidate,)
        elif isinstance(candidate, (list, tuple, set, frozenset)):
            names = tuple(candidate)
        else:
            names = ()
        for name in names:
            normalized = _normalize_scope(name)
            if normalized is not None:
                found.add(normalized)
    if len(found) != 1:
        return None
    return next(iter(found))


class LedgerError(RuntimeError):
    """Base class for ledger failures."""


class LedgerLockError(LedgerError):
    """Raised when the cross-process ledger lock cannot be acquired."""


class LedgerEntryKind(StrEnum):
    """What a ledger entry records: a recovery decision, a gate event, or an attempt.

    Every read filters on it: ``last_decision`` and ``reconcile`` look at
    DECISION entries, ``pending_gate`` and the escalation marker are GATE
    entries, and the attempt count is the number of ATTEMPT entries.
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
    #: The dependency whose attempt budget this entry charges. ``None`` is the
    #: run-wide bucket: every entry written before scopes existed (issue #744)
    #: lives there, and so does any attempt whose ownership could not be
    #: established (see :func:`resolve_scope`).
    scope: str | None = None

    def content(self) -> dict[str, Any]:
        """The sealed portion of the entry, excluding its own hash.

        ``scope`` is included only when set, so a record written before scopes
        existed still hashes exactly as it did then: the chain an auditor holds
        from before this field must keep verifying after the upgrade.
        """
        content: dict[str, Any] = {
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
        if self.scope is not None:
            content["scope"] = self.scope
        return content

    def verify(self) -> bool:
        """Whether the entry's content hash still matches its content."""
        return self.content_hash == stable_hash(self.content())

    def to_record(self) -> dict[str, Any]:
        """The full JSON-safe record: :meth:`content` plus its ``content_hash``.

        This is the line backends persist; :meth:`from_record` is its exact
        inverse, so a record round-trips to an equal entry.
        """
        return self.content() | {"content_hash": self.content_hash}

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> RecoveryLedgerEntry:
        """Rebuild an entry from a :meth:`to_record` record.

        Raises ``KeyError`` when a required field is absent and lets pydantic
        raise on an embedded contract that does not validate: a record that
        did not come from ``to_record`` is a caller bug to surface, not a
        ledger condition to absorb.

        ``scope`` is re-validated: a hand-edited record that carries a scope
        which is not a non-empty string falls back to the run-wide bucket, so a
        malformed scope cannot manufacture a fresh private budget out of a file
        an attacker controls.
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
            scope=_normalize_scope(rec.get("scope")),
        )


class LedgerBackend:
    """Where ledger entries live. Storage-agnostic by design."""

    def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """Return the run's entries, an empty list when it has none.

        Order is the backend's own; ``RecoveryLedger.entries`` sorts by
        sequence before reading. A run with no data must yield an empty
        list, not an error.
        """
        raise NotImplementedError

    def save(self, entry: RecoveryLedgerEntry) -> None:
        """Append one sealed entry. Must not rewrite or reorder what exists."""
        raise NotImplementedError

    def replace(self, run_id: str, entries: Sequence[RecoveryLedgerEntry]) -> None:
        """Substitute the run's entire entry list with ``entries``.

        Called only by ``compact`` with a re-sealed chain, so the write must
        overwrite, not append: appending would duplicate the very history
        compaction was asked to bound.
        """
        raise NotImplementedError


class MemoryLedgerBackend(LedgerBackend):
    """In-memory backend: one entry list per run, gone when the process exits.

    For tests and ephemeral runs; use ``FileLedgerBackend`` when the ledger
    must survive the process.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[RecoveryLedgerEntry]] = {}

    def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """A copy of the run's entries: mutating it cannot corrupt the backend."""
        return list(self._store.get(run_id, []))

    def save(self, entry: RecoveryLedgerEntry) -> None:
        """Append to the run's list, creating it on first save."""
        self._store.setdefault(entry.run_id, []).append(entry)

    def replace(self, run_id: str, entries: Sequence[RecoveryLedgerEntry]) -> None:
        """Overwrite the run's list with a copy of ``entries``."""
        self._store[run_id] = list(entries)


#: Every character a Windows filename forbids, separators included. POSIX
#: forbids none of the extras, so replacing them all on every platform costs
#: nothing and keeps a caller-supplied run id usable as a filename on both
#: families (issue #842: only "/" and "\" were replaced, so run ids carrying
#: ":*?<>"|" produced unopenable ledger files on Windows).
_UNSAFE_FILENAME_CHARS = '/\\:*?"<>|'

#: Windows reserves these device names as filenames with any extension, so a
#: sanitized id whose stem is one of them still cannot open there.
_RESERVED_DEVICE_NAMES = frozenset(
    {"AUX", "CON", "NUL", "PRN"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _sanitize_run_id(run_id: str) -> str:
    """Turn a caller-supplied run id into a safe filename component.

    The result keeps every character that is legal on both platform families
    and substitutes an underscore for the rest, so the same run id maps to
    the same file everywhere.
    """
    safe = "".join("_" if char in _UNSAFE_FILENAME_CHARS else char for char in run_id)
    if safe.split(".", 1)[0].upper() in _RESERVED_DEVICE_NAMES:
        safe = f"_{safe}"
    return safe


class FileLedgerBackend(LedgerBackend):
    """One JSONL file per run. Each line is one entry record."""

    def __init__(self, directory: str) -> None:
        self._directory = directory

    def _path(self, run_id: str) -> str:
        return os.path.join(self._directory, f"ledger-{_sanitize_run_id(run_id)}.jsonl")

    def _ensure_directory(self) -> None:
        os.makedirs(self._directory, exist_ok=True)

    def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
        """Read the run's JSONL file, skipping blank lines.

        A missing file is a run with no entries (empty list), not an error:
        every ledger starts from GENESIS.
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
        """Append one entry as a JSONL line, creating the directory if needed."""
        self._ensure_directory()
        with open(self._path(entry.run_id), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_record()) + "\n")

    def replace(self, run_id: str, entries: Sequence[RecoveryLedgerEntry]) -> None:
        """Rewrite the run's file with exactly ``entries``, nothing else."""
        self._ensure_directory()
        with open(self._path(run_id), "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry.to_record()) + "\n")


@dataclass
class ReconcileReport:
    """Result of comparing the ledger against the live state."""

    drift: bool
    details: list[str]


@dataclass(frozen=True)
class BudgetStatus:
    """The recovery-attempt allowance standing for one dependency scope.

    Carries counts and the scope name only. The reason an attempt failed, the
    arguments it carried and the files it touched are not budget facts, so they
    stay out of anything a contract or a CLI prints (issue #744).
    """

    scope: str | None
    attempts: int
    max_attempts: int
    escalated: bool

    @property
    def remaining(self) -> int:
        """Attempts the scope still has, never negative."""
        return max(0, self.max_attempts - self.attempts)

    @property
    def exhausted(self) -> bool:
        """Whether the allowance has been spent."""
        return self.attempts >= self.max_attempts

    @property
    def requires_human(self) -> bool:
        """Whether this scope is now human-gated: escalated, or simply spent."""
        return self.escalated or self.exhausted

    def to_dict(self) -> dict[str, Any]:
        """The JSON-safe form; the scope is named ``global`` when it is run-wide."""
        return {
            "scope": self.scope if self.scope is not None else "global",
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
            "requires_human": self.requires_human,
        }


def _ceiling(limit: int, global_limit: int | None) -> int:
    """The allowance actually enforced: never above the run-wide ceiling.

    ``global_limit`` ``None`` means the caller imposed no run-wide ceiling, so
    the scoped limit stands; otherwise the smaller of the two wins, so a
    per-dependency allowance configured above the run-wide number still
    escalates at that number.
    """
    if global_limit is None:
        return limit
    return min(limit, global_limit)


def _marker_covers(entry: RecoveryLedgerEntry, scope: str | None) -> bool:
    """Whether an anchored ``human_required`` marker blocks ``scope``.

    A run-wide marker (no scope) blocks every scope, and any marker blocks the
    run-wide query: an unknown-ownership caller must read a known escalation as
    its own. A scoped marker leaves every other scope alone, which is the whole
    point of per-dependency budgets.
    """
    marker = entry.scope
    if marker is None:
        return True
    if scope is None:
        return True
    return marker == scope


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
        scope: str | None = None,
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
            scope=scope,
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
        scope: str | None = None,
    ) -> RecoveryLedgerEntry:
        return self._seal_and_save(
            run_id,
            self._backend.load(run_id),
            kind=kind,
            contract=contract,
            gate=gate,
            anchor=anchor,
            note=note,
            scope=scope,
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
        self,
        run_id: str,
        *,
        scope: object = None,
        note: str = "",
        max_attempts: int | None = None,
        global_max_attempts: int | None = None,
    ) -> int:
        """Record one recovery attempt and return the new count for its scope.

        ``scope`` is any ownership signal (a dependency name, an action's
        ``dep_scope``, the resource set a scoped assessment was confined to);
        it is reduced by :func:`resolve_scope`, so unknown or conflicting
        ownership charges the run-wide bucket rather than a private one.

        When ``max_attempts`` is given and the scope's new count reaches it, an
        anchored ``human_required`` gate entry is written (once per scope), so
        the escalation survives later compaction of the ATTEMPT entries. The
        marker carries the scope, so dependency A's escalation does not block a
        repair path for dependency B.

        ``global_max_attempts`` caps a scoped allowance at the run-wide ceiling:
        a per-dependency limit configured above it still escalates at the
        run-wide number, so a scope can never buy more attempts than the run
        was ever allowed (issue #744).
        """
        resolved = resolve_scope(scope)
        with self._locked(run_id):
            entries = self._backend.load(run_id)
            sealed = self._seal_and_save(
                run_id, entries, kind=LedgerEntryKind.ATTEMPT, note=note, scope=resolved
            )
            count = (
                sum(
                    1
                    for e in entries
                    if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == resolved
                )
                + 1
            )
            limit = (
                _ceiling(max_attempts, global_max_attempts) if max_attempts is not None else None
            )
            escalated = any(
                e.kind == LedgerEntryKind.GATE.value
                and e.gate == HUMAN_REQUIRED
                and _marker_covers(e, resolved)
                for e in entries
            )
            if limit is not None and count >= limit and not escalated:
                label = resolved if resolved is not None else "global"
                self._seal_and_save(
                    run_id,
                    [*entries, sealed],
                    kind=LedgerEntryKind.GATE,
                    gate=HUMAN_REQUIRED,
                    anchor=True,
                    scope=resolved,
                    note=f"attempt {count} for scope {label} reached the escalation "
                    f"threshold {limit}",
                )
            return count

    def attempts(self, run_id: str, *, scope: object = None) -> int:
        """The attempt count for ``scope``: how many of its ATTEMPT entries survive.

        ``scope`` is reduced by :func:`resolve_scope`; without one this counts
        the run-wide bucket, which is every attempt written before scopes
        existed. Compaction can lower the count, which is why escalation is
        recorded as an anchored GATE entry (see ``record_attempt``) rather than
        inferred from the number.
        """
        resolved = resolve_scope(scope)
        return sum(
            1
            for e in self.entries(run_id)
            if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == resolved
        )

    def requires_human(
        self,
        run_id: str,
        *,
        scope: object = None,
        max_attempts: int = 3,
        global_max_attempts: int | None = None,
    ) -> bool:
        """True once ``scope`` has reached the human-in-the-loop threshold.

        Also True if a persisted ``human_required`` marker covers the scope, so
        a prior escalation is not forgotten when compaction drops old ATTEMPT
        entries. A run-wide marker covers every scope, and a scoped marker also
        answers the run-wide query, because an unknown-ownership caller cannot
        assume some other dependency's escalation is not its own (issue #744).
        """
        resolved = resolve_scope(scope)
        entries = self.entries(run_id)
        if any(
            e.kind == LedgerEntryKind.GATE.value
            and e.gate == HUMAN_REQUIRED
            and _marker_covers(e, resolved)
            for e in entries
        ):
            return True
        limit = _ceiling(max_attempts, global_max_attempts)
        return self._attempt_count(entries, resolved) >= limit

    def budget(
        self,
        run_id: str,
        *,
        scope: object = None,
        max_attempts: int = 3,
        global_max_attempts: int | None = None,
    ) -> BudgetStatus:
        """The allowance standing for ``scope``: counts only, for a contract or
        a CLI to print.

        ``scope`` is reduced by :func:`resolve_scope`; the returned status names
        the scope it actually resolved, so a caller whose ownership was
        ambiguous can see that it fell back to the run-wide bucket rather than
        silently getting a private one.
        """
        resolved = resolve_scope(scope)
        entries = self.entries(run_id)
        limit = _ceiling(max_attempts, global_max_attempts)
        escalated = any(
            e.kind == LedgerEntryKind.GATE.value
            and e.gate == HUMAN_REQUIRED
            and _marker_covers(e, resolved)
            for e in entries
        )
        used = self._attempt_count(entries, resolved)
        return BudgetStatus(
            scope=resolved,
            attempts=used,
            max_attempts=limit,
            escalated=escalated,
        )

    @staticmethod
    def _attempt_count(entries: Sequence[RecoveryLedgerEntry], scope: str | None) -> int:
        return sum(
            1 for e in entries if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == scope
        )

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
        """All entries for the run, sorted by sequence.

        This is the chain-walk order ``verify`` depends on. Sequences are
        sparse after ``compact`` (survivors keep their original numbers), so
        position and sequence number disagree there; sorting handles both.
        """
        return sorted(self._backend.load(run_id), key=lambda e: e.sequence)

    def last_decision(self, run_id: str) -> RecoveryLedgerEntry | None:
        """The most recent DECISION entry for the run, or ``None`` if it has none."""
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
