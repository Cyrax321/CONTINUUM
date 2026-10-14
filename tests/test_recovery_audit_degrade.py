from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from continuum.checkpoint.manager import CheckpointManager
from continuum.events import EventType
from continuum.models import RecoveryMode, RecoverySafety, Run, StateStatus
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.base import CorruptedRecord
from continuum.storage.sqlite import SQLiteStorage


@pytest.fixture
def poisoned_path(tmp_path: Path) -> str:
    path = str(tmp_path / "run.db")
    store = SQLiteStorage(path)
    store.create_run(Run(run_id="r1", goal="audit"))
    store.append_event("r1", EventType.RUN_STARTED, {"goal": "audit"})
    store.append_event(
        "r1",
        EventType.ACTION_RECORDED,
        {
            "key": "k1",
            "action_id": "a1",
            "action_type": "t1",
            "arguments": {},
            "status": "started",
        },
    )
    store.close()

    conn = sqlite3.connect(path)
    conn.execute('UPDATE events SET payload = \'{"key": "k1", "brok\' WHERE sequence = 2')
    conn.commit()
    conn.close()
    return path


def test_assess_degrades_to_a_verdict_on_a_poisoned_log(poisoned_path: str) -> None:
    """assess must answer, not raise: a poisoned log is the run that needs one.

    This is the issue #383 contract the engine asserts at engine.py:348-352.
    """
    engine = RecoveryEngine(SQLiteStorage(poisoned_path))
    decision = engine.assess("r1")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any("stops folding at sequence 2" in r for r in decision.rationale)


def test_the_poisoned_prefix_is_reported_not_silently_trusted(poisoned_path: str) -> None:
    """Whatever the degraded verdict says, it must not claim the log is intact."""
    engine = RecoveryEngine(SQLiteStorage(poisoned_path))
    decision = engine.assess("r1")
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_HUMAN
    assert decision.mode is RecoveryMode.REQUEST_HUMAN


def test_restore_raise_still_fails_loudly_on_a_poisoned_log(poisoned_path: str) -> None:
    """Under on_unprojectable='raise', restore propagates CorruptedRecord."""
    manager = CheckpointManager(SQLiteStorage(poisoned_path))
    with pytest.raises(CorruptedRecord):
        manager.restore("r1", on_unprojectable="raise")


def test_restore_degrade_returns_readable_prefix(poisoned_path: str) -> None:
    """Under on_unprojectable='degrade', restore marks state INVALID at unreadable sequence."""
    manager = CheckpointManager(SQLiteStorage(poisoned_path))
    restored = manager.restore("r1", on_unprojectable="degrade")
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 2


def test_assess_with_checkpoint_and_poisoned_tail(tmp_path: Path) -> None:
    """A run with a checkpoint followed by a corrupted event in tail degrades properly."""
    path = str(tmp_path / "checkpoint_tail.db")
    store = SQLiteStorage(path)
    store.create_run(Run(run_id="r1", goal="audit"))
    store.append_event("r1", EventType.RUN_STARTED, {"goal": "audit"})
    manager = CheckpointManager(store)
    state = manager.project_current("r1")
    manager.checkpoint("r1", state=state, force_version=True)
    store.append_event(
        "r1",
        EventType.ACTION_RECORDED,
        {
            "key": "k2",
            "action_id": "a2",
            "action_type": "t2",
            "arguments": {},
            "status": "started",
        },
    )
    store.close()

    conn = sqlite3.connect(path)
    conn.execute('UPDATE events SET payload = \'{"bad": "json\' WHERE sequence = 3')
    conn.commit()
    conn.close()

    engine = RecoveryEngine(SQLiteStorage(path))
    decision = engine.assess("r1")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_HUMAN
