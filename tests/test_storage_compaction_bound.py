"""Contract tests for the shared event-log compaction boundary (#1653).

The resolved bound must leave room for the live anchor marker. Exercise the
resolver independently of either storage engine's compaction transaction so
regressions in its preflight checks are detected directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.models import Goal, Run, SemanticState
from continuum.storage import SQLiteStorage
from continuum.storage.compaction import resolve_compaction_bound

RUN_ID = "run_compaction_bound"


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[SQLiteStorage]:
    """A real storage engine with a projectable, started run."""
    with SQLiteStorage(str(tmp_path / "bounds.db")) as store:
        store.create_run_started(Run(run_id=RUN_ID, goal="preserve the anchor"))
        yield store


def test_resolves_default_bound_below_future_anchor(storage: SQLiteStorage) -> None:
    """The common path refreshes a stale checkpoint and retains room for the marker."""
    from continuum.checkpoint.manager import CheckpointManager
    
    CheckpointManager(storage).checkpoint(RUN_ID)
    earlier_head = storage.last_sequence(RUN_ID)

    version, through = resolve_compaction_bound(storage, RUN_ID, None)
    head_after_refresh = storage.last_sequence(RUN_ID)

    assert head_after_refresh == earlier_head + 1
    assert version == storage.latest_version(RUN_ID)
    assert through == min(version.source_sequence, head_after_refresh)
    assert 1 <= through < head_after_refresh + 1
    assert storage.read_archived_events(RUN_ID) == []


def test_resolves_explicit_prefix_below_future_anchor(storage: SQLiteStorage) -> None:
    """An explicit safe prefix remains respected even though a new checkpoint is minted."""
    from continuum.checkpoint.manager import CheckpointManager
    CheckpointManager(storage).checkpoint(RUN_ID)

    version, through = resolve_compaction_bound(storage, RUN_ID, 1)

    assert through == 1
    assert through <= version.source_sequence
    assert through < storage.last_sequence(RUN_ID) + 1


@pytest.mark.parametrize("offset", [0, 1, 100])
def test_explicit_bound_cannot_consume_anchor_marker(storage: SQLiteStorage, offset: int) -> None:
    """Regression for #705/#1078: deleting the >= guard must fail this test."""
    from continuum.checkpoint.manager import CheckpointManager
    CheckpointManager(storage).checkpoint(RUN_ID)
    # resolve_compaction_bound adds one forced checkpoint annotation before the
    # compaction transaction appends EVENT_LOG_ANCHORED.
    prospective_anchor = storage.last_sequence(RUN_ID) + 2
    unsafe_through = prospective_anchor + offset
    events_before = list(storage.read_events(RUN_ID))

    with pytest.raises(
        ValueError,
        match=(
            rf"through_sequence {unsafe_through} would archive the anchor marker"
            rf" at sequence {prospective_anchor}"
        ),
    ):
        resolve_compaction_bound(storage, RUN_ID, unsafe_through)

    # A rejected bound must preserve the entire prior log. The forced
    # checkpoint annotation is the only additional event.
    events_after = list(storage.read_events(RUN_ID))
    assert events_after[: len(events_before)] == events_before
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1].sequence == prospective_anchor - 1
    assert storage.read_archived_events(RUN_ID) == []


def test_projection_failure_is_reported_as_anchor_failure(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed projection must become the resolver's contextual ValueError."""
    from continuum.checkpoint.manager import CheckpointManager

    def broken_projection(self: object, run_id: str, *, full_history: bool = True) -> SemanticState:
        raise RuntimeError("unprojectable event")

    monkeypatch.setattr(CheckpointManager, "project_current", broken_projection)

    with pytest.raises(
        ValueError,
        match=f"run '{RUN_ID}' could not be anchored: unprojectable event",
    ) as exc_info:
        resolve_compaction_bound(storage, RUN_ID, None)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert storage.latest_version(RUN_ID) is None


    def checkpoint_without_persistence(self: object, run_id: str, **kwargs: object) -> None:
        return None    
    """Cover the defensive branch for a backend that reports no persisted version.

    SQLiteStorage normally persists a version during checkpoint(). Suppressing
    that one operation simulates a non-conforming backend while retaining real
    SQLite event, environment, and version queries throughout the resolver.
    """
    from continuum.checkpoint.manager import CheckpointManager

    def checkpoint_without_persistence(
        self: object, run_id: str, **kwargs: object
    ) -> None:
        return None

    monkeypatch.setattr(CheckpointManager, "checkpoint", checkpoint_without_persistence)

    with pytest.raises(ValueError, match="could not be anchored: no projectable state"):
        resolve_compaction_bound(storage, RUN_ID, None)


def test_empty_archive_bound_is_rejected(tmp_path: Path) -> None:
    """A persisted zero-event state cannot produce an archive prefix.

    Use a real empty-head run with a persisted version. A checkpoint annotation
    would create a new event, defeating the zero-bound condition under test.
    """
    with SQLiteStorage(str(tmp_path / "empty.db")) as store:
        store.create_run(Run(run_id=RUN_ID, goal="empty"))
        state = SemanticState(run_id=RUN_ID, goal=Goal(description="empty"))
        assert state.source_sequence == 0
        store.put_version(state)
        assert store.last_sequence(RUN_ID) == 0

        with pytest.raises(ValueError, match="nothing to compact: anchor would be empty"):
            resolve_compaction_bound(store, RUN_ID, None)
