from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from continuum.checkpoint import (
    CheckpointError,
    CheckpointManager,
    CheckpointTrigger,
    IntervalPolicy,
    ManualPolicy,
    SemanticPolicy,
)
from continuum.events import EventType
from continuum.models import Goal, Run, SemanticState, utcnow
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
    storage.append_event(
        "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
    )
    yield storage
    storage.close()


def advance(store: SQLiteStorage, count: int = 1) -> None:
    for _ in range(count):
        store.append_event("run_1", EventType.WORK_COMPLETED, {})


# --- creating -------------------------------------------------------------- #


def test_a_checkpoint_is_sealed_versioned_and_persisted(store: SQLiteStorage) -> None:
    advance(store, 5)
    checkpoint = CheckpointManager(store).checkpoint("run_1", trigger="milestone")

    assert checkpoint.verify()
    assert checkpoint.version == 0
    assert checkpoint.state.progress.completed == 5
    assert store.get_checkpoint(checkpoint.checkpoint_id) == checkpoint
    assert store.list_versions("run_1") == [0]


def test_checkpointing_records_an_event(store: SQLiteStorage) -> None:
    advance(store, 2)
    checkpoint = CheckpointManager(store).checkpoint("run_1")

    recorded = [e for e in store.read_events("run_1") if e.type is EventType.STATE_CHECKPOINTED]
    assert len(recorded) == 1
    assert recorded[0].payload["checkpoint_id"] == checkpoint.checkpoint_id
    assert recorded[0].payload["integrity_hash"] == checkpoint.integrity_hash


def test_the_checkpoint_records_the_event_cursor_it_covers(store: SQLiteStorage) -> None:
    advance(store, 4)
    checkpoint = CheckpointManager(store).checkpoint("run_1")
    assert checkpoint.state.source_sequence == 5  # RUN_STARTED + 4


