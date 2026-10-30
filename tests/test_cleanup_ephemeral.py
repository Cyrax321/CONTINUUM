from continuum.checkpoint import CheckpointManager
from continuum.checkpoint.policy import CheckpointTrigger
from continuum.events import EventType
from continuum.models import RecoveryContract, RecoverySafety, Run
from continuum.recovery import MemoryLedgerBackend, RecoveryLedger, cleanup_ephemeral_artifacts
from continuum.replayguard import protected_call
from continuum.storage import SQLiteStorage


def test_referenced_anchors_survive_cleanup() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    mgr = CheckpointManager(storage)

    a = mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL, reason="ephemeral 1")
    storage.append_event(
        "run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "summary": "s1", "source": "d"}
    )
    b = mgr.checkpoint("run_1", trigger=CheckpointTrigger.RECOVERY, reason="anchor")
    storage.append_event(
        "run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev2", "summary": "s2", "source": "d"}
    )
    c = mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL, reason="ephemeral 2")

    ledger = RecoveryLedger(MemoryLedgerBackend())
    contract = RecoveryContract(
        run_id="run_1",
        checkpoint_version=b.version,
        recovery_status=RecoverySafety.REQUIRES_REPAIR,
        reason="referenced",
    )
    ledger.append_decision("run_1", contract, anchor=True)

    deleted = cleanup_ephemeral_artifacts(storage, ledger, "run_1", keep_anchors=True)

    assert a.checkpoint_id in deleted
    assert c.checkpoint_id in deleted
    remaining = {cp.checkpoint_id for cp in storage.list_checkpoints("run_1")}
    assert b.checkpoint_id in remaining
    assert len(remaining) == 1


def test_unreferenced_non_anchor_is_removed() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    mgr = CheckpointManager(storage)
    only = mgr.checkpoint("run_1", trigger=CheckpointTrigger.MANUAL, reason="temp")
    ledger = RecoveryLedger(MemoryLedgerBackend())

    deleted = cleanup_ephemeral_artifacts(storage, ledger, "run_1")

    assert deleted == [only.checkpoint_id]
    assert list(storage.list_checkpoints("run_1")) == []


def test_compaction_anchor_survives_cleanup_ephemeral_artifacts() -> None:
    """Compaction anchor checkpoints must survive cleanup_ephemeral_artifacts (issue #1769).

    compact_run creates an anchor checkpoint before archiving the event prefix.
    When cleanup_ephemeral_artifacts(..., keep_anchors=True) runs, the anchor
    checkpoint must be preserved so that the compacted run remains restorable.
    """
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "test", "total": 10})
    for i in range(5):
        protected_call(storage, "run_1", action_type="doc", key=f"d:{i}", fn=lambda i=i: {"d": i})

    storage.compact_run("run_1")

    checkpoints = list(storage.list_checkpoints("run_1"))
    assert len(checkpoints) == 1
    anchor_cp = checkpoints[0]
    # The seal is COMPACTION, not RECOVERY: last_recovery_anchor must keep
    # returning the true rollback point, and this anchor is written after the
    # unsafe event at a higher version, so it would otherwise always shadow it
    # (#1852).
    assert anchor_cp.trigger == CheckpointTrigger.COMPACTION

    ledger = RecoveryLedger(MemoryLedgerBackend())
    deleted = cleanup_ephemeral_artifacts(storage, ledger, "run_1", keep_anchors=True)
    assert deleted == []
    remaining = list(storage.list_checkpoints("run_1"))
    assert len(remaining) == 1
    assert remaining[0].checkpoint_id == anchor_cp.checkpoint_id

    mgr = CheckpointManager(storage)
    restored = mgr.restore("run_1")
    assert restored.state.goal.description == "test"
