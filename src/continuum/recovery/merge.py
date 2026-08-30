"""Merge through the shared precondition gate (issue #408).

Merge combines history from a source run into a target run at a common
anchor. Like fork and restore it routes through the shared gate in
``gate.py`` so refusal shape and lineage stamping are identical. For merge
the gate checks the span ``(anchor, head]`` on the target run; depended
results retain the fork semantics (full derived set) because merge does not
reactivate history the way restore does. The only per-edit difference for
depended results is restore, documented in ``gate.py``.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.recovery.gate import (
    EditPreconditionError,
    check_merge_preconditions,
    check_preconditions,
)
from continuum.storage.base import Storage

__all__ = [
    "MergePreconditionError",
    "approve_merge",
    "merge_to_anchor",
]


class MergePreconditionError(EditPreconditionError):
    """Alias for :class:`EditPreconditionError` with ``edit_type == "merge"``."""

    pass


def merge_to_anchor(
    storage: Storage,
    run_id: str,
    anchor: int,
    *,
    reason: str,
    carry_forward: Collection[str] | None = None,
    source_run_id: str | None = None,
    source_anchor: int | None = None,
    source_anchor_sequence: int | None = None,
) -> tuple[Any, set[str], dict[str, Any]]:
    """Check preconditions for merging into ``run_id`` at ``anchor``.

    When ``source_run_id`` is given both sides are derived: target
    ``(anchor, target_head]`` and source
    ``(source_anchor, source_head]`` where ``source_anchor`` is the explicit
    ancestor or ``storage.latest_version(source_run_id).source_sequence`` when
    absent. Merge refuses if either side has unaccounted preconditions (union).
    ``carry_forward`` may name items from either side (key, action_id or
    sequence). Per-edit filtering keeps fork semantics for depended_results.
    """
    if source_anchor is None and source_anchor_sequence is not None:
        source_anchor = int(source_anchor_sequence)
    if source_run_id is None:
        return check_preconditions(
            storage,
            run_id,
            anchor,
            edit_type="merge",
            carry_forward=carry_forward,
        )
    union, carry_set, union_summary, _t, _s = check_merge_preconditions(
        storage,
        target_run_id=run_id,
        target_anchor=anchor,
        source_run_id=source_run_id,
        source_anchor=source_anchor,
        carry_forward=carry_forward,
    )
    return union, carry_set, union_summary


def approve_merge(
    storage: Storage,
    run_id: str,
    *,
    source_run_id: str | None = None,
    anchor_sequence: int | None = None,
    source_anchor_sequence: int | None = None,
    reason: str,
    carry_forward: Collection[str] | None = None,
) -> Run:
    """Approve a merge into ``run_id`` at ``anchor_sequence``.

    Both sides are derived when ``source_run_id`` is given: target
    ``(anchor, target_head]`` and source
    ``(source_anchor, source_head]`` where ``source_anchor`` is the common
    ancestor or, when absent, ``storage.latest_version(source_run_id).source_sequence``.
    If ``source_run_id`` is None only the target side is checked (backward
    compat). Merge refuses if either side has unaccounted unsettled
    authorizations, depended results or uncertain slots (union); ``carry_forward``
    may name items from either side (key, action_id or sequence). Depended
    filtering keeps fork semantics (full set) and cross-run references are
    considered stranded as well. Rationale names both sides' sequences and the
    lineage event stamps both sides' summaries.
    """
    run = storage.get_run(run_id)
    if not reason or not reason.strip():
        raise ValueError("a merge needs a stated reason; the reason is the audit")
    if source_run_id is not None:
        storage.get_run(source_run_id)

    if anchor_sequence is None:
        latest = storage.latest_version(run_id)
        anchor = latest.source_sequence if latest else 0
    else:
        anchor = int(anchor_sequence)

    if source_run_id is None:
        derivation, carry_set, summary = check_preconditions(
            storage,
            run_id,
            anchor,
            edit_type="merge",
            carry_forward=carry_forward,
        )
        payload: dict[str, Any] = {
            "reason": reason.strip(),
            "anchor_sequence": anchor,
            "divergence_sequence": anchor,
            "candidate_sequence": storage.last_sequence(run_id),
            "source_run_id": source_run_id,
            "preconditions": summary,
            "precondition_summary": summary,
            "derivation": summary,
            "derivation_summary": summary,
            "carry_forward": sorted(carry_set),
            "edit_type": "merge",
        }
        storage.append_event(
            run_id,
            EventType.RUN_MERGED,
            payload,
            source=Origin.HUMAN,
        )
        return run

    source_anchor: int | None = None
    if source_anchor_sequence is not None:
        source_anchor = int(source_anchor_sequence)
    derivation, carry_set, summary, target_summary, source_summary = check_merge_preconditions(
        storage,
        target_run_id=run_id,
        target_anchor=anchor,
        source_run_id=source_run_id,
        source_anchor=source_anchor,
        carry_forward=carry_forward,
    )
    target_head = storage.last_sequence(run_id)
    source_head = storage.last_sequence(source_run_id)
    if source_anchor is None:
        latest_src = storage.latest_version(source_run_id)
        resolved_source_anchor = latest_src.source_sequence if latest_src else 0
    else:
        resolved_source_anchor = source_anchor
    payload = {
        "reason": reason.strip(),
        "anchor_sequence": anchor,
        "divergence_sequence": anchor,
        "candidate_sequence": target_head,
        "target_anchor_sequence": anchor,
        "target_candidate_sequence": target_head,
        "source_run_id": source_run_id,
        "source_anchor_sequence": resolved_source_anchor,
        "source_candidate_sequence": source_head,
        "preconditions": summary,
        "precondition_summary": summary,
        "derivation": summary,
        "derivation_summary": summary,
        "target_preconditions": target_summary,
        "source_preconditions": source_summary,
        "target_summary": target_summary,
        "source_summary": source_summary,
        "carry_forward": sorted(carry_set),
        "edit_type": "merge",
    }
    storage.append_event(
        run_id,
        EventType.RUN_MERGED,
        payload,
        source=Origin.HUMAN,
    )
    return run
