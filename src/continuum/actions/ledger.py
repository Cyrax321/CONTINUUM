"""The action ledger: remembering external side effects.

Storage gives durability for *state*. It cannot give exactly-once semantics for
effects on other systems, because the effect and the record of it are two
separate writes with a gap between them. The ledger's job is to make that gap
observable rather than invisible.

The protocol
------------

::

    key = ledger.claim(...)      # 1. write intent  (STARTED)
    result = do_the_thing()      # 2. perform the effect
    ledger.complete(key, ...)    # 3. record the outcome (COMPLETED)

A crash can land anywhere:

* **before 1** — nothing happened. Retry is safe.
* **between 1 and 2** — intent recorded, effect may or may not have occurred.
  On recovery the action is ``STARTED`` with no result: **the effect is of
  unknown status.** The ledger refuses to guess.
* **between 2 and 3** — the effect definitely happened but was never recorded.
  Indistinguishable from the previous case *from the ledger alone*, which is
  precisely why it must not be resolved by assumption.
* **after 3** — fully recorded. A repeat call returns the stored result.

Why not just retry?
-------------------

Retrying an unrecorded action is only safe if the operation is naturally
idempotent. Creating a GitHub issue, charging a card and sending an email are
not. Retrying duplicates them; skipping may drop them. Neither default is
correct, so the ledger raises ``UnknownSideEffect`` and requires the caller to
supply a reconciler — usually a cheap read against the external system that can
answer "did this actually happen?".

This is honest at-least-once with mandatory reconciliation, not exactly-once.
The distinction is documented rather than marketed away.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from heapq import merge
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

from continuum.actions.grants import GrantDenied, normalize_grant, scan_grants
from continuum.actions.idempotency import (
    IdempotencyKey,
    arguments_hash,
    idempotency_key,
    identity_tokens,
    leaf_tokens,
    location_tokens,
    locations_agree,
    resolve_authorization_id,
)
from continuum.budgets import (
    DEFAULT_BUDGETS_PATH,
    BudgetConfigError,
    ensure_authorization_entry,
    increment,
    load_budgets,
    save_budgets,
    would_refuse,
)
from continuum.concurrency.lease import LeaseCoordinator
from continuum.events import EventType
from continuum.models import Action, ActionStatus, UnknownSideEffect, utcnow
from continuum.security.hashing import stable_hash
from continuum.storage.base import Storage

_ACTION_EVENT_TYPES = (
    EventType.ACTION_RECORDED,
    EventType.ACTION_RECONCILED,
    EventType.ACTION_COMPENSATED,
)


def fold_action_events(events: Any) -> dict[str, Action]:
    """Fold action events into ``{key: Action}``, last write per key wins.

    Shared by :meth:`ActionLedger._replay` and the cross-run scan behind
    unscoped claims so both read the log with identical semantics.
    """
    actions: dict[str, Action] = {}
    for event in events:
        if event.type not in _ACTION_EVENT_TYPES:
            continue
        payload = dict(event.payload)
        key = str(payload.get("key", ""))
        if not key:
            continue
        actions[key] = Action.model_validate(payload["action"])
    return actions


__all__ = [
    "ActionLedger",
    "ActionOutcome",
    "LedgerError",
    "DuplicateAction",
    "ClaimLockError",
]


def _stem(token: str) -> str:
    """The basename-stem of a token, used to recognise derived spellings.

    ``INV-001.sent`` and ``INV-001`` are the same resource rendered more or less
    verbosely, so the matcher treats the former as derived from the latter. A
    token without an extension returns itself, which keeps plain words and ids
    self-derived.
    """
    stem, _ = os.path.splitext(token)
    return stem


# Extensions accepted as genuine file suffixes for the stem-derivation rule
# (issue #67, option 2). A dotted extra token is only treated as derived from
# its basename when its extension looks like a real file suffix, so a handle
# such as ``alice.smith`` is not collapsed into ``alice`` while legitimate
# renderings like ``INV-001.sent`` or ``report.csv`` still deduplicate.
_KNOWN_SUFFIXES = frozenset(
    {
        "csv",
        "tsv",
        "json",
        "jsonl",
        "ndjson",
        "parquet",
        "pq",
        "avro",
        "orc",
        "xlsx",
        "xls",
        "pdf",
        "txt",
        "md",
        "html",
        "htm",
        "xml",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "webp",
        "zip",
        "gz",
        "tar",
        "tgz",
        "bz2",
        "rar",
        "7z",
        "db",
        "sqlite",
        "sql",
        "arrow",
        "feather",
        "pkl",
        "pickle",
        "bin",
        "log",
        "dat",
        "out",
        "tmp",
        "part",
        "bak",
        "old",
        "eml",
        "msg",
        "wav",
        "mp3",
        "mp4",
        "mov",
        "sent",
    }
)


def _superset_derives_from_subset(subset: frozenset[str], superset: frozenset[str]) -> bool:
    """True when every token present only in ``superset`` is a stem-extended form
    of a token in ``subset`` whose extension looks like a genuine file suffix.

    Containment alone is not enough: a completed action folds its ``external_id``
    and any optional descriptive argument into its token set, so the stored set
    is a *superset* of a sparser re-claim even when the two are genuinely
    different work. The only safe superset is one whose extra tokens are derived
    from the shared ones (``INV-001.sent`` from ``INV-001``), never unrelated
    values like a one-off ``message`` or the effect's ``external_id``.

    The derivation also requires the extension to be a known file suffix
    (``_KNOWN_SUFFIXES``): ``alice.smith`` shares the stem ``alice`` but its
    ``.smith`` extension is not a real suffix, so it is treated as distinct work
    rather than a rendering of ``alice`` (issue #67).
    """
    extra = superset - subset
    if not extra:
        return True
    stems = {_stem(token).lower() for token in subset}
    for token in extra:
        stem = _stem(token).lower()
        if stem not in stems:
            return False
        _, ext = os.path.splitext(token)
        if ext.lstrip(".").lower() not in _KNOWN_SUFFIXES:
            return False
    return True


class LedgerError(RuntimeError):
    """The ledger was used in a way that cannot be made safe."""


class DuplicateAction(LedgerError):
    """A second attempt was made while the first is still in flight."""


class ClaimLockError(LedgerError):
    """The run's lease is held elsewhere, so this ledger must not write.

    Raised instead of proceeding, because the alternative -- claiming anyway --
    is the duplicate side effect this ledger exists to prevent. A caller seeing
    this should back off and retry, not force the write: another agent owns the
    run right now, and whatever it is claiming may be the very action this
    caller wanted.
    """


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _single_writer(
    method: Callable[Concatenate[ActionLedger, _P], _R],
) -> Callable[Concatenate[ActionLedger, _P], _R]:
    """Run a mutating ledger method while holding the run's lease.

    Every method that folds the log and then appends to it is a
    read-modify-write, so every one of them races. Marking them declaratively
    keeps that list honest: a new mutator without this decorator is visibly
    missing something, whereas a forgotten ``with self._locked()`` deep inside a
    hundred-line body is not.

    A ledger built without a lease is unaffected, which is what keeps the
    single-process path exactly as it was.
    """

    @wraps(method)
    def wrapper(self: ActionLedger, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with self._locked():
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What a claim returned.

    ``fresh`` distinguishes "go ahead and perform this" from "already done,
    here is the previous result" — the single most important bit for callers.
    """

    key: IdempotencyKey
    action: Action
    fresh: bool

    @property
    def already_completed(self) -> bool:
        return not self.fresh and self.action.status is ActionStatus.COMPLETED

    @property
    def result(self) -> Mapping[str, Any] | None:
        return self.action.result

    @property
    def external_id(self) -> str | None:
        return self.action.external_id


class ActionLedger:
    """Durable record of external side effects, keyed by idempotency.

    Actions are stored as events, so the ledger inherits the event log's
    ordering, durability and tamper-evidence rather than inventing its own.

    Single-writer per run. Deduplication is a claim-then-check against the
    folded log, not an atomic compare-and-set, so two processes claiming the
    same key at the same instant both read "no prior slot" and both open one.
    The ``docs/multi_agent_isolation.md`` ownership model applies: one run, one
    owner at a time.

    Pass ``lease`` to have the ledger enforce that itself. Every mutating
    method then acquires the run's lease for ``holder_id`` before it folds the
    log, and releases it after the append, so concurrent claimants on one key
    collapse to a single winner; the losers raise :class:`ClaimLockError`
    rather than opening a parallel slot. ``holder_id`` is required alongside a
    lease and must be a stable agent identity, not a shared constant: a default
    would make every process look like the same holder and quietly disable the
    protection it was passed to provide.

    The lease is reentrant for its own holder, so the documented pattern of a
    caller acquiring the run lease and *then* using the ledger still works --
    the ledger recognises the lease as already its own, and leaves releasing it
    to whoever acquired it.

    Without ``lease`` the behaviour is exactly as before: unsynchronised, and
    only as strong as the caller's own serialization. That remains the default
    because the single-process path has nothing to serialise against.

    Two limits are worth stating rather than implying. The lease is scoped to
    one ``run_id``, so an unscoped claim (``scoped_to_run=False``) racing the
    same key from *another* run is not covered by this run's lease; both
    claimants would need to agree on a lease to be safe. And the claim is still
    not atomic in storage, so a caller that mixes leased and unleased ledgers on
    one run gets the weaker guarantee.
    """

    def __init__(
        self,
        storage: Storage,
        run_id: str,
        *,
        lease: LeaseCoordinator | None = None,
        holder_id: str | None = None,
        ttl: timedelta | None = None,
    ) -> None:
        if lease is not None and not holder_id:
            raise ValueError(
                "holder_id is required when a lease is supplied: a shared default "
                "would make two processes appear to be the same lease holder and "
                "silently defeat the serialization. Pass a stable agent identity."
            )
        self.storage = storage
        self.run_id = run_id
        self._lease = lease
        self._holder_id = holder_id or ""
        self._ttl = ttl

    # -- single-writer ---------------------------------------------------- #

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the run's lease for the duration of one mutating call.

        Reentrant for the lease's own holder. A caller that already acquired the
        run lease (the pattern ``docs/multi_agent_isolation.md`` describes, and
        the one ``continuum serve`` follows) passes straight through, and the
        lease is left for that caller to release. Without this, the ledger would
        fail every claim made by an agent that had correctly taken ownership of
        the run first, which is precisely the well-behaved caller.
        """
        if self._lease is None:
            yield
            return
        if self._lease.is_held(self.run_id, self._holder_id):
            # Already ours. Do not release on the way out: the outer holder owns
            # the lease's lifetime and may still need it.
            yield
            return
        if not self._lease.acquire(self.run_id, self._holder_id, self._ttl):
            raise ClaimLockError(
                f"run {self.run_id!r} is leased to {self._lease.holder(self.run_id)!r}; "
                f"{self._holder_id!r} must not write to the action ledger while another "
                f"agent owns the run. Retry once the lease is free."
            )
        try:
            yield
        finally:
            self._lease.release(self.run_id, self._holder_id)

    # -- budget drawdown (issue #413) ------------------------------------- #

    def _budget_path(self) -> Path:
        """Registry path, overridable for tests via env."""
        return Path(os.environ.get("CONTINUUM_BUDGETS_PATH", DEFAULT_BUDGETS_PATH))

    def _budget_authorization_id(
        self,
        action_type: str,
        key: str | None,
        arguments: Mapping[str, Any] | None,
        volatile: Sequence[str],
    ) -> str | None:
        """Stable authorization bucket for this operation, or None when unbound.

        For budgets the bucket must survive fresh-key rotation (issue #390):
        two attempts with different idempotency keys but the same resource
        tokens (e.g. same invoice id) are the same authorization and must
        draw down the same counter.  Passing the explicit key would give
        each fresh key its own bucket and defeat the cap-amplification fix
        (#413), so the helper ignores an explicit key and derives from
        resource tokens alone. Ledger anchoring is not used here so a
        prior failed attempt does not make a retry look unbound.
        """
        try:
            return resolve_authorization_id(
                action_type, None, arguments, volatile=volatile, ledger=None
            )
        except Exception:
            return None

    def _budget_consume_claim(
        self,
        action_type: str,
        authorization_id: str,
    ) -> None:
        """Consume one authorization-bound budget slot for a fresh attempt.

        Fail closed: an unreadable or malformed registry refuses the claim
        rather than letting it proceed with no accounting. All writes go
        through the pure helpers from ``budgets.py``.

        When the registry file does not exist, budgets are treated as
        unconfigured and no drawdown happens. This keeps runs and tests
        without authorization data byte-identical to today while still
        enforcing caps once an operator creates the file.
        """
        path = self._budget_path()
        if not path.exists():
            return
        try:
            raw = load_budgets(path)
        except BudgetConfigError as exc:
            raise LedgerError(f"budget registry invalid: {exc}") from exc
        ensure_authorization_entry(raw, action_type, authorization_id)
        refused, reason = would_refuse(raw, action_type, authorization_id)
        if refused:
            remaining = 0
            try:
                from continuum.budgets import get_remaining as _get_rem

                remaining = _get_rem(raw, action_type, authorization_id) or 0
            except Exception:
                remaining = 0
            raise LedgerError(
                f"budget exhausted for {action_type!r} / {authorization_id!r} "
                f"({reason}, remaining {remaining})"
            )
        increment(raw, action_type, authorization_id)
        save_budgets(path, raw)

    def _budget_consume_settlement(
        self,
        action_type: str,
        authorization_id: str,
    ) -> None:
        """Consume one slot for a confirmation/settlement event.

        Settlements share the same per-authorization counter as claims, so a
        completed confirmation visibly draws down the budget and a rapid
        complete/re-claim cannot amplify the cap. Failures to read or write
        the registry are swallowed here: a settlement must land even if the
        budget file is momentarily unreadable, otherwise the ledger would
        refuse to record that an effect happened.
        """
        path = self._budget_path()
        if not path.exists():
            return
        try:
            raw = load_budgets(path)
        except Exception:
            return
        try:
            ensure_authorization_entry(raw, action_type, authorization_id)
        except Exception:
            return
        try:
            increment(raw, action_type, authorization_id)
        except Exception:
            return
        try:
            save_budgets(path, raw)
        except Exception:
            return

    # -- reading ---------------------------------------------------------- #

    def _replay(self) -> dict[str, Action]:
        """Rebuild the ledger by folding action events. Cheap and verifiable.

        Archived events (compaction, issue #239) fold too: a claim settled
        before compaction must keep protecting afterwards, or exactly-once
        would quietly reset at the anchor boundary and a month-old side
        effect could fire a second time. Both streams are already sequence-
        sorted, so they merge linearly instead of paying a re-sort on this
        hot path.
        """
        merged = merge(
            self.storage.read_archived_events(self.run_id),
            self.storage.read_events(self.run_id),
            key=lambda e: e.sequence,
        )
        return fold_action_events(merged)

    def folded(self) -> dict[str, Action]:
        """Public view of the ``key -> newest action`` fold, archive included."""
        return self._replay()

    def _foreign_action(self, key: str) -> Action | None:
        """Find ``key`` in another run's ledger, for unscoped claims.

        An unscoped idempotency key carries no run prefix, so the same key is
        directly comparable across runs. When a claim declares itself
        run-global (``scoped_to_run=False``), honouring that promise requires
        looking at every other run in the store before opening a fresh slot,
        not just this run's log. Returns the most recent action recorded under
        ``key`` outside this run, or None.

        Engines that maintain the action index (issue #216) answer this as an
        indexed read; the rest pay the historical scan, O(total logged
        events), only on the unscoped path after the local lookup missed.
        """
        if getattr(self.storage, "supports_action_index", False):
            return self.storage.foreign_action(key, exclude_run=self.run_id)
        found: Action | None = None
        for run in self.storage.list_runs():
            if run.run_id == self.run_id:
                continue
            folded = fold_action_events(self.storage.read_events(run.run_id))
            candidate = folded.get(key)
            if candidate is not None:
                found = candidate
        return found

    def get(self, key: str) -> Action | None:
        return self._replay().get(key)

    def resolve_key(self, identifier: str) -> str | None:
        """The ledger key for ``identifier``, which may be a key or an ``action_id``.

        The ledger is keyed by idempotency key, but almost everything a caller
        reads back is keyed by ``action_id``: ``Action.action_id`` itself, the
        recovery plan's ``reconcile_action:<target>`` steps, the contract's
        ``required_actions``, and the rendered report. So the identifier a
        recovering caller has in hand is usually the one the settle methods did
        not accept, and the two are indistinguishable by shape (issue #367).

        Resolving both here rather than at one call site means every settle
        method inherits it, and the recovery guidance that names an ``action_id``
        becomes executable as written instead of needing to be rewritten in terms
        of an identifier no output exposes.

        The mapping is unambiguous: one key holds one action, and a re-claim after
        FAILED or COMPENSATED copies the existing action, so ``action_id`` stays
        with its key rather than being reissued. Returns ``None`` when neither
        space matches.
        """
        folded = self._replay()
        if identifier in folded:
            return identifier
        for stored_key, action in folded.items():
            if action.action_id == identifier:
                return stored_key
        return None

    def _identity_match(
        self,
        action_type: str,
        arguments: Mapping[str, Any] | None,
        volatile: Sequence[str],
    ) -> tuple[IdempotencyKey, Action] | None:
        """Defensive recognition of an already-recorded action despite argument drift.

        The idempotency key hashes arguments verbatim, so an agent that renames
        an argument field (``target`` vs ``outbox_file``) or reformats a path
        between sessions computes a different key and the exact lookup misses.
        As a fallback, when the caller supplied no explicit key, a completed or
        interrupted action of the same type is recognised by shared identity
        tokens (scalar values plus path basenames and external ids) rather than
        by the full argument shape.

        Recognition requires one token set to *contain* the other, not merely to
        intersect it, and it compares leaf tokens so a path counts as its
        basename rather than its full spelling (see ``leaf_tokens``). Drift makes
        a description more or less verbose about the same resource, so the
        sparser set is a subset of the richer one (``{INV-001}`` inside
        ``{INV-001, INV-001.sent}``). Two genuinely different resources each
        carry a leaf the other lacks, which is what keeps two tickets that merely
        share ``urgent`` from collapsing into one. Under mere intersection the
        second side effect would be silently swallowed -- the exact failure this
        ledger exists to prevent.

        Leaf comparison alone was not enough either, for the same reason in a
        different disguise: two files with the same name in different directories
        share every leaf, so ``/tenants/acme/report.csv`` matched
        ``/tenants/globex/report.csv`` and globex was never notified (issue #365).
        A match therefore also requires the *locations* to agree, which
        ``locations_agree`` decides by suffix rather than equality so the drift
        case that motivated leaf comparison (``invoices/INV-5.pdf`` for
        ``/data/invoices/INV-5.pdf``) still matches. A side carrying no path at
        all makes no claim about location and so contradicts nothing.

        Containment on its own is still too loose: a completed action folds its
        outcome ``external_id`` and any optional descriptive argument into its
        token set, so the stored set is a *superset* of a sparser re-claim even
        when the two are different work. The superset is therefore only accepted
        when every token it carries beyond the subset is *derived* from the
        subset -- a path stem (``INV-001.sent`` from ``INV-001``), not an
        unrelated value such as a one-off ``message``. The outcome ``external_id``
        is excluded from the comparison entirely, because it is recorded by
        ``complete()`` and never present on the incoming claim, so it could only
        ever manufacture a false superset (issue #64).

        Returns ``(key, action)`` for the unique same-type match, or ``None``
        when nothing is distinctive enough to be confident. ``None`` is also
        returned when several actions share a token, because guessing which one
        the caller means is exactly the quiet failure mode this exists to avoid.
        """
        # The run id rides along inside arguments as ``continuum_run_id`` and is
        # common to every claim in the run, so it must never count as a
        # resource token when deciding whether two claims are the same work.
        plumbing = leaf_tokens(identity_tokens(external_id=self.run_id))
        incoming_all = identity_tokens(arguments, volatile=volatile)
        incoming = leaf_tokens(incoming_all) - plumbing
        incoming_where = location_tokens(incoming_all)
        if not incoming:
            return None

        completed: list[tuple[IdempotencyKey, Action]] = []
        uncertain: list[tuple[IdempotencyKey, Action]] = []
        for stored_key, action in self._replay().items():
            if action.action_type != action_type:
                continue
            # The ``external_id`` is an *outcome* recorded by ``complete()`` and
            # is never present on the incoming claim, so folding it into ``known``
            # would make the stored set a systematic superset of every sparser
            # re-claim. It is therefore excluded from the comparison (issue #64).
            known_all = identity_tokens(action.arguments)
            known = leaf_tokens(known_all) - plumbing
            # An empty ``known`` is contained in everything; treat a stored
            # action with no identity of its own as unrecognisable, not as a
            # match for every claim of the same type.
            if not known:
                continue
            # Same leaves, different directories, is different work (issue #365).
            if not locations_agree(incoming_where, location_tokens(known_all)):
                continue
            if incoming <= known:
                # The stored action carries more tokens than the claim. That is
                # only safe when the extra tokens are derived from the shared
                # ones (a path stem, an ``external_id`` shape), never unrelated
                # optional arguments.
                if not _superset_derives_from_subset(incoming, known):
                    continue
            elif known <= incoming:
                if not _superset_derives_from_subset(known, incoming):
                    continue
            else:
                continue
            if action.status in (ActionStatus.STARTED, ActionStatus.UNKNOWN):
                uncertain.append((IdempotencyKey(stored_key), action))
            elif action.status is ActionStatus.COMPLETED:
                completed.append((IdempotencyKey(stored_key), action))

        if len(completed) == 1:
            return completed[0]
        if len(completed) > 1:
            return None
        if len(uncertain) == 1:
            return uncertain[0]
        return None

    def all(self) -> Sequence[Action]:
        return list(self._replay().values())

    def pending(self) -> Sequence[Action]:
        """Actions whose real-world outcome is not known.

        These are exactly the actions a recovering agent must reconcile before
        it is safe to continue.
        """
        return [
            action
            for action in self._replay().values()
            if action.status in (ActionStatus.STARTED, ActionStatus.UNKNOWN)
        ]

    # -- writing ---------------------------------------------------------- #

    def _record(
        self,
        key: str,
        action: Action,
        event_type: EventType = EventType.ACTION_RECORDED,
        pinning: dict[str, str] | None = None,
        grant: dict[str, str] | None = None,
    ) -> Action:
        payload: dict[str, Any] = {
            "key": key,
            "action_id": action.action_id,
            "action_type": action.action_type,
            "status": action.status.value,
            "external_id": action.external_id,
            "action": action.model_dump(mode="json"),
        }
        if pinning:
            # Issue #241: caller-asserted environment hashes ride on the
            # STARTED record so drift is diffable per attempt. Settlements
            # omit it; the fold keeps the newest non-empty anyway.
            payload["pinning"] = dict(pinning)
        if grant:
            # Issue #269: single-use authority reference attached at claim
            # time; terminal records inherit it via the shared payload keys,
            # so scan_grants can mark consumption from either event type.
            payload["grant"] = dict(grant)
        self.storage.append_event(self.run_id, event_type, payload)
        return action

    @_single_writer
    def claim(
        self,
        action_type: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        volatile: Sequence[str] = (),
        scoped_to_run: bool = True,
        key: str | None = None,
        on_unknown: Callable[[Action], ActionOutcome | None] | None = None,
        dep_scope: str | None = None,
        pinning: dict[str, str] | None = None,
        grant: Mapping[str, str] | None = None,
    ) -> ActionOutcome:
        """Register intent to perform an action, or report it already happened.

        ``pinning`` (issue #241) is an optional validated dict of environment
        hashes/ids recorded verbatim in the ACTION_RECORDED payload so replay
        correctness can diff the agent's moving parts across a run.

        ``grant`` (issue #269) attaches a single-use authority reference,
        ``{"id": ..., "scope": ...}``, to this attempt. A grant whose attempt
        reached a terminal status counts as consumed: a later claim carrying
        it is refused with GrantDenied and an audited GRANT_DENIED event,
        which is what stops a restored agent from resurrecting spent
        authority (the Authority Resurrection attack class). A live mid-flight
        retry under the same key and grant is untouched.

        Returns ``fresh=True`` when the caller should go ahead. Returns
        ``fresh=False`` with the stored result when the action already
        completed.

        With ``scoped_to_run=False`` the key carries no run prefix and is
        honoured store-wide: a completed record in any run deduplicates the
        claim, and an unresolved attempt in another run raises
        ``UnknownSideEffect`` rather than opening a parallel slot (issue 34).

        Raises ``UnknownSideEffect`` when a previous attempt was interrupted and
        its real-world outcome cannot be determined, unless ``on_unknown``
        resolves it.
        """
        explicit_key = key is not None
        idem = idempotency_key(
            action_type,
            arguments,
            scope=self.run_id if scoped_to_run else None,
            volatile=volatile,
            key=key,
        )
        key = idem
        existing = self.get(key)

        if existing is None and not scoped_to_run:
            # The local log has no such action, but an unscoped key claims
            # global identity: another run may already hold it (issue 34).
            foreign = self._foreign_action(key)
            if foreign is not None:
                if foreign.status is ActionStatus.COMPLETED:
                    # The effect already happened under this identity, wherever
                    # it happened. Report it instead of duplicating it.
                    return ActionOutcome(key=key, action=foreign, fresh=False)
                if foreign.status in (ActionStatus.STARTED, ActionStatus.UNKNOWN):
                    # Another run is mid-flight on the same identity and this
                    # ledger cannot reconcile a foreign record (its outcome
                    # belongs to that run's log), so refuse rather than guess.
                    raise UnknownSideEffect(
                        f"action {foreign.action_type!r} (key {key[:12]}...) has an "
                        f"unresolved attempt recorded by another run; reconcile "
                        f"that run before claiming the same unscoped identity."
                    )
                # FAILED or COMPENSATED elsewhere means no live effect stands
                # in the way; this run may open its own slot.

        if existing is None and not explicit_key:
            # No explicit key was supplied (the caller did not assert an
            # identity), and the exact argument-hash lookup missed. Recognise
            # an already-recorded attempt by shared identity tokens before
            # opening a brand-new slot, so argument drift between sessions does
            # not turn a completed action into a fresh proceed=true.
            matched = self._identity_match(action_type, arguments, volatile)
            if matched is not None:
                existing = matched[1]
                # The caller will report completion or failure against the key
                # returned in the outcome, so it must be the stored key of the
                # record we are deferring to, not the freshly-derived one.
                key = matched[0]

        # Single-use grants (#269): refuse resurrection of spent authority
        # before anything fires. A live attempt carrying the same grant under
        # the same key is an ordinary mid-flight retry and passes through.
        grant_clean = normalize_grant(grant)
        if grant_clean is not None:
            spent, grants_by_key = scan_grants(self.storage.read_events(self.run_id))
            prior = spent.get(grant_clean["id"])
            live_match = (
                existing is not None
                and existing.status is ActionStatus.STARTED
                and grants_by_key.get(key, {}).get("id") == grant_clean["id"]
            )
            if prior is not None and not live_match:
                denied = GrantDenied(grant_clean["id"], prior, key)
                self.storage.append_event(
                    self.run_id,
                    EventType.GRANT_DENIED,
                    {
                        "grant_id": grant_clean["id"],
                        "scope": grant_clean["scope"],
                        "prior_action_id": prior.action_id,
                        "prior_status": prior.status,
                        "attempted_key": key,
                        "attempted_action_type": action_type,
                    },
                )
                raise denied

        # Authorization-bound budget (issue #413): derive the stable
        # authorization bucket for this attempt. Unbound (None) means no
        # budget to enforce, which keeps runs without authorization data
        # byte-identical to today. The bucket is token-derived and
        # ledger-anchored, so distinct fresh idempotency keys for the same
        # resource (same invoice id) share the same counter and cannot
        # bypass the cap by minting new keys (the #390 amplification fix).
        budget_auth_id = self._budget_authorization_id(action_type, None, arguments, volatile)

        if existing is None:
            if budget_auth_id is not None:
                self._budget_consume_claim(action_type, budget_auth_id)
            action = Action(
                run_id=self.run_id,
                action_type=action_type,
                dep_scope=dep_scope,
                arguments=dict(arguments or {}),
                arguments_hash=arguments_hash(arguments, volatile=volatile),
                status=ActionStatus.STARTED,
                started_at=utcnow(),
            )
            self._record(key, action, pinning=pinning, grant=grant_clean)
            return ActionOutcome(key=key, action=action, fresh=True)

        if existing.status is ActionStatus.COMPLETED:
            return ActionOutcome(key=key, action=existing, fresh=False)

        if existing.status is ActionStatus.COMPENSATED:
            # The effect was undone, so performing it again is legitimate.
            if budget_auth_id is not None:
                self._budget_consume_claim(action_type, budget_auth_id)
            action = existing.model_copy(
                update={
                    "status": ActionStatus.STARTED,
                    "started_at": utcnow(),
                    "result": None,
                    "result_hash": None,
                    "external_id": None,
                }
            )
            self._record(key, action)
            return ActionOutcome(key=key, action=action, fresh=True)

        if existing.status is ActionStatus.FAILED:
            if budget_auth_id is not None:
                self._budget_consume_claim(action_type, budget_auth_id)
            action = existing.model_copy(
                update={"status": ActionStatus.STARTED, "started_at": utcnow()}
            )
            self._record(key, action)
            return ActionOutcome(key=key, action=action, fresh=True)

        # STARTED or UNKNOWN: a previous attempt was interrupted.
        if on_unknown is not None:
            resolved = on_unknown(existing)
            if resolved is not None:
                # The resolution is a real decision and must outlive this call:
                # persist it so the next claim (or intercept_action) and
                # ledger.pending() reflect it instead of re-raising UnknownSideEffect.
                self._record(resolved.key, resolved.action, EventType.ACTION_RECONCILED)
                return resolved

        uncertain = existing.model_copy(
            update={"status": ActionStatus.UNKNOWN, "side_effect_uncertain": True}
        )
        self._record(key, uncertain)
        raise UnknownSideEffect(
            f"action {existing.action_type!r} (key {key[:12]}...) was interrupted before its "
            f"outcome was recorded; the side effect may or may not have occurred. "
            f"Reconcile it before retrying.",
            # The caller is being told to reconcile, so it needs the identity to
            # reconcile *with*. Truncating it into the message was the only place
            # it appeared, which left a recovering session unable to act on its
            # own instruction (issue #367).
            action_key=str(key),
            action_id=uncertain.action_id,
        )

    @_single_writer
    def complete(
        self,
        key: str,
        *,
        external_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> Action:
        """Record that the effect succeeded.

        Settles a claim that is still in flight. Re-reporting an action that is
        already ``COMPLETED`` is allowed, because a caller repeating itself after
        a dropped response is not asserting anything new.

        Every other status is refused (issue #366). Those are not settlements, they
        are corrections of a recorded outcome, and correcting an outcome needs
        evidence about the outside world that this method neither takes nor
        records. ``UNKNOWN`` is the case that matters: the action reached that
        status precisely because nobody could say whether the effect happened, and
        completing it here erased the recovery blocker, wrote no note, and left an
        ``ACTION_RECORDED`` event indistinguishable from an ordinary first-time
        success. An auditor could not tell that an uncertain charge had been
        resolved by assertion.

        :meth:`reconcile` is the supported route for all of them. It takes the
        same decision, demands the caller stand behind it, and records
        ``ACTION_RECONCILED`` with a note so the correction is visible in the log.

        Omitted arguments never erase what is on record. A caller repeating a
        completion after a dropped response usually sends only the key, and
        overwriting ``external_id`` and ``result`` with ``None`` would destroy the
        receipt proving the effect happened. Same invariant :meth:`reconcile`
        already documents, and a no-op on a first completion, where there is
        nothing yet to preserve.
        """
        key, existing = self._require(key)
        if existing.status not in (ActionStatus.STARTED, ActionStatus.COMPLETED):
            raise LedgerError(
                f"action {existing.action_type!r} is {existing.status.value}, not in flight, so "
                f"completing it would assert an outcome nothing has verified. "
                f"Check the external system, then call reconcile(occurred=True) "
                f"(continuum_reconcile_action over MCP), which records the evidence "
                f"and the note alongside the correction."
            )
        settled_external = external_id if external_id is not None else existing.external_id
        settled_result = dict(result) if result is not None else existing.result
        action = existing.model_copy(
            update={
                "status": ActionStatus.COMPLETED,
                "external_id": settled_external,
                "result": dict(settled_result) if settled_result is not None else None,
                "result_hash": (
                    stable_hash(dict(settled_result)) if settled_result is not None else None
                ),
                "completed_at": utcnow(),
                "side_effect_uncertain": False,
            }
        )
        recorded = self._record(key, action)
        # Settlement drawdown (issue #413): same per-authorization bucket as claims.
        if existing.status is ActionStatus.STARTED:
            auth_settle = self._budget_authorization_id(
                existing.action_type, None, dict(existing.arguments), ()
            )
            if auth_settle is not None:
                self._budget_consume_settlement(existing.action_type, auth_settle)
        return recorded

    @_single_writer
    def fail(self, key: str, error: str, *, certain: bool = True) -> Action:
        """Record that the effect did not happen.

        ``certain=False`` is for failures where the effect may still have landed
        — a timeout after the request was sent, for instance. Those become
        ``UNKNOWN`` rather than ``FAILED``, because a timeout is not evidence of
        absence.
        """
        key, existing = self._require(key)
        action = existing.model_copy(
            update={
                "status": ActionStatus.FAILED if certain else ActionStatus.UNKNOWN,
                "last_error": error,
                "side_effect_uncertain": not certain,
                "completed_at": utcnow() if certain else None,
            }
        )
        return self._record(key, action)

    @_single_writer
    def reconcile(
        self,
        key: str,
        *,
        occurred: bool,
        external_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        note: str = "",
    ) -> Action:
        """Resolve an uncertain action using evidence from the outside world.

        ``occurred=True`` means a check confirmed the effect exists; the action
        becomes ``COMPLETED`` and will never be repeated. ``occurred=False``
        means it confirmed absence; the action becomes ``FAILED`` and may be
        retried, and its now-falsified ``external_id`` and ``result`` are
        cleared (issue #29) so no reader trusts evidence of a completion the
        system has just decided never happened.

        Reconciling an already-COMPLETED action is deliberately permitted: an
        agent that optimistically called :meth:`complete` may be contradicted by
        a later probe, and correcting that record is the point of this method.
        The caller is trusted to have real evidence, because nothing here can
        check the outside world on its behalf.

        What is *not* permitted is losing evidence by omission. ``occurred=True``
        keeps any ``external_id`` and ``result`` already on record when the
        caller does not supply replacements, so confirming an effect happened can
        never erase the receipt proving it did.
        """
        key, existing = self._require(key)
        if occurred:
            # Fall back to what is already recorded rather than overwriting with
            # None. Confirming an effect occurred must never be the reason its
            # receipt disappears; a caller replacing the evidence passes it.
            settled_external = external_id if external_id is not None else existing.external_id
            settled_result = dict(result) if result is not None else existing.result
            action = existing.model_copy(
                update={
                    "status": ActionStatus.COMPLETED,
                    "external_id": settled_external,
                    "result": dict(settled_result) if settled_result is not None else None,
                    "result_hash": (
                        stable_hash(dict(settled_result)) if settled_result is not None else None
                    ),
                    "completed_at": utcnow(),
                    "side_effect_uncertain": False,
                    "last_error": note or existing.last_error,
                }
            )
        else:
            action = existing.model_copy(
                update={
                    "status": ActionStatus.FAILED,
                    "external_id": None,
                    "result": None,
                    "result_hash": None,
                    "side_effect_uncertain": False,
                    "last_error": note or "reconciliation found no external effect",
                }
            )
        recorded = self._record(key, action, EventType.ACTION_RECONCILED)
        # Settlement drawdown (issue #413): same bucket as claims.
        auth_settle = self._budget_authorization_id(
            existing.action_type, None, dict(existing.arguments), ()
        )
        if auth_settle is not None:
            self._budget_consume_settlement(existing.action_type, auth_settle)
        return recorded

    @_single_writer
    def compensate(self, key: str, *, note: str = "", by: str | None = None) -> Action:
        """Record that a completed effect was deliberately undone."""
        key, existing = self._require(key)
        action = existing.model_copy(
            update={
                "status": ActionStatus.COMPENSATED,
                "compensated_by": [*existing.compensated_by, by] if by else existing.compensated_by,
                "last_error": note or existing.last_error,
                "side_effect_uncertain": False,
            }
        )
        return self._record(key, action, EventType.ACTION_COMPENSATED)

    @_single_writer
    def flag_for_review(self, key: str, reason: str) -> Action:
        """Escalate an action a human must judge."""
        key, existing = self._require(key)
        action = existing.model_copy(
            update={"status": ActionStatus.REQUIRES_REVIEW, "last_error": reason}
        )
        return self._record(key, action)

    def _require(self, key: str) -> tuple[str, Action]:
        """Resolve ``key`` to its stored key and action, or explain what is wrong.

        Returns the *resolved* key alongside the action, because the caller has
        to record its settlement under the key the fold uses, not under whatever
        identifier the caller happened to hold (issue #367).

        The message names both identifier spaces. The previous wording,
        ``no action recorded for key <prefix>...``, left a caller that had passed
        a perfectly valid ``action_id`` with no way to tell that it had reached
        for the wrong identifier rather than a nonexistent action.
        """
        resolved = self.resolve_key(key)
        if resolved is None:
            known = len(self._replay())
            raise LedgerError(
                f"no action in run {self.run_id!r} matches {key[:16]!r} as either an "
                f"idempotency key or an action_id ({known} action(s) recorded). "
                f"List them with `continuum actions {self.run_id}` or "
                f"continuum_list_actions, and pass the action_key or action_id from there."
            )
        return resolved, self._replay()[resolved]
