from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from continuum.checkpoint import (
    CheckpointError,
    CheckpointManager,
    CheckpointTrigger,
    IntervalPolicy,
    ManualPolicy,
    SemanticPolicy,
    clear_resume_pointer,
)
from continuum.events import EventType
from continuum.models import Goal, Run, SemanticState, StateStatus, utcnow
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage
from continuum.storage.base import CorruptedRecord


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


def test_the_default_policy_does_not_re_checkpoint_unchanged_state_after_a_restart(
    tmp_path: Path,
) -> None:
    """The same restart invariant, for the default HybridPolicy.

    IntervalPolicy is served by the _last_checkpoint_at back-fill, but
    SemanticPolicy and EventPolicy read _last_state, which was never seeded the
    same way. So a fresh manager saw previous_state=None even with a stored
    checkpoint: SemanticPolicy fired "first state for this run" and EventPolicy
    rescanned the whole history from cursor 0, writing a redundant checkpoint of
    unchanged state on the first call after every restart (issue #1347).
    """
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 10})
        CheckpointManager(store).checkpoint("run_1")  # default policy
        assert len(store.list_checkpoints("run_1")) == 1

    with SQLiteStorage(db) as store:  # restart: fresh manager, empty memory
        restarted = CheckpointManager(store)  # default policy
        assert restarted.maybe_checkpoint("run_1") is None
        assert len(store.list_checkpoints("run_1")) == 1

        # A genuine change after the restart still checkpoints.
        store.append_event(
            "run_1",
            EventType.ACTION_RECORDED,
            {
                "action": {
                    "action_id": "a1",
                    "action_type": "send_invoice",
                    "status": "completed",
                    "arguments": {},
                }
            },
        )
        assert restarted.maybe_checkpoint("run_1") is not None


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


# --- resume pointer clearing ----------------------------------------------- #


def test_clear_resume_pointer_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resume_path = tmp_path / "resume.json"
    monkeypatch.setattr("continuum.checkpoint.manager.RESUME_JSON", str(resume_path))
    assert clear_resume_pointer("run_1") is False


@pytest.mark.parametrize(
    "content",
    [
        "not-valid-json",
        "[1, 2, 3]",
        '"a bare string"',
        "123",
        "null",
    ],
)
def test_clear_resume_pointer_unreadable_or_nondict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("continuum.checkpoint.manager.RESUME_JSON", str(resume_path))
    assert clear_resume_pointer("run_1") is False
    assert resume_path.exists()


def test_clear_resume_pointer_foreign_run_id_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(json.dumps({"run_id": "run_other"}), encoding="utf-8")
    monkeypatch.setattr("continuum.checkpoint.manager.RESUME_JSON", str(resume_path))
    assert clear_resume_pointer("run_1") is False
    assert resume_path.exists()


