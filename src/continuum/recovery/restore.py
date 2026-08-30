"""Restore through the shared precondition gate (issue #408).

Restore reactivates history at an anchor checkpoint, discarding the span
``(anchor, head]`` and replaying from the anchor. It routes through the
same precondition gate as fork, reusing the pure derivation and the
identical refusal and lineage helpers from ``gate.py``.

Per-edit-type semantics for restore are documented in ``gate.py``: a
``depended_results`` entry that is only referenced inside the discarded span
is not stranded for restore because the pair will be discarded together and
recomputed. Only results still required by the surviving prefix block the
restore, which is why the same derivation can yield an empty depended set
for restore where fork would still block.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.recovery.gate import EditPreconditionError, check_preconditions
from continuum.storage.base import Storage

__all__ = [
    "RestorePreconditionError",
    "approve_restore",
    "restore_to_anchor",
]


class RestorePreconditionError(EditPreconditionError):
    """Alias for :class:`EditPreconditionError` with ``edit_type == "restore"``."""

    pass


def _anchor_for(
    storage: Storage,
    run_id: str,
    target: str | int | None,
) -> int:
    """Anchor sequence for the requested restore target."""
    if target is None:
        latest = storage.latest_checkpoint(run_id)
        if latest is None:
            return 0
        return latest.state.source_sequence
    if isinstance(target, int):
        version = target
        for cp in storage.list_checkpoints(run_id):
            if cp.version == version or cp.state.source_sequence == version:
                return cp.state.source_sequence
        raise ValueError(
            f"no checkpoint with version/source_sequence {version!r} for run {run_id!r}"
        )
    text = str(target).strip()
    if not text:
        raise ValueError("restore target must be non-empty")
    try:
        cp = storage.get_checkpoint(text)
        if cp.run_id == run_id:
            return cp.state.source_sequence
    except Exception:
        pass
    try:
        version = int(text)
        for cp in storage.list_checkpoints(run_id):
            if cp.version == version or cp.state.source_sequence == version:
                return cp.state.source_sequence
        for cp in storage.list_checkpoints(run_id):
            if cp.checkpoint_id == text:
                return cp.state.source_sequence
    except ValueError:
        pass
    raise ValueError(f"no checkpoint {text!r} for run {run_id!r}")


def restore_to_anchor(
    storage: Storage,
    run_id: str,
    anchor: int,
    *,
    reason: str,
    carry_forward: Collection[str] | None = None,
) -> tuple[Any, set[str], dict[str, Any]]:
    """Check preconditions for restoring ``run_id`` to ``anchor``."""
    return check_preconditions(
        storage,
        run_id,
        anchor,
        edit_type="restore",
        carry_forward=carry_forward,
    )


def approve_restore(
    storage: Storage,
    run_id: str,
    *,
    reason: str,
    target: str | int | None = None,
    anchor_sequence: int | None = None,
    carry_forward: Collection[str] | None = None,
) -> Run:
    """Approve a restore of ``run_id`` to ``target`` or ``anchor_sequence``."""
    run = storage.get_run(run_id)
    if not reason or not reason.strip():
        raise ValueError("a restore needs a stated reason; the reason is the audit")
    if target is not None and anchor_sequence is not None:
        raise ValueError("pass exactly one of target or anchor_sequence")

    if anchor_sequence is not None:
        anchor = int(anchor_sequence)
    else:
        anchor = _anchor_for(storage, run_id, target)

    derivation, carry_set, summary = check_preconditions(
        storage,
        run_id,
        anchor,
        edit_type="restore",
        carry_forward=carry_forward,
    )

    payload: dict[str, Any] = {
        "reason": reason.strip(),
        "anchor_sequence": anchor,
        "divergence_sequence": anchor,
        "candidate_sequence": storage.last_sequence(run_id),
        "target": str(target) if target is not None else None,
        "preconditions": summary,
        "precondition_summary": summary,
        "derivation": summary,
        "derivation_summary": summary,
        "carry_forward": sorted(carry_set),
        "edit_type": "restore",
    }
    storage.append_event(
        run_id,
        EventType.RUN_RESTORED,
        payload,
        source=Origin.HUMAN,
    )
    return run
