"""Cleanup of ephemeral recovery artifacts.

Snapshots and intermediate checkpoints accumulate as an agent runs. Only the
checkpoints referenced by a sealed recovery contract and the explicit recovery
anchors need to survive. Everything else is ephemeral and can be removed to
keep storage bounded.

This module provides a single routine that deletes unreferenced checkpoints
while preserving any checkpoint that is either referenced by the ledger or
marked as a recovery anchor.
"""

from __future__ import annotations

from continuum.checkpoint.policy import CheckpointTrigger
from continuum.recovery.ledger import RecoveryLedger
from continuum.storage.base import Storage

__all__ = [
    "cleanup_ephemeral_artifacts",
]


def cleanup_ephemeral_artifacts(
    storage: Storage,
    ledger: RecoveryLedger,
    run_id: str,
    *,
    keep_anchors: bool = True,
) -> list[str]:
    """Remove checkpoints not referenced by a sealed contract.

    A checkpoint is kept when it is a recovery anchor (trigger == RECOVERY and
    keep_anchors is True) or its version appears in any ledger decision's
    contract. All other checkpoints are deleted.

    Returns the ids of the checkpoints that were removed. Referenced anchors
    always survive, which the acceptance test for issue 167 asserts.
    """
    checkpoints = list(storage.list_checkpoints(run_id))
    if not checkpoints:
        return []

    referenced_versions = {
        entry.contract.checkpoint_version
        for entry in ledger.entries(run_id)
        if entry.contract is not None
    }

    deleted: list[str] = []
    for checkpoint in checkpoints:
        is_referenced = checkpoint.version in referenced_versions
        is_anchor = checkpoint.trigger == CheckpointTrigger.RECOVERY
        if is_referenced:
            continue
        if keep_anchors and is_anchor:
            continue
        storage.delete_checkpoint(checkpoint.checkpoint_id)
        deleted.append(checkpoint.checkpoint_id)

    return deleted