def test_successive_checkpoints_increment_the_version(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    advance(store, 1)
    first = manager.checkpoint("run_1")
    advance(store, 1)
    second = manager.checkpoint("run_1")

    assert (first.version, second.version) == (0, 1)
    assert [c.version for c in manager.history("run_1")] == [0, 1]


def test_checkpointing_a_foreign_state_is_refused(store: SQLiteStorage) -> None:
    other = SemanticState(run_id="run_other", goal=Goal(description="g"))
    with pytest.raises(CheckpointError, match="belongs to run"):
        CheckpointManager(store).checkpoint("run_1", state=other)


# --- policy integration ---------------------------------------------------- #


def test_maybe_checkpoint_respects_a_declining_policy(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store, policy=ManualPolicy())
    advance(store, 3)
    assert manager.maybe_checkpoint("run_1") is None
    assert manager.history("run_1") == []


def test_maybe_checkpoint_writes_when_the_policy_agrees(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store, policy=ManualPolicy())
    advance(store, 3)
    checkpoint = manager.maybe_checkpoint("run_1", explicit=True)
    assert checkpoint is not None
    assert checkpoint.trigger == CheckpointTrigger.MANUAL


def test_the_recorded_trigger_explains_why(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store, policy=SemanticPolicy(progress_stride=5))
    advance(store, 5)
    checkpoint = manager.maybe_checkpoint("run_1")
    assert checkpoint is not None
    assert checkpoint.trigger in (
        CheckpointTrigger.MILESTONE,
        CheckpointTrigger.IMPORTANT_STATE_CHANGE,
    )


def test_a_semantic_policy_declines_pure_volume(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store, policy=SemanticPolicy(progress_stride=1000))
    advance(store, 1)
    assert manager.maybe_checkpoint("run_1") is not None  # first state
    advance(store, 1)
    assert manager.maybe_checkpoint("run_1") is None  # nothing meaningful changed


def test_an_interval_policy_sees_an_existing_checkpoint_after_a_restart(
    tmp_path: Path,
) -> None:
    """A fresh manager must not re-checkpoint just because its memory is empty."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 10})
        CheckpointManager(store).checkpoint("run_1")

    with SQLiteStorage(db) as store:
        restarted = CheckpointManager(store, policy=IntervalPolicy(3600))
        assert restarted.maybe_checkpoint("run_1") is None

        future = utcnow() + timedelta(hours=2)
        assert restarted.maybe_checkpoint("run_1", now=future) is not None


# --- restoring ------------------------------------------------------------- #


def test_restore_returns_the_checkpoint_when_nothing_happened_since(
    store: SQLiteStorage,
) -> None:
    manager = CheckpointManager(store)
    advance(store, 3)
    checkpoint = manager.checkpoint("run_1")

    restored = manager.restore("run_1")
    assert restored.from_checkpoint
    assert restored.checkpoint == checkpoint
    assert restored.pending_events == 0
    assert restored.state.progress.completed == 3


def test_restore_replays_work_done_after_the_checkpoint(store: SQLiteStorage) -> None:
    """A crash between checkpoints must not discard the work in between."""
    manager = CheckpointManager(store)
    advance(store, 3)
    manager.checkpoint("run_1")
    advance(store, 4)

    restored = manager.restore("run_1")
    assert restored.replayed
    assert restored.pending_events == 4
    assert restored.state.progress.completed == 7
    assert restored.state == project("run_1", store.read_events("run_1"))


def test_restore_can_refuse_to_replay(store: SQLiteStorage) -> None:
    """A validator needs the checkpoint on its own terms before trusting newer events."""
    manager = CheckpointManager(store)
    advance(store, 3)
    manager.checkpoint("run_1")
    advance(store, 4)

    restored = manager.restore("run_1", replay=False)
    assert not restored.replayed
    assert restored.pending_events == 4
    assert restored.state.progress.completed == 3


def test_restore_falls_back_to_the_event_log_without_a_checkpoint(
    store: SQLiteStorage,
) -> None:
    advance(store, 6)
    restored = CheckpointManager(store).restore("run_1")

    assert not restored.from_checkpoint
    assert restored.replayed
    assert restored.state.progress.completed == 6


def test_restoring_an_empty_run_fails_clearly(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="run_empty", goal="g"))
    with pytest.raises(CheckpointError, match="no checkpoint and no events"):
        CheckpointManager(store).restore("run_empty")


def test_the_newest_checkpoint_wins(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    advance(store, 2)
    manager.checkpoint("run_1")
    advance(store, 2)
    latest = manager.checkpoint("run_1")

    assert manager.restore("run_1").checkpoint == latest


# --- durability ------------------------------------------------------------ #


def test_a_checkpoint_survives_a_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"

    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 100})
        for _ in range(40):
            store.append_event("run_1", EventType.WORK_COMPLETED, {})
        CheckpointManager(store).checkpoint("run_1", trigger="milestone")
        for _ in range(5):
            store.append_event("run_1", EventType.WORK_COMPLETED, {})

    with SQLiteStorage(db) as store:
        restored = CheckpointManager(store).restore("run_1")
        assert restored.from_checkpoint
        assert restored.checkpoint is not None
        assert restored.checkpoint.verify()
        assert restored.pending_events == 5
        assert restored.state.progress.completed == 45
        assert store.verify_events("run_1").ok


def test_a_checkpoints_own_annotation_is_not_treated_as_pending_work(
    store: SQLiteStorage,
) -> None:
    """The STATE_CHECKPOINTED event is written after projection, so it sits one
    past the cursor. Counting it would make every fresh checkpoint look stale
    and replay a no-op on every restore."""
    manager = CheckpointManager(store)
    advance(store, 3)
    checkpoint = manager.checkpoint("run_1")

    tail = store.read_events("run_1", after_sequence=checkpoint.state.source_sequence)
    assert [e.type for e in tail] == [EventType.STATE_CHECKPOINTED]

    for _ in range(3):
        assert manager.restore("run_1").pending_events == 0


def test_repeated_checkpoints_do_not_accumulate_phantom_pending_events(
    store: SQLiteStorage,
) -> None:
    manager = CheckpointManager(store)
    for _ in range(4):
        advance(store, 2)
        manager.checkpoint("run_1")
        assert manager.restore("run_1").pending_events == 0


def test_a_crash_between_checkpoint_and_annotation_still_restores(
    tmp_path: Path,
) -> None:
    """The documented interleaving: checkpoint committed, annotation never written.

    The checkpoint is valid and must still be usable; the cursor simply falls
    back to the projected sequence.
    """
    import sqlite3

    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 10})
        for _ in range(3):
            store.append_event("run_1", EventType.WORK_COMPLETED, {})
        CheckpointManager(store).checkpoint("run_1")

    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM events WHERE type = 'STATE_CHECKPOINTED'")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store:
        restored = CheckpointManager(store).restore("run_1")
        assert restored.from_checkpoint
        assert restored.pending_events == 0
        assert restored.state.progress.completed == 3


def test_evaluate_does_not_write_anything(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    advance(store, 3)
    before = store.last_sequence("run_1")

    assert manager.evaluate("run_1", explicit=True).should
    assert store.last_sequence("run_1") == before
    assert manager.history("run_1") == []


def test_checkpoint_verify_detects_tampered_hash(store: SQLiteStorage) -> None:
    cp = CheckpointManager(store).checkpoint("run_1", trigger="manual", reason="")
    assert cp.verify()
    bad = cp.model_copy(update={"integrity_hash": "bad"})
    assert not bad.verify()
    # Tampering with None also fails (covers the unset case)
    tampered_none = cp.model_copy(update={"integrity_hash": None})
    assert not tampered_none.verify()
