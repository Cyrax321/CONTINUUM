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
from continuum.recovery.gate import EditPreconditionError, check_preconditions
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
) -> tuple[Any, set[str], dict[str, Any]]:
    """Check preconditions for merging into ``run_id`` at ``anchor``."""
    return check_preconditions(
        storage,
        run_id,
        anchor,
        edit_type="merge",
        carry_forward=carry_forward,
    )


def approve_merge(
    storage: Storage,
    run_id: str,
    *,
    source_run_id: str | None = None,
    anchor_sequence: int | None = None,
    reason: str,
    carry_forward: Collection[str] | None = None,
) -> Run:
    """Approve a merge into ``run_id`` at ``anchor_sequence``."""
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
