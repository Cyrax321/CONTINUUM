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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from continuum.budgets import max_attempts_for_dependency
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
    "BudgetStatus",
    "derive_recovery_dependencies",
    "dependencies_for_contract",
    "dependencies_for_action",
    "resolve_scope",
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


def _dependency_gate(dependency: str) -> str:
    """The gate name that records escalation for a single dependency (#1428).

    Namespaced rather than a bare ``human_required`` so the two escalations stay
    distinguishable: a dependency that exhausted only its own allowance must not
    set the run-wide marker, and readers that ask ``requires_human()`` for the
    run as a whole must not see it either.
    """
    return f"{HUMAN_REQUIRED}:{dependency}"


def dependencies_for_contract(contract: RecoveryContract) -> list[str]:
    """Derive external dependency names from a sealed recovery contract."""
    deps: list[str] = []
    for line in contract.evidence:
        prefix = "localized recovery scoped to: "
        if line.startswith(prefix):
            raw = line[len(prefix) :].strip()
            deps.extend([d.strip() for d in raw.split(",") if d.strip()])
    for action in contract.required_actions:
        prefix = "revalidate_dependency:"
        if action.startswith(prefix):
            dep = action[len(prefix) :].strip()
            if dep and dep not in deps:
                deps.append(dep)
    return deps


def dependencies_for_action(action: Any) -> list[str]:
    """Derive external dependency names from an action or action-like object."""
    if action is None:
        return []
    dep = getattr(action, "dep_scope", None)
    if isinstance(action, dict):
        dep = action.get("dep_scope") or action.get("dependency")
    if not dep or not isinstance(dep, str):
        return []
    if "," in dep:
        return [d.strip() for d in dep.split(",") if d.strip()]
    return [dep.strip()]


