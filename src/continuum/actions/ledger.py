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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from continuum.actions.idempotency import (
    IdempotencyKey,
    arguments_hash,
    idempotency_key,
    identity_tokens,
    leaf_tokens,
)
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
    """

    def __init__(self, storage: Storage, run_id: str) -> None:
        self.storage = storage
        self.run_id = run_id

    # -- reading ---------------------------------------------------------- #

    def _replay(self) -> dict[str, Action]:
        """Rebuild the ledger by folding action events. Cheap and verifiable."""
        return fold_action_events(self.storage.read_events(self.run_id))

    def _foreign_action(self, key: str) -> Action | None:
        """Find ``key`` in another run's ledger, for unscoped claims.

        An unscoped idempotency key carries no run prefix, so the same key is
        directly comparable across runs. When a claim declares itself
        run-global (``scoped_to_run=False``), honouring that promise requires
        looking at every other run in the store before opening a fresh slot,
        not just this run's log. Returns the most recent action recorded under
        ``key`` outside this run, or None. The scan reads each run's events
        once, so it costs O(total logged events) and is only paid on the
        unscoped path after the local lookup missed.
        """
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
        incoming = leaf_tokens(identity_tokens(arguments, volatile=volatile)) - plumbing
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
            known = leaf_tokens(identity_tokens(action.arguments)) - plumbing
            # An empty ``known`` is contained in everything; treat a stored
            # action with no identity of its own as unrecognisable, not as a
            # match for every claim of the same type.
            if not known:
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
        self, key: str, action: Action, event_type: EventType = EventType.ACTION_RECORDED
    ) -> Action:
        self.storage.append_event(
            self.run_id,
            event_type,
            {
                "key": key,
                "action_id": action.action_id,
                "action_type": action.action_type,
                "status": action.status.value,
                "external_id": action.external_id,
                "action": action.model_dump(mode="json"),
            },
        )
        return action

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
    ) -> ActionOutcome:
        """Register intent to perform an action, or report it already happened.

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

        if existing is None:
            action = Action(
                run_id=self.run_id,
                action_type=action_type,
                dep_scope=dep_scope,
                arguments=dict(arguments or {}),
                arguments_hash=arguments_hash(arguments, volatile=volatile),
                status=ActionStatus.STARTED,
                started_at=utcnow(),
            )
            self._record(key, action)
            return ActionOutcome(key=key, action=action, fresh=True)

        if existing.status is ActionStatus.COMPLETED:
            return ActionOutcome(key=key, action=existing, fresh=False)

        if existing.status is ActionStatus.COMPENSATED:
            # The effect was undone, so performing it again is legitimate.
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
            f"Reconcile it before retrying."
        )

    def complete(
        self,
        key: str,
        *,
        external_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> Action:
        """Record that the effect succeeded."""
        existing = self._require(key)
        action = existing.model_copy(
            update={
                "status": ActionStatus.COMPLETED,
                "external_id": external_id,
                "result": dict(result) if result is not None else None,
                "result_hash": stable_hash(dict(result)) if result is not None else None,
                "completed_at": utcnow(),
                "side_effect_uncertain": False,
            }
        )
        return self._record(key, action)

    def fail(self, key: str, error: str, *, certain: bool = True) -> Action:
        """Record that the effect did not happen.

        ``certain=False`` is for failures where the effect may still have landed
        — a timeout after the request was sent, for instance. Those become
        ``UNKNOWN`` rather than ``FAILED``, because a timeout is not evidence of
        absence.
        """
        existing = self._require(key)
        action = existing.model_copy(
            update={
                "status": ActionStatus.FAILED if certain else ActionStatus.UNKNOWN,
                "last_error": error,
                "side_effect_uncertain": not certain,
                "completed_at": utcnow() if certain else None,
            }
        )
        return self._record(key, action)

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
        retried.
        """
        existing = self._require(key)
        if occurred:
            action = existing.model_copy(
                update={
                    "status": ActionStatus.COMPLETED,
                    "external_id": external_id,
                    "result": dict(result) if result is not None else None,
                    "result_hash": stable_hash(dict(result)) if result is not None else None,
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
        return self._record(key, action, EventType.ACTION_RECONCILED)

    def compensate(self, key: str, *, note: str = "", by: str | None = None) -> Action:
        """Record that a completed effect was deliberately undone."""
        existing = self._require(key)
        action = existing.model_copy(
            update={
                "status": ActionStatus.COMPENSATED,
                "compensated_by": [*existing.compensated_by, by] if by else existing.compensated_by,
                "last_error": note or existing.last_error,
                "side_effect_uncertain": False,
            }
        )
        return self._record(key, action, EventType.ACTION_COMPENSATED)

    def flag_for_review(self, key: str, reason: str) -> Action:
        """Escalate an action a human must judge."""
        existing = self._require(key)
        action = existing.model_copy(
            update={"status": ActionStatus.REQUIRES_REVIEW, "last_error": reason}
        )
        return self._record(key, action)

    def _require(self, key: str) -> Action:
        existing = self.get(key)
        if existing is None:
            raise LedgerError(f"no action recorded for key {key[:12]}...")
        return existing