def test_clear_resume_pointer_unlink_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(json.dumps({"run_id": "run_1"}), encoding="utf-8")
    monkeypatch.setattr("continuum.checkpoint.manager.RESUME_JSON", str(resume_path))

    def mock_unlink(self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", mock_unlink)
    assert clear_resume_pointer("run_1") is False
    assert resume_path.exists()


def test_clear_resume_pointer_matching_run_id_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(json.dumps({"run_id": "run_1"}), encoding="utf-8")
    monkeypatch.setattr("continuum.checkpoint.manager.RESUME_JSON", str(resume_path))
    assert clear_resume_pointer("run_1") is True
    assert not resume_path.exists()


# --- corruption degradation and clean prefix -------------------------------- #


def test_mark_corruption_refusal_already_invalid() -> None:
    from continuum.checkpoint.manager import _mark_corruption_refusal

    existing = SemanticState(
        run_id="run_1",
        goal=Goal(description="test"),
        status=StateStatus.INVALID,
        unprojectable_at_sequence=3,
        unprojectable_event_type="action_recorded",
        unprojectable_reason="earlier break",
    )
    refusal = CorruptedRecord("corrupted event record", sequence=10)
    res = _mark_corruption_refusal(existing, refusal)
    assert res is existing
    assert res.unprojectable_at_sequence == 3
    assert res.unprojectable_reason == "earlier break"


def test_mark_corruption_refusal_multiline_reason() -> None:
    from continuum.checkpoint.manager import _mark_corruption_refusal

    state = SemanticState(run_id="run_1", goal=Goal(description="test"))
    refusal = CorruptedRecord("line 1 error\nline 2 trace\nline 3 context", sequence=7)
    res = _mark_corruption_refusal(state, refusal)
    assert res.status is StateStatus.INVALID
    assert res.unprojectable_at_sequence == 7
    assert res.unprojectable_event_type == "unreadable"
    assert res.unprojectable_reason == "corrupted event record: line 1 error"


def test_clean_prefix_raises_when_sequence_is_none(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    refusal = CorruptedRecord("unsequenced corruption", sequence=None)
    with pytest.raises(CorruptedRecord, match="unsequenced corruption"):
        manager._clean_prefix("run_1", after_sequence=0, refusal=refusal)


def test_restore_without_checkpoint_corrupted_record_degrade_and_raise(
    store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    advance(store, 2)
    original_read = store.read_events

    def mock_read(
        run_id: str, after_sequence: int = 0, upto: int | None = None, **kwargs: Any
    ) -> list[Any]:
        if upto is None:
            raise CorruptedRecord("corrupted record at 3\nextra info", sequence=3)
        return original_read(run_id, after_sequence=after_sequence, upto=upto, **kwargs)

    monkeypatch.setattr(store, "read_events", mock_read)
    manager = CheckpointManager(store)

    with pytest.raises(CorruptedRecord):
        manager.restore("run_1", on_unprojectable="raise")

    restored = manager.restore("run_1", on_unprojectable="degrade")
    assert restored.replayed is True
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 3
    assert restored.state.unprojectable_reason == "corrupted event record: corrupted record at 3"


def test_restore_without_checkpoint_corrupted_record_empty_prefix_raises(
    store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mock_read(
        run_id: str, after_sequence: int = 0, upto: int | None = None, **kwargs: Any
    ) -> list[Any]:
        if upto is None:
            raise CorruptedRecord("corrupted record at 1", sequence=1)
        return []

    monkeypatch.setattr(store, "read_events", mock_read)
    manager = CheckpointManager(store)
    with pytest.raises(CorruptedRecord, match="corrupted record at 1"):
        manager.restore("run_1", on_unprojectable="degrade")


def test_restore_with_checkpoint_corrupted_record_degrade_and_raise(
    store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = CheckpointManager(store)
    advance(store, 2)
    manager.checkpoint("run_1")
    advance(store, 3)

    original_read = store.read_events

    # Case 1: corruption at sequence 5 (immediately after checkpoint cursor 4, so no readable pending work)
    def mock_read_no_pending(
        run_id: str, after_sequence: int = 0, upto: int | None = None, **kwargs: Any
    ) -> list[Any]:
        if after_sequence > 0 and upto is None:
            raise CorruptedRecord("bad pending record", sequence=5)
        return original_read(run_id, after_sequence=after_sequence, upto=upto, **kwargs)

    monkeypatch.setattr(store, "read_events", mock_read_no_pending)

    with pytest.raises(CorruptedRecord):
        manager.restore("run_1", on_unprojectable="raise")

    # on_unprojectable="degrade" with no readable pending events marks refusal on checkpoint.state
    restored_no_pending = manager.restore("run_1", replay=True, on_unprojectable="degrade")
    assert restored_no_pending.replayed is False
    assert restored_no_pending.pending_events == 0
    assert restored_no_pending.state.status is StateStatus.INVALID
    assert restored_no_pending.state.unprojectable_at_sequence == 5

    # Case 2: corruption at sequence 6 (sequence 5 is readable, so incremental replay occurs)
    def mock_read_with_pending(
        run_id: str, after_sequence: int = 0, upto: int | None = None, **kwargs: Any
    ) -> list[Any]:
        if after_sequence > 0 and upto is None:
            raise CorruptedRecord("bad pending record at 6", sequence=6)
        return original_read(run_id, after_sequence=after_sequence, upto=upto, **kwargs)

    monkeypatch.setattr(store, "read_events", mock_read_with_pending)

    # replay=False stops at checkpoint state without incremental projection
    restored_no_replay = manager.restore("run_1", replay=False, on_unprojectable="degrade")
    assert restored_no_replay.replayed is False
    assert restored_no_replay.state.status is StateStatus.INVALID
    assert restored_no_replay.state.unprojectable_at_sequence == 6

    # replay=True replays sequence 5 incrementally then marks refusal
    restored_replayed = manager.restore("run_1", replay=True, on_unprojectable="degrade")
    assert restored_replayed.replayed is True
    assert restored_replayed.pending_events == 1
    assert restored_replayed.state.status is StateStatus.INVALID
    assert restored_replayed.state.unprojectable_at_sequence == 6


# --- pruning normalization -------------------------------------------------- #


def test_prune_clamps_keep_to_minimum_one(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    advance(store, 1)
    cp1 = manager.checkpoint("run_1")
    advance(store, 1)
    cp2 = manager.checkpoint("run_1")
    advance(store, 1)
    cp3 = manager.checkpoint("run_1")

    deleted = manager.prune("run_1", keep=0)
    assert deleted == [cp1.checkpoint_id, cp2.checkpoint_id]
    remaining = manager.history("run_1")
    assert len(remaining) == 1
    assert remaining[0].checkpoint_id == cp3.checkpoint_id


def test_prune_noop_when_history_smaller_than_keep(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    advance(store, 1)
    manager.checkpoint("run_1")
    assert manager.prune("run_1", keep=5) == []


def test_prune_preserves_anchor_triggers(store: SQLiteStorage) -> None:
    manager = CheckpointManager(store)
    advance(store, 1)
    cp_milestone = manager.checkpoint("run_1", trigger=CheckpointTrigger.MILESTONE)
    advance(store, 1)
    cp_recovery = manager.checkpoint("run_1", trigger=CheckpointTrigger.RECOVERY)
    advance(store, 1)
    cp_compaction = manager.checkpoint("run_1", trigger=CheckpointTrigger.COMPACTION)
    advance(store, 1)
    cp_latest = manager.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)

    deleted = manager.prune("run_1", keep=1, keep_anchors=True)
    assert deleted == [cp_milestone.checkpoint_id]
    remaining_ids = {c.checkpoint_id for c in manager.history("run_1")}
    assert cp_recovery.checkpoint_id in remaining_ids
    assert cp_compaction.checkpoint_id in remaining_ids
    assert cp_latest.checkpoint_id in remaining_ids
    assert cp_milestone.checkpoint_id not in remaining_ids

    deleted_more = manager.prune("run_1", keep=1, keep_anchors=False)
    assert set(deleted_more) == {cp_recovery.checkpoint_id, cp_compaction.checkpoint_id}
    assert [c.checkpoint_id for c in manager.history("run_1")] == [cp_latest.checkpoint_id]
