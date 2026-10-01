"""Shared bound resolution for log compaction (issues #239, #705, #1078).

Compaction archives a run's pre-anchor prefix in one transaction: append the
``EVENT_LOG_ANCHORED`` marker, copy the prefix into ``events_archive``, delete
it from ``events``. The boundary that makes that safe is computed *before* the
transaction, and the two storage engines used to compute it independently.
Postgres kept every check SQLite had except the anchor guard from #705, so an
explicit ``through_sequence`` could archive and delete the anchor marker
itself, and the live log forked away from the archive on the next append
(issue #1078).

Both engines now resolve the bound here, so a check added to one is a check
added to both. The transaction body stays engine-specific (parameter style,
connection handling); everything that decides *what* is safe to archive is
shared.
"""

from __future__ import annotations

from continuum.models import SemanticState
from continuum.storage.base import Storage

__all__ = ["resolve_compaction_bound"]


def resolve_compaction_bound(
    storage: Storage, run_id: str, through_sequence: int | None
) -> tuple[SemanticState, int]:
    """Anchor the run if needed, then return ``(version, through)``.

    ``through`` is the last sequence to archive and is always strictly below
    the anchor marker's own sequence, so the live log keeps its anchor. Raises
    ``ValueError`` when the run cannot be anchored, when an explicit
    ``through_sequence`` would reach the anchor, or when there is nothing to
    archive.
    """
    # Local import: checkpoint.manager imports storage, so a module-level
    # import here would cycle.
    from continuum.checkpoint.manager import CheckpointManager

    lv = storage.latest_version(run_id)
    head = storage.last_sequence(run_id)
    needs_fresh_anchor = lv is None or through_sequence is not None or lv.source_sequence < head
    if needs_fresh_anchor:
        try:
            CheckpointManager(storage).checkpoint(run_id, force_version=True)
        except Exception as exc:
            raise ValueError(f"run {run_id!r} could not be anchored: {exc}") from exc
        lv = storage.latest_version(run_id)
    storage_version = lv
    if storage_version is None:
        raise ValueError(f"run {run_id!r} could not be anchored: no projectable state")

    # The anchor marker is appended at the head of the log in the caller's
    # transaction, so its sequence is the current head + 1. An explicit
    # through_sequence at or above it would archive the marker and every live
    # row after it; the next append would then mint a fresh genesis with
    # prev_hash = None and the live chain forks away from the archive (#705).
    anchor_sequence = storage.last_sequence(run_id) + 1
    if through_sequence is not None and through_sequence >= anchor_sequence:
        raise ValueError(
            f"through_sequence {through_sequence} would archive the anchor marker"
            f" at sequence {anchor_sequence}: the live log must retain its anchor"
        )
    through = (
        through_sequence
        if through_sequence is not None
        else min(storage_version.source_sequence, storage.last_sequence(run_id))
    )
    if through < 1:
        raise ValueError("nothing to compact: anchor would be empty")
    return storage_version, through
