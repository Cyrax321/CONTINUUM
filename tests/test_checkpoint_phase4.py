from __future__ import annotations

from collections.abc import Iterator

import pytest

from continuum.checkpoint import CheckpointManager, CheckpointTrigger
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="Recover safely"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "Recover safely", "total": 10})
    yield storage
    storage.close()


def advance(store: SQLiteStorage, count: int = 1) -> None:
    for _ in range(count):
        store.append_event("run_1", EventType.WORK_COMPLETED, {})


def test_recovery_trigger_is_available() -> None:
    assert CheckpointTrigger.RECOVERY == "recovery"


def test_checkpoint_on_recovery_creates_an_anchor(store: SQLiteStorage) -> None:
    advance(store, 3)
    cp = CheckpointManager(store).checkpoint_on_recovery("run_1", reason="dataset went stale")
    assert cp.trigger == CheckpointTrigger.RECOVERY
    assert cp.reason == "dataset went stale"


def test_last_recovery_anchor_finds_the_newest_anchor(store: SQLiteStorage) -> None:
    advance(store, 2)
    mgr = CheckpointManager(store)
    mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)
    anchor = mgr.checkpoint_on_recovery("run_1", reason="first failure")

    found = mgr.last_recovery_anchor("run_1")
    assert found is not None
    assert found.checkpoint_id == anchor.checkpoint_id


def test_last_recovery_anchor_respects_before_version(store: SQLiteStorage) -> None:
    advance(store, 2)
    mgr = CheckpointManager(store)
    mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)  # v0
    anchor = mgr.checkpoint_on_recovery("run_1")  # v1 anchor
    mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)  # v2

    assert mgr.last_recovery_anchor("run_1", before_version=anchor.version - 1) is None
    assert mgr.last_recovery_anchor("run_1", before_version=anchor.version) is not None


def test_prune_keeps_recent_checkpoints_and_recovery_anchors(store: SQLiteStorage) -> None:
    advance(store, 1)
    mgr = CheckpointManager(store)
    ids: list[str] = []
    # 7 checkpoints; index 1 is an old recovery anchor, the rest are plain.
    for i in range(7):
        if i == 1:
            cp = mgr.checkpoint_on_recovery("run_1", reason=f"anchor-{i}")
        else:
            cp = mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL, reason=f"plain-{i}")
        ids.append(cp.checkpoint_id)

    deleted = mgr.prune("run_1", keep=3, keep_anchors=True)
    # newest 3 (idx 4,5,6) always retained; of the old 4 (idx 0..3) the anchor
    # at idx 1 is preserved, so idx 0,2,3 are removed.
    assert set(deleted) == {ids[0], ids[2], ids[3]}

    remaining = {c.checkpoint_id for c in mgr.history("run_1")}
    assert ids[1] in remaining  # anchor kept
    assert ids[6] in remaining  # newest kept
    assert ids[0] not in remaining


def test_prune_can_drop_anchors_when_asked(store: SQLiteStorage) -> None:
    advance(store, 1)
    mgr = CheckpointManager(store)
    ids: list[str] = []
    for i in range(5):
        cp = (
            mgr.checkpoint_on_recovery("run_1")
            if i == 0
            else mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)
        )
        ids.append(cp.checkpoint_id)

    deleted = mgr.prune("run_1", keep=2, keep_anchors=False)
    # newest 2 (idx 3,4) kept; oldest 3 (idx 0,1,2) removed including the anchor.
    assert set(deleted) == {ids[0], ids[1], ids[2]}


def test_prune_is_a_noop_when_within_keep(store: SQLiteStorage) -> None:
    advance(store, 1)
    mgr = CheckpointManager(store)
    for _ in range(3):
        mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)

    assert mgr.prune("run_1", keep=5) == []


def test_compaction_trigger_is_available() -> None:
    assert CheckpointTrigger.COMPACTION == "compaction"


def test_compaction_anchor_does_not_shadow_the_recovery_anchor(
    store: SQLiteStorage,
) -> None:
    """A compaction seal must not win last_recovery_anchor (#1852).

    compact_run writes its anchor after the unsafe event at a higher version;
    stamped RECOVERY it was the newest RECOVERY checkpoint, so
    --to-recovery-anchor rolled back to the compaction point and skipped the
    real rollback point entirely.
    """
    advance(store, 2)
    mgr = CheckpointManager(store)
    rollback = mgr.checkpoint_on_recovery("run_1", reason="dataset went stale")
    advance(store, 2)
    store.compact_run("run_1")

    seals = [c for c in mgr.history("run_1") if c.trigger == CheckpointTrigger.COMPACTION]
    assert seals and all(c.version > rollback.version for c in seals)

    found = mgr.last_recovery_anchor("run_1")
    assert found is not None
    assert found.checkpoint_id == rollback.checkpoint_id


def test_last_recovery_anchor_is_none_when_only_compaction_anchors_exist(
    store: SQLiteStorage,
) -> None:
    """A run never given a recovery decision has no rollback point (#1852).

    Reporting the compaction seal here would invite callers to "roll back" to
    the state the unsafe event already happened in.
    """
    advance(store, 2)
    store.compact_run("run_1")

    assert CheckpointManager(store).last_recovery_anchor("run_1") is None


def test_prune_keeps_a_compaction_anchor(store: SQLiteStorage) -> None:
    """The compaction seal is the state a compacted live tail starts from.

    Deleting it orphans the run, so prune's keep_anchors rule covers it too,
    not only recovery anchors.
    """
    advance(store, 1)
    store.compact_run("run_1")
    mgr = CheckpointManager(store)
    seal = next(c for c in mgr.history("run_1") if c.trigger == CheckpointTrigger.COMPACTION)
    for _ in range(6):
        mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL)

    deleted = mgr.prune("run_1", keep=3, keep_anchors=True)

    assert seal.checkpoint_id not in deleted
    assert seal.checkpoint_id in {c.checkpoint_id for c in mgr.history("run_1")}
