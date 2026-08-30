"""Pure precondition derivation over event prefixes (issue #406, epic #389).

Before fork, restore or merge executes, something has to answer one question:
what does this edit have to account for? This module derives that answer from
the run record alone. It is a read-only fold in the same family as
``state.semantic.project``, held to the same two properties and tested for
them:

**Purity.** Storage is only ever read (the archived and live event streams,
plus the run row's existence). There are no writes, no mode changes and no
clock reads here, because a derivation that nudged the world while inspecting
it could not be trusted at the decision points (#407, #408) that consume it.

**Determinism.** The same prefix always yields an equal result. Nothing reads
wall-clock time, iterates unordered storage state without sorting, or consults
anything outside the events at or before the candidate point.

What is derived
---------------

Three sets, every item carrying the sequence number of the event that asserts
the fact the item reports:

* ``unsettled_authorizations``: approvals granted inside the span and never
  revoked through the candidate point. These are live permissions that would
  silently carry into a new branch, so the same effect could be authorized
  twice across the two halves of the edit.
* ``depended_results``: actions completed inside the span whose recorded
  outcome a surviving later step still references, by ledger key or action id.
  An edit that discards such a completion strands the step waiting on it.
* ``uncertain_slots``: ledger slots opened inside the span still holding an
  unresolved outcome (``STARTED`` or ``UNKNOWN``, matching
  ``ActionLedger.pending()``) at the candidate point. The outside world may or
  may not have been changed; no branch may be cut across that doubt.

Scope rules
-----------

The span reported on is the half-open range ``(anchor_sequence,
candidate_sequence]``. Facts at or before the anchor survive any edit between
the two points intact, so they are not the edit's to account for. Everything
after the candidate is presumed rewritten by the edit and is invisible here:
settlement is honoured only up to the candidate, so a revocation or
reconciliation recorded after the proposed edit point cannot settle anything
inside the span.

Each set qualifies by its own characteristic event lying in the span: the
grant for authorizations, the completion for depended results, the slot's
opening record for uncertain slots. Membership is deliberately judged at the
event that creates the risk, not at later restatements of it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from heapq import merge
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from continuum.events import Event, EventType
from continuum.models import Action, ActionStatus
from continuum.storage.base import Storage

__all__ = [
    "DependedResult",
    "DerivationResult",
    "EditPoint",
    "UncertainSlot",
    "UnsettledAuthorization",
    "derive",
]

_ACTION_EVENT_TYPES = frozenset(
    {
        EventType.ACTION_RECORDED,
        EventType.ACTION_RECONCILED,
        EventType.ACTION_COMPENSATED,
    }
)

#: Statuses whose real-world outcome is not known. Deliberately the same pair
#: ``ActionLedger.pending()`` reports, so the derivation and the ledger can
#: never disagree about what counts as outstanding.
_OPEN_SLOT_STATUSES = (ActionStatus.STARTED, ActionStatus.UNKNOWN)


class EditPoint(BaseModel):
    """Where an execution edit sits on a run's timeline.

    ``anchor_sequence`` is the point the edit is anchored to (a restore
    target, a divergence base); ``candidate_sequence`` is the proposed edit
    point. Sequences start at 1 per run, so 0 means "before anything".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    anchor_sequence: int = Field(ge=0)
    candidate_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> EditPoint:
        if self.candidate_sequence < self.anchor_sequence:
            raise ValueError(
                f"candidate_sequence ({self.candidate_sequence}) is before "
                f"anchor_sequence ({self.anchor_sequence}); the derivation span "
                f"would be inverted"
            )
        return self


class UnsettledAuthorization(BaseModel):
    """An approval granted inside the span and not revoked at the candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str
    subject: str
    """What was approved, as recorded on the grant (empty if never stated)."""

    sequence: int
    """Sequence of the ``APPROVAL_GRANTED`` event."""


class DependedResult(BaseModel):
    """A completed action whose recorded outcome a later step references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    action_id: str
    action_type: str
    sequence: int
    """Sequence of the ``ACTION_*`` event asserting the completion."""


