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

__all__ = [
    "ActionLedger",
    "ActionOutcome",
    "LedgerError",
    "DuplicateAction",
]


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
        actions: dict[str, Action] = {}
        for event in self.storage.read_events(self.run_id):
            if event.type not in (
                EventType.ACTION_RECORDED,
                EventType.ACTION_RECONCILED,
                EventType.ACTION_COMPENSATED,
            ):
                continue
            payload = dict(event.payload)
            key = str(payload.get("key", ""))
            if not key:
                continue
            actions[key] = Action.model_validate(payload["action"])
        return actions

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
            known = (
                leaf_tokens(identity_tokens(action.arguments, external_id=action.external_id))
                - plumbing
            )
            # An empty ``known`` is contained in everything; treat a stored
            # action with no identity of its own as unrecognisable, not as a
            # match for every claim of the same type.
            if not known:
                continue
            if not (incoming <= known or known <= incoming):
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
    ) -> ActionOutcome:
        """Register intent to perform an action, or report it already happened.

        Returns ``fresh=True`` when the caller should go ahead. Returns
        ``fresh=False`` with the stored result when the action already
        completed.

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
