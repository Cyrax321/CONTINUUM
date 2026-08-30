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

Gate reuse (#408)
-----------------

The precondition enforcement is now the shared gate in
``src/continuum/recovery/gate.py``, reused by restore and merge with
identical refusal shape and lineage stamping. Fork delegates to that gate
with ``edit_type="fork"`` so all three edits share one decision point.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from continuum.actions.idempotency import identity_tokens
from continuum.events import EventType
from continuum.models import Action, Origin, Run
from continuum.recovery.gate import (
    ForkPreconditionError as _GateForkError,
)
from continuum.recovery.gate import check_preconditions as _gate_check
from continuum.storage.base import Storage

__all__ = [
    "ForkNeighbour",
    "ForkPreconditionError",
    "detect_fork_candidates",
    "approve_fork",
]

ForkPreconditionError = _GateForkError


@dataclass(frozen=True)
class ForkNeighbour:
    """One journalled action the denied call resembles."""

    key: str
    action_id: str
    action_type: str
    status: str
    external_id: str | None
    shared_tokens: tuple[str, ...]


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
    """Journalled same-type actions sharing a resource token with this call."""
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


def _raise_if_blocked(
    storage: Storage,
    parent_run_id: str,
    divergence: int,
    *,
    carry_forward: Collection[str] | None,
) -> tuple[Any, set[str], dict[str, Any]]:
    """Derive preconditions for ``(divergence, head]`` and raise if blocked."""
    return _gate_check(
        storage,
        parent_run_id,
        divergence,
        edit_type="fork",
        carry_forward=carry_forward,
    )


def approve_fork(
    storage: Storage,
    parent_run_id: str,
    *,
    reason: str,
    child_run_id: str | None = None,
    carry_forward: Collection[str] | None = None,
) -> Run:
    """Create an approved divergent continuation of ``parent_run_id``."""
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
    storage.create_run_started(child, source=Origin.HUMAN)
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
    payload["edit_type"] = "fork"
    payload["anchor_sequence"] = divergence
    payload["candidate_sequence"] = storage.last_sequence(parent_run_id)
    storage.append_event(
        parent_run_id,
        EventType.RUN_FORKED,
        payload,
        source=Origin.HUMAN,
    )
    try:
        from continuum.models import AttemptLesson
        from continuum.recovery.summary import ATTEMPT_LESSON_FIELD_CAP
        from continuum.security.hashing import stable_hash

        def _trunc(text: str) -> str:
            return text[:ATTEMPT_LESSON_FIELD_CAP] if len(text) > ATTEMPT_LESSON_FIELD_CAP else text

        scar_ids = [str(item.get("action_id", "")) for item in summary.get("uncertain_slots", [])]
        scar_ids = [sid for sid in scar_ids if sid][:16]
        evidence: list[str] = []
        for item in summary.get("unsettled_authorizations", []):
            evidence.append(f"{item.get('approval_id', '')}: {item.get('subject', '')}".strip(": "))
        for item in summary.get("depended_results", []):
            evidence.append(f"{item.get('action_id', '')} ({item.get('key', '')})")
        evidence = [_trunc(str(ev)) for ev in evidence if ev][:8]
        attempt_id = stable_hash(
            {"parent": parent_run_id, "child": child.run_id, "divergence": divergence}
        )[:16]
        lesson = AttemptLesson(
            attempt_id=_trunc(attempt_id),
            falsified=_trunc(reason.strip()),
            env_delta="",
            scar_action_ids=scar_ids,
            next_avoid="",
            source_evidence=evidence,
            created_at=storage.get_run(parent_run_id).updated_at,
        )
        import json as _json

        while len(_json.dumps(lesson.model_dump(mode="json"), sort_keys=True).encode()) > 2048:
            if lesson.source_evidence:
                lesson = lesson.model_copy(update={"source_evidence": lesson.source_evidence[:-1]})
                continue
            if lesson.falsified:
                lesson = lesson.model_copy(
                    update={"falsified": lesson.falsified[: max(len(lesson.falsified) // 2, 0)]}
                )
                continue
            break
        storage.append_event(
            parent_run_id,
            EventType.ATTEMPT_LESSON,
            lesson.model_dump(mode="json"),
            source=Origin.DETERMINISTIC,
        )
        storage.append_event(
            child.run_id,
            EventType.ATTEMPT_LESSON,
            lesson.model_dump(mode="json"),
            source=Origin.DETERMINISTIC,
        )
    except Exception:
        pass
    return child