def derive_recovery_dependencies(
    *,
    dependency: str | None = None,
    dependencies: Iterable[str] | None = None,
    contract: RecoveryContract | None = None,
    action: Any | None = None,
    scope: Iterable[str] | None = None,
) -> list[str]:
    """Derive external dependency names from explicit inputs, contract, action, or scope."""
    out: list[str] = []
    if dependency:
        out.append(dependency)
    if dependencies:
        for d in dependencies:
            if d and d not in out:
                out.append(d)
    if scope:
        for s in scope:
            if s and s not in out:
                out.append(s)
    if contract is not None:
        for d in dependencies_for_contract(contract):
            if d not in out:
                out.append(d)
    if action is not None:
        for d in dependencies_for_action(action):
            if d not in out:
                out.append(d)
    return out


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
    #: The external dependency an ATTEMPT went through (#1428). ``None`` on every
    #: other kind and on attempts not tied to one, so a ledger written before the
    #: field existed loads unchanged.
    dependency: str | None = None
    #: The dependency whose attempt budget this entry charges (#744). ``None`` is
    #: the run-wide bucket: every entry written before scopes existed lives
    #: there, and so does any attempt whose ownership could not be established
    #: (see :func:`resolve_scope`). Orthogonal to ``dependency``: a record made
    #: from an explicit dependency tags that field, and one made from a scope
    #: tags this one, so the run-wide count is "no scope claimed".
    scope: str | None = None

    def content(self) -> dict[str, Any]:
        """The sealed portion of the entry, excluding its own hash.

        ``dependency`` and ``scope`` are included only when set, so a record
        written before either field existed still hashes exactly as it did then:
        the chain an auditor holds from before the upgrade must keep verifying.
        """
        data: dict[str, Any] = {
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
        if self.dependency is not None:
            data["dependency"] = self.dependency
        if self.scope is not None:
            data["scope"] = self.scope
        return data

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
            dependency=rec.get("dependency"),
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
        dependency: str | None = None,
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
            dependency=dependency,
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
        dependency: str | None = None,
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
            dependency=dependency,
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
        note: str = "",
        max_attempts: int | None = None,
        dependency: str | None = None,
        dependencies: Iterable[str] | None = None,
        contract: RecoveryContract | None = None,
        action: Any | None = None,
        scope: Iterable[str] | None = None,
        dependency_budgets: Mapping[str, Any] | None = None,
        global_max_attempts: int | None = None,
    ) -> int:
        """Record one recovery attempt and return the attempt count.

        There are two ways to name what an attempt is for, and they are kept
        orthogonal rather than collapsed into one field:

        - The **dependency** model (#1428, #1459): ``dependency``, ``dependencies``,
          ``contract``, or ``action`` name external resources, and one ATTEMPT entry
          is recorded per resolved dependency, each counted against its own ceiling.
          The entry tags ``dependency``, and a namespaced
          ``human_required:<dependency>`` marker escalates that dependency alone.
        - The **scope** model (#744): ``scope`` names a code region. A scope that
          reduces to exactly one normalized name charges that bucket; one that names
          several resources is conflicting ownership and falls back to the run-wide
          bucket. The entry tags ``scope``, and a bare ``human_required`` marker
          escalates that scope.

        With ``max_attempts`` given, an anchored escalation marker is written (once)
        when the count for its bucket reaches the threshold, so the escalation
        survives later compaction of the ATTEMPT entries.

        ``dependency_budgets`` is either the loaded budget registry or a bare
        ``{dependency: limit}`` mapping; if omitted, it is loaded from
        ``.continuum/budgets.json`` if that file exists.
        """
        if dependency_budgets is None:
            try:
                from continuum.budgets import DEFAULT_BUDGETS_PATH, load_budgets

                dependency_budgets = load_budgets(Path(DEFAULT_BUDGETS_PATH))
            except Exception:
                dependency_budgets = None

        # Which model an attempt belongs to is decided by what named it, not by
        # what derive_recovery_dependencies happens to yield: a bare ``scope``
        # argument still derives dependencies (the string is split into names),
        # but its bucket is the scope, charged through resolve_scope rather than
        # through the per-dependency ceilings.
        if (
            dependency is not None
            or dependencies is not None
            or contract is not None
            or action is not None
        ):
            deps = derive_recovery_dependencies(
                dependency=dependency,
                dependencies=dependencies,
                contract=contract,
                action=action,
                scope=scope,
            )
        else:
            deps = []

        resolved = resolve_scope(scope) if scope is not None else None
        scope_deps = [resolved] if resolved is not None else []

        with self._locked(run_id):
            entries = self._backend.load(run_id)
            if deps:
                current_entries = list(entries)
                last_count = 0
                for dep in deps:
                    sealed = self._seal_and_save(
                        run_id,
                        current_entries,
                        kind=LedgerEntryKind.ATTEMPT,
                        note=note,
                        dependency=dep,
                    )
                    current_entries.append(sealed)
                    gate_entry = self._maybe_escalate_dependency(
                        current_entries[:-1], sealed, dep, dependency_budgets, max_attempts
                    )
                    if gate_entry is not None:
                        current_entries.append(gate_entry)
                    last_count = sum(
                        1
                        for e in current_entries
                        if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency == dep
                    )
                return last_count

            if scope_deps:
                return self._record_scoped_attempt(
                    run_id, entries, note, scope_deps[0], max_attempts, global_max_attempts
                )

            # Nothing named ownership, so this is the run-wide bucket. Its
            # counter is the pre-#1428 one -- attempts that tagged no
            # dependency -- so a flaky dependency exhausting its own allowance
            # cannot also set the run-wide gate.
            sealed = self._seal_and_save(
                run_id,
                entries,
                kind=LedgerEntryKind.ATTEMPT,
                note=note,
                dependency=None,
            )
            count = (
                sum(
                    1
                    for e in entries
                    if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency is None
                )
                + 1
            )
            limit = (
                _ceiling(max_attempts, global_max_attempts) if max_attempts is not None else None
            )
            escalated = any(
                e.kind == LedgerEntryKind.GATE.value
                and e.gate == HUMAN_REQUIRED
                and _marker_covers(e, None)
                for e in entries
            )
            if limit is not None and count >= limit and not escalated:
                self._seal_and_save(
                    run_id,
                    [*entries, sealed],
                    kind=LedgerEntryKind.GATE,
                    gate=HUMAN_REQUIRED,
                    anchor=True,
                    scope=None,
                    note=f"attempt {count} reached the escalation threshold {limit}",
                )
            return count

    def _record_scoped_attempt(
        self,
        run_id: str,
        entries: Sequence[RecoveryLedgerEntry],
        note: str,
        resolved_scope: str,
        max_attempts: int | None,
        global_max_attempts: int | None,
    ) -> int:
        """Charge one attempt to a resolved scope bucket and escalate if it is full.

        Kept beside the run-wide path in :meth:`record_attempt` so the two buckets
        stay structurally identical: same entry kind, same marker, same one-shot
        guard -- differing only in which tag carries the bucket name.
        """
        sealed = self._seal_and_save(
            run_id,
            entries,
            kind=LedgerEntryKind.ATTEMPT,
            note=note,
            dependency=resolved_scope,
            scope=resolved_scope,
        )
        count = (
            sum(
                1
                for e in entries
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == resolved_scope
            )
            + 1
        )
        limit = _ceiling(max_attempts, global_max_attempts) if max_attempts is not None else None
        escalated = any(
            e.kind == LedgerEntryKind.GATE.value
            and e.gate == HUMAN_REQUIRED
            and _marker_covers(e, resolved_scope)
            for e in entries
        )
        if limit is not None and count >= limit and not escalated:
            self._seal_and_save(
                run_id,
                [*entries, sealed],
                kind=LedgerEntryKind.GATE,
                gate=HUMAN_REQUIRED,
                anchor=True,
                scope=resolved_scope,
                note=(
                    f"attempt {count} for scope {resolved_scope!r} reached "
                    f"the escalation threshold {limit}"
                ),
            )
        return count

    def _maybe_escalate_dependency(
        self,
        entries: Sequence[RecoveryLedgerEntry],
        sealed: RecoveryLedgerEntry,
        dependency: str,
        dependency_budgets: Mapping[str, Any] | None,
        global_max: int | None,
    ) -> RecoveryLedgerEntry | None:
        """Write the per-dependency escalation marker if this attempt used its ceiling.

        ``sealed`` is already saved, so it counts but is not in ``entries``; the
        gate entry is appended after it exactly the way the global one is, so
        both markers sort after the attempt that tripped them.
        """
        limit = max_attempts_for_dependency(dependency_budgets, dependency, fallback=global_max)
        if limit is None:
            return None
        gate = _dependency_gate(dependency)
        if any(e.kind == LedgerEntryKind.GATE.value and e.gate == gate for e in entries):
            return None  # already escalated: one marker is enough, and it is anchored
        used = (
            sum(
                1
                for e in entries
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency == dependency
            )
            + 1
        )
        if used >= limit:
            return self._seal_and_save(
                run_id=sealed.run_id,
                entries=[*entries, sealed],
                kind=LedgerEntryKind.GATE,
                gate=gate,
                anchor=True,
                dependency=dependency,
                note=(
                    f"dependency {dependency!r} reached its escalation threshold {limit} "
                    f"(attempt {used})"
                ),
            )
        return None

    def attempts(
        self,
        run_id: str,
        *,
        dependency: str | None = None,
        dependencies: Iterable[str] | None = None,
        contract: RecoveryContract | None = None,
        action: Any | None = None,
        scope: Iterable[str] | None = None,
    ) -> int:
        """The run's recovery-attempt count: how many ATTEMPT entries survive.

        With a dependency or derived dependencies, only attempts tagged for those
        dependencies count (#1428, #1459). When none are specified, returns the count
        of all surviving attempts for the run.

        Compaction can lower this count, which is why escalation is recorded
        as an anchored GATE entry (see ``record_attempt``) rather than
        inferred from the number.
        """
        # Same routing rule as record_attempt: only arguments that name a
        # dependency select the dependency model. A bare ``scope`` derives
        # dependencies too (the string splits into names), but its bucket is
        # the scope.
        if (
            dependency is not None
            or dependencies is not None
            or contract is not None
            or action is not None
        ):
            deps = derive_recovery_dependencies(
                dependency=dependency,
                dependencies=dependencies,
                contract=contract,
                action=action,
                scope=scope,
            )
        else:
            deps = []
        if deps:
            dep_set = set(deps)
            return sum(
                1
                for e in self.entries(run_id)
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency in dep_set
            )

        resolved = resolve_scope(scope) if scope is not None else None
        if resolved is not None:
            return sum(
                1
                for e in self.entries(run_id)
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == resolved
            )

        # The run-wide bucket is the set of attempts that claimed no ownership
        # at all, which is what the pre-#744 caller sees.
        return sum(
            1
            for e in self.entries(run_id)
            if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope is None
        )

    def requires_human(
        self,
        run_id: str,
        *,
        max_attempts: int = 3,
        dependency: str | None = None,
        dependencies: Iterable[str] | None = None,
        contract: RecoveryContract | None = None,
        action: Any | None = None,
        scope: Iterable[str] | None = None,
        dependency_budgets: Mapping[str, Any] | None = None,
        global_max_attempts: int | None = None,
    ) -> bool:
        """True once attempts have reached the human-in-the-loop threshold.

        Also True if a persisted ``human_required`` marker exists, so a prior
        escalation is not forgotten when compaction drops old ATTEMPT entries.
        A marker covers its own scope, and the run-wide query sees any marker at
        all -- an ownership-less caller must never read a known escalation as
        still-clearable, so that direction fails closed.

        With ``dependency``, ``dependencies``, ``contract``, ``action``, or ``scope``,
        the threshold is evaluated per dependency against its own ceiling (#1428, #1459):
        one flaky external service escalating itself must not starve unrelated core
        tasks of their recovery attempts. If any targeted dependency has exceeded its
        ceiling, True is returned.
        """
        if dependency_budgets is None:
            try:
                from continuum.budgets import DEFAULT_BUDGETS_PATH, load_budgets

                dependency_budgets = load_budgets(Path(DEFAULT_BUDGETS_PATH))
            except Exception:
                dependency_budgets = None

        entries = self.entries(run_id)
        resolved = resolve_scope(scope) if scope is not None else None
        if any(
            e.kind == LedgerEntryKind.GATE.value
            and e.gate == HUMAN_REQUIRED
            and _marker_covers(e, resolved)
            for e in entries
        ):
            return True

        if (
            dependency is not None
            or dependencies is not None
            or contract is not None
            or action is not None
        ):
            deps = derive_recovery_dependencies(
                dependency=dependency,
                dependencies=dependencies,
                contract=contract,
                action=action,
                scope=scope,
            )
        else:
            deps = []
        if deps:
            for dep in deps:
                gate = _dependency_gate(dep)
                if any(e.kind == LedgerEntryKind.GATE.value and e.gate == gate for e in entries):
                    return True
                limit = max_attempts_for_dependency(dependency_budgets, dep, fallback=max_attempts)
                used = sum(
                    1
                    for e in entries
                    if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency == dep
                )
                if limit is not None and used >= limit:
                    return True
            return False

        limit = _ceiling(max_attempts, global_max_attempts)
        if resolved is not None:
            used = sum(
                1
                for e in entries
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == resolved
            )
        else:
            # The run-wide threshold counts attempts that tagged no dependency,
            # so per-dependency spending cannot trip it.
            used = sum(
                1
                for e in entries
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency is None
            )
        return used >= limit

    def budget(
        self,
        run_id: str,
        *,
        scope: Iterable[str] | None = None,
        max_attempts: int = 3,
        global_max_attempts: int | None = None,
        dependency: str | None = None,
        dependencies: Iterable[str] | None = None,
        contract: RecoveryContract | None = None,
        action: Any | None = None,
        dependency_budgets: Mapping[str, Any] | None = None,
    ) -> BudgetStatus:
        """The budget one bucket has left, before it has to escalate to a human.

        ``max_attempts`` is the caller's own limit and ``global_max_attempts`` the
        run-wide ceiling; the effective limit is the smaller of the two, so a
        per-scope allowance above the ceiling buys nothing. The ``attempts`` field
        is what the ledger actually charged, which may already be past
        ``max_attempts`` if the ceiling moved.
        """
        if dependency_budgets is None:
            try:
                from continuum.budgets import DEFAULT_BUDGETS_PATH, load_budgets

                dependency_budgets = load_budgets(Path(DEFAULT_BUDGETS_PATH))
            except Exception:
                dependency_budgets = None

        resolved = resolve_scope(scope) if scope is not None else None
        limit = _ceiling(max_attempts, global_max_attempts)
        entries = self.entries(run_id)
        if resolved is not None:
            used = sum(
                1
                for e in entries
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.scope == resolved
            )
        else:
            used = sum(
                1
                for e in entries
                if e.kind == LedgerEntryKind.ATTEMPT.value and e.dependency is None
            )
        escalated = any(
            e.kind == LedgerEntryKind.GATE.value
            and e.gate == HUMAN_REQUIRED
            and _marker_covers(e, resolved)
            for e in entries
        )
        return BudgetStatus(
            scope=resolved,
            attempts=used,
            max_attempts=limit,
            escalated=escalated,
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
