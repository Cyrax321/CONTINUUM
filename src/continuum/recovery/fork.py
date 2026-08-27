"""Fork semantics: audited divergent continuations (issue #259) with precondition gate (#407).

The replay-safety triad has three outcomes at the tool boundary. REPLAY
exists (#237, ``protected_call`` returns the journalled result). REJECT
exists (the gate refuses unclaimed or uncertain effects). FORK did not:
when a restored agent legitimately re-plans and emits a call whose intent
genuinely differs from anything journalled, blocking it forever is wrong,
but executing it silently inside the original run hides the divergence.
Codex CLI ships context-only session forks with no durability; ACRFence
(arXiv:2603.20625) names replay-or-fork but never implemented it. This
module owns the third outcome, approval-first:

- :func:`detect_fork_candidates` recognises the interesting refusals. A
  gated call denied as unclaimed is a *fork candidate* when its resource
  tokens overlap a journalled action of the same type: the agent is
  redoing work it remembers under different parameters, which after a
  restore is exactly what legitimate divergence looks like. No overlap
  means an ordinary first-seen effect and no signal.
- :func:`approve_fork` executes an approved divergence: a linked child run
  (``parent_run_id``, reusing the #243 hierarchy) plus a ``RUN_FORKED``
  event on the parent log recording child, reason and divergence point.
  The parent chain stays append-only and untouched otherwise.

Approval-first is deliberate: automatic branching would let an injected
prompt steer topology silently. A human names the reason, and the reason
is the audit. Forks parent onto the named run; a fork of a fork is just a
deeper hierarchy, which the #243 roll-up already understands.

Precondition gate (#407, epic #389)
-----------------------------------

Before the fork is recorded, the module derives what the edit must account
for (``src/continuum/recovery/preconditions.py``). The derivation is pure
and read-only; this module is the decision point that enforces it
fail-closed:

* any ``unsettled_authorizations``, ``depended_results`` or
  ``uncertain_slots`` reported in ``(divergence, head]`` blocks the fork
  unless the caller explicitly asserts ``carry_forward`` for that exact
  item;
* refusals carry a machine-readable rationale naming the offending
  sequence numbers and action or approval ids;
* allowed forks stamp the derivation summary and the explicit
  carry-forward assertion onto the ``RUN_FORKED`` lineage event so the
  audit shows why the edit was safe.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from continuum.actions.idempotency import identity_tokens
from continuum.events import EventType
from continuum.models import Action, Origin, Run
from continuum.storage.base import Storage

__all__ = [
    "ForkNeighbour",
    "ForkPreconditionError",
    "detect_fork_candidates",
    "approve_fork",
]


@dataclass(frozen=True)
class ForkNeighbour:
    """One journalled action the denied call resembles."""

    key: str
    action_id: str
    action_type: str
    status: str
    external_id: str | None
    shared_tokens: tuple[str, ...]


class ForkPreconditionError(ValueError):
    """Fork refused because preconditions in the edit span are unaccounted for."""

    def __init__(
        self,
        message: str,
        *,
        derivation: Any,
        unaccounted: Any,
        rationale: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.derivation = derivation
        self.unaccounted = unaccounted
        self.rationale = rationale


def _action_tokens(action: Action) -> frozenset[str]:
    return identity_tokens(
        arguments=dict(action.arguments or {}),
        external_id=action.external_id,
    )


def detect_fork_candidates(
    *,
    action_type: str,
    tool_input: Mapping[str, Any],
    actions_by_key: Mapping[str, Action],
) -> list[ForkNeighbour]:
    """Journalled same-type actions sharing a resource token with this call.

    Deterministic: candidates sort by (number of shared tokens desc, key),
    so identical ledgers yield identical refusal envelopes.
    """
    incoming = identity_tokens(arguments=dict(tool_input))
    if not incoming:
        return []

    neighbours: list[ForkNeighbour] = []
    for key, action in actions_by_key.items():
        if action.action_type != action_type:
            continue
        shared = tuple(sorted(incoming & _action_tokens(action)))
        if not shared:
            continue
        neighbours.append(
            ForkNeighbour(
                key=key,
                action_id=action.action_id,
                action_type=action.action_type,
                status=action.status.value,
                external_id=action.external_id,
                shared_tokens=shared,
            )
        )
    neighbours.sort(key=lambda n: (-len(n.shared_tokens), n.key))
    return neighbours


def _summary_payload(derivation: Any) -> dict[str, Any]:
    """JSON-native summary of a derivation for the lineage event."""
    return {
        "unsettled_authorizations": sorted(
            [
                {
                    "approval_id": item.approval_id,
                    "subject": item.subject,
                    "sequence": item.sequence,
                }
                for item in derivation.unsettled_authorizations
            ],
            key=lambda d: d["sequence"],
        ),
        "depended_results": sorted(
            [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "sequence": item.sequence,
                }
                for item in derivation.depended_results
            ],
            key=lambda d: d["sequence"],
        ),
        "uncertain_slots": sorted(
            [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "status": item.status,
                    "sequence": item.sequence,
                }
                for item in derivation.uncertain_slots
            ],
            key=lambda d: d["sequence"],
        ),
    }


def _is_carried(candidates: Collection[str], carry_set: set[str]) -> bool:
    """True when any candidate identifier is explicitly carried."""
    return any(c in carry_set for c in candidates)


def _unaccounted_sets(
    derivation: Any,
    carry_set: set[str],
) -> Any:
    """Derivation subset that remains unaccounted after carry_forward."""
    from continuum.recovery.preconditions import DerivationResult

    unsettled = frozenset(
        item
        for item in derivation.unsettled_authorizations
        if not _is_carried({item.approval_id, str(item.sequence)}, carry_set)
    )
    depended = frozenset(
        item
        for item in derivation.depended_results
        if not _is_carried({item.key, item.action_id, str(item.sequence)}, carry_set)
    )
    uncertain = frozenset(
        item
        for item in derivation.uncertain_slots
        if not _is_carried({item.key, item.action_id, str(item.sequence)}, carry_set)
    )
    return DerivationResult(
        unsettled_authorizations=unsettled,
        depended_results=depended,
        uncertain_slots=uncertain,
    )


def _raise_if_blocked(
    storage: Storage,
    parent_run_id: str,
    divergence: int,
    *,
    carry_forward: Collection[str] | None,
) -> tuple[Any, set[str], dict[str, Any]]:
    """Derive preconditions for ``(divergence, head]`` and raise if blocked.

    Returns the full derivation, the normalized carry set and the summary
    payload when the edit is allowed. Raises :class:`ForkPreconditionError`
    with machine-readable rationale otherwise.
    """
    from continuum.recovery.preconditions import EditPoint, derive

    head = storage.last_sequence(parent_run_id)
    if divergence > head:
        head = divergence
    point = EditPoint(
        run_id=parent_run_id,
        anchor_sequence=divergence,
        candidate_sequence=head,
    )
    derivation = derive(storage, point)
    carry_set = {str(x) for x in (carry_forward or ())}
    unaccounted = _unaccounted_sets(derivation, carry_set)

    if (
        unaccounted.unsettled_authorizations
        or unaccounted.depended_results
        or unaccounted.uncertain_slots
    ):
        parts: list[str] = []
        rationale: dict[str, Any] = {
            "divergence_sequence": divergence,
            "candidate_sequence": head,
            "unsettled_authorizations": [
                {
                    "approval_id": item.approval_id,
                    "sequence": item.sequence,
                    "subject": item.subject,
                }
                for item in sorted(unaccounted.unsettled_authorizations, key=lambda x: x.sequence)
            ],
            "depended_results": [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "sequence": item.sequence,
                }
                for item in sorted(unaccounted.depended_results, key=lambda x: x.sequence)
            ],
            "uncertain_slots": [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "status": item.status,
                    "sequence": item.sequence,
                }
                for item in sorted(unaccounted.uncertain_slots, key=lambda x: x.sequence)
            ],
            "carry_forward": sorted(carry_set),
        }
        if unaccounted.unsettled_authorizations:
            ids = ", ".join(
                f"{item.approval_id} at sequence {item.sequence}"
                for item in sorted(unaccounted.unsettled_authorizations, key=lambda x: x.sequence)
            )
            parts.append(
                f"{len(unaccounted.unsettled_authorizations)} unsettled authorization(s): {ids}"
            )
        if unaccounted.depended_results:
            ids = ", ".join(
                f"{item.action_id} (key {item.key}) at sequence {item.sequence}"
                for item in sorted(unaccounted.depended_results, key=lambda x: x.sequence)
            )
            parts.append(
                f"{len(unaccounted.depended_results)} depended result(s) would be stranded: {ids}"
            )
        if unaccounted.uncertain_slots:
            ids = ", ".join(
                f"{item.action_id} (key {item.key}, status {item.status}) at sequence {item.sequence}"
                for item in sorted(unaccounted.uncertain_slots, key=lambda x: x.sequence)
            )
            parts.append(f"{len(unaccounted.uncertain_slots)} uncertain slot(s) still open: {ids}")
        message = (
            "fork refused: preconditions in (divergence, head] are unaccounted for: "
            + "; ".join(parts)
            + ". Pass carry_forward with the identifiers you intend to carry, or reconcile first."
        )
        raise ForkPreconditionError(
            message,
            derivation=derivation,
            unaccounted=unaccounted,
            rationale=rationale,
        )
    return derivation, carry_set, _summary_payload(derivation)


def approve_fork(
    storage: Storage,
    parent_run_id: str,
    *,
    reason: str,
    child_run_id: str | None = None,
    carry_forward: Collection[str] | None = None,
) -> Run:
    """Create an approved divergent continuation of ``parent_run_id``.

    Writes ``RUN_FORKED`` to the parent log (Origin.HUMAN: a human approved
    this branch) and creates the linked child run with ``parent_run_id``
    set, so #243's aggregation and ``continuum tree`` see it without any new
    machinery. The child starts empty: it inherits nothing mutable from the
    parent, by the same rule that keeps siblings independent.

    Precondition gate (#407): the span ``(divergence, head]`` is derived via
    :func:`continuum.recovery.preconditions.derive`. Any
    unsettled authorizations, depended results or uncertain slots in that span
    blocks the fork unless each offending item's identifier appears in
    ``carry_forward``. The refusal carries machine-readable rationale naming
    sequence numbers and action or approval ids. Allowed forks stamp the
    derivation summary and the carry-forward assertion onto the lineage event
    payload for auditability.
    """
    parent = storage.get_run(parent_run_id)
    if not reason or not reason.strip():
        raise ValueError("a fork needs a stated reason; the reason is the audit")

    from continuum.storage.base import RunNotFound

    if child_run_id is None:
        existing = {r.run_id for r in storage.list_runs(limit=None)}
        n = 1
        while True:
            candidate = f"{parent_run_id}_fork{n}"
            if candidate not in existing:
                child_run_id = candidate
                break
            n += 1
    else:
        try:
            storage.get_run(child_run_id)
        except RunNotFound:
            pass
        else:
            raise ValueError(f"run {child_run_id!r} already exists")

    latest = storage.latest_version(parent_run_id)
    divergence = latest.source_sequence if latest else 0

    derivation, carry_set, summary = _raise_if_blocked(
        storage,
        parent_run_id,
        divergence,
        carry_forward=carry_forward,
    )

    child = Run(
        run_id=child_run_id,
        goal=parent.goal,
        parent_run_id=parent_run_id,
        metadata={
            "fork": "true",
            "fork_reason": reason.strip(),
            "fork_parent_sequence": divergence,
        },
    )
    # create_run_started, not create_run: the child needs its own RUN_STARTED or
    # it has a row and an empty log, which nothing downstream can read. project()
    # refuses a log that never recorded RUN_STARTED, so `resume`, `tree`,
    # `replay` and `inspect` all fail on the child -- including the exact command
    # this function's own caller prints as the next step. Same defect class as
    # #47, which fixed it for the OpenAI adapter. The row and its first event are
    # one fact, so they are written in one transaction.
    #
    # Origin.HUMAN matches the RUN_FORKED event below: a person approved this
    # branch, and the child's goal is inherited from a run a human already
    # stated, so it is not an agent self-report.
    storage.create_run_started(child, source=Origin.HUMAN)
    # Stash derivation summary and explicit carry-forward on the lineage event.
    # Multiple keys are written for forward compatibility: "preconditions",
    # "precondition_summary" and "derivation" all carry the same payload so
    # readers keying on any one name see the audit.
    payload: dict[str, Any] = {
        "child_run_id": child.run_id,
        "reason": reason.strip(),
        "divergence_sequence": divergence,
        "preconditions": summary,
        "precondition_summary": summary,
        "derivation": summary,
        "derivation_summary": summary,
        "carry_forward": sorted(carry_set),
    }
    storage.append_event(
        parent_run_id,
        EventType.RUN_FORKED,
        payload,
        source=Origin.HUMAN,
    )
    return child