class UncertainSlot(BaseModel):
    """A ledger slot opened inside the span whose outcome is still open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    action_id: str
    action_type: str
    status: str
    sequence: int
    """Sequence of the ``ACTION_*`` event asserting the open status."""


class DerivationResult(BaseModel):
    """Everything an edit between the two points must account for.

    Empty sets mean the edit crosses nothing outstanding; they do not mean the
    edit is safe. Judging safety from these sets is the consumer's job
    (#407/#408), not this module's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unsettled_authorizations: frozenset[UnsettledAuthorization] = Field(default_factory=frozenset)
    depended_results: frozenset[DependedResult] = Field(default_factory=frozenset)
    uncertain_slots: frozenset[UncertainSlot] = Field(default_factory=frozenset)


@dataclass(slots=True)
class _SlotTrack:
    """Working state for one ledger key while folding the prefix."""

    first_seq: int
    latest_seq: int
    action: Action


def _payload_strings(payload: Mapping[str, Any]) -> frozenset[str]:
    """Every string value reachable in a JSON-native event payload.

    Whole-string equality only: identifiers are atomic tokens, and substring
    matching would manufacture dependencies out of prose that merely contains
    an id-shaped fragment. Non-string scalars cannot name an action or an
    approval and are skipped.
    """
    found: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            found.add(current)
    return frozenset(found)


def derive(storage: Storage, edit_point: EditPoint) -> DerivationResult:
    """Derive what an edit between the anchor and the candidate must account for.

    Single pass over the run's merged event prefix (archived stream first, then
    live, both already sequence-sorted), stopping at ``candidate_sequence``.
    Reads storage; never writes, never changes modes, never reads the clock.

    Raises ``RunNotFound`` for a run the store does not know: a phantom run
    would derive empty sets and read as "nothing to account for", which is a
    guess in favour of proceeding and is refused here instead.
    """
    run_id = edit_point.run_id
    anchor = edit_point.anchor_sequence
    candidate = edit_point.candidate_sequence

    # Existence check, not state use: see the docstring. A run whose events
    # have all been archived still has its run row and derives correctly.
    storage.get_run(run_id)

    def in_span(sequence: int) -> bool:
        return anchor < sequence <= candidate

    # approval_id -> (sequence of the grant, subject). A revocation removes
    # the entry; a re-grant after a revocation re-enters with the new sequence.
    granted: dict[str, tuple[int, str]] = {}
    slots: dict[str, _SlotTrack] = {}
    # Ledger key -> result item awaiting a surviving reference. Entries enter
    # on completion and leave on any later record for the same key, so the
    # newest status always decides.
    watched: dict[str, DependedResult] = {}
    depended: set[DependedResult] = set()

    stream: Iterator[Event] = merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda event: event.sequence,
    )
    for event in stream:
        if event.sequence > candidate:
            break

        strings = _payload_strings(event.payload)
        current_key: str | None = None
        if event.type in _ACTION_EVENT_TYPES:
            raw_key = event.payload.get("key")
            current_key = str(raw_key) if raw_key else None

        # References are matched against the watch as it stood before this
        # event, so an action can never be its own dependency and nothing can
        # depend on a result before it existed. A later record for the same
        # key (a compensation, a reconcile) is excluded too: settling or
        # undoing an action is not depending on it.
        if watched and strings:
            for key, item in watched.items():
                if key == current_key:
                    continue
                if key in strings or item.action_id in strings:
                    depended.add(item)

        if event.type is EventType.APPROVAL_GRANTED:
            approval_id = event.payload.get("approval_id")
            if approval_id:
                granted[str(approval_id)] = (
                    event.sequence,
                    str(event.payload.get("subject", "")),
                )
        elif event.type is EventType.APPROVAL_REVOKED:
            approval_id = event.payload.get("approval_id")
            if approval_id:
                granted.pop(str(approval_id), None)
        elif current_key is not None:
            action = Action.model_validate(event.payload["action"])
            track = slots.get(current_key)
            slots[current_key] = _SlotTrack(
                first_seq=track.first_seq if track else event.sequence,
                latest_seq=event.sequence,
                action=action,
            )
            watched.pop(current_key, None)
            updated = slots[current_key]
            # Membership is judged at the completion, the event that creates
            # the risk: an action claimed before the anchor but completed
            # inside the span loses its completion to the edit, so a surviving
            # step referencing its result is stranded exactly as much as one
            # whose whole lifecycle sat inside the span.
            if in_span(event.sequence) and updated.action.status is ActionStatus.COMPLETED:
                watched[current_key] = DependedResult(
                    key=current_key,
                    action_id=updated.action.action_id,
                    action_type=updated.action.action_type,
                    sequence=event.sequence,
                )

    return DerivationResult(
        unsettled_authorizations=frozenset(
            UnsettledAuthorization(approval_id=approval_id, subject=subject, sequence=sequence)
            for approval_id, (sequence, subject) in granted.items()
            if in_span(sequence)
        ),
        depended_results=frozenset(depended),
        uncertain_slots=frozenset(
            UncertainSlot(
                key=key,
                action_id=track.action.action_id,
                action_type=track.action.action_type,
                status=track.action.status.value,
                sequence=track.latest_seq,
            )
            for key, track in slots.items()
            if in_span(track.first_seq) and track.action.status in _OPEN_SLOT_STATUSES
        ),
    )
