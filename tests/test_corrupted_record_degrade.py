"""A corrupted event payload degrades to an advisory verdict, not a crash (#1907).

``RecoveryEngine.assess`` asks "where does this run stand", and a log whose
records refuse to load is precisely the run that needs an answer. Assessment
used to die in the storage read: ``_row_to_event`` raised ``CorruptedRecord``
before the projection ran, so the degrade machinery (issue #383) never had an
event to refuse and no verdict existed at all. These tests pin the contract
the issue states: the clean prefix preceding the damage is folded, the
restored state is marked INVALID at the refused sequence, and the verdict is
REQUEST_HUMAN naming it, with a repair step for the broken row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.checkpoint.manager import CheckpointError
from continuum.events import Event, EventType
from continuum.models import RecoveryMode, Run, StateStatus
from continuum.recovery import RecoveryEngine
from continuum.recovery.planner import RepairKind
from continuum.storage import SQLiteStorage
from continuum.storage.base import CorruptedRecord, Storage


def _seed_run(
    storage: SQLiteStorage, run_id: str = "r1", work_units: int = 4, total: int | None = None
) -> None:
    """A run with RUN_STARTED plus ``work_units`` completed units (no checkpoint)."""
    storage.create_run(Run(run_id=run_id, goal="test"))
    storage.append_event(
        run_id,
        EventType.RUN_STARTED,
        {"goal": "test", "total": total if total is not None else work_units},
    )
    for i in range(work_units):
        storage.append_event(run_id, EventType.WORK_COMPLETED, {"count": 1, "doc": i})


def _corrupt_payload(db: Path, run_id: str, sequence: int, *, table: str = "events") -> None:
    """Make one row's payload unparseable, exactly as the issue reproduces."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            f"UPDATE {table} SET payload = ? WHERE run_id = ? AND sequence = ?",
            ('{"broken": "trunc', run_id, sequence),
        )
        conn.commit()
    finally:
        conn.close()


# --- the issue, end to end ------------------------------------------------- #


def test_corrupted_payload_assesses_to_request_human(tmp_path: Path) -> None:
    """The reported crash: assess must answer instead of raising (#1907)."""
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)  # sequences 1..5: RUN_STARTED then 4 x WORK_COMPLETED
    storage.close()
    _corrupt_payload(db, "r1", 4)

    engine = RecoveryEngine(SQLiteStorage(db))
    decision = engine.assess("r1")
    engine.storage.close()

    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.state.status is StateStatus.INVALID
    assert decision.state.unprojectable_at_sequence == 4
    assert decision.state.unprojectable_event_type == "WORK_COMPLETED"
    assert "failed to load" in decision.state.unprojectable_reason
    # The clean prefix folded: sequences 2 and 3 completed, 4 refused, 5 unread.
    assert decision.state.progress.completed == 2
    assert decision.state.progress.total == 4
    # The verdict names the break, and the contract carries the repair step.
    assert any("sequence 4" in reason for reason in decision.rationale)
    assert "sequence 4" in decision.contract.reason
    repair_log = [s for s in decision.plan.steps if s.kind is RepairKind.REPAIR_LOG]
    assert repair_log
    assert repair_log[0].target == "sequence_4"
    assert repair_log[0].requires_human


# --- restore, degrade mode and raise mode ---------------------------------- #


def test_degrade_restore_marks_the_refused_row_on_the_filed_prefix(tmp_path: Path) -> None:
    """restore(degrade) folds the clean prefix and marks it INVALID at the break."""
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)
    storage.close()
    _corrupt_payload(db, "r1", 4)

    manager = CheckpointManager(SQLiteStorage(db))
    restored = manager.restore("r1", on_unprojectable="degrade")
    manager.storage.close()

    assert restored.checkpoint is None
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 4
    assert restored.state.unprojectable_event_type == "WORK_COMPLETED"
    assert restored.state.progress.completed == 2
    assert restored.pending_events == 3


def test_raise_mode_still_raises_corrupted_record(tmp_path: Path) -> None:
    """The write paths that restore feeds keep today's behaviour (#1907).

    ``on_unprojectable="raise"`` is the default for every writer; a read
    refusal must still propagate loudly there rather than becoming a partial
    state a checkpoint could later seal as authoritative.
    """
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)
    storage.close()
    _corrupt_payload(db, "r1", 4)

    manager = CheckpointManager(SQLiteStorage(db))
    with pytest.raises(CorruptedRecord):
        manager.restore("r1")
    manager.storage.close()


def test_checkpointed_run_marks_the_break_in_the_pending_tail(tmp_path: Path) -> None:
    """A checkpointed run degrades at the corrupted pending row, not before it."""
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage, work_units=3, total=5)  # sequences 1..4, 3 of 5 units done
    CheckpointManager(storage).checkpoint("r1")  # annotation at 5, covering 1..4
    storage.append_event("r1", EventType.WORK_COMPLETED, {"count": 1, "doc": 99})  # 6
    storage.append_event("r1", EventType.WORK_COMPLETED, {"count": 1, "doc": 100})  # 7
    storage.close()
    _corrupt_payload(db, "r1", 7)

    manager = CheckpointManager(SQLiteStorage(db))
    restored = manager.restore("r1", on_unprojectable="degrade")
    manager.storage.close()

    assert restored.checkpoint is not None
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 7
    # The checkpoint's 3 units plus sequence 6 folded; sequence 7 refused.
    assert restored.state.progress.completed == 4
    assert restored.replayed is True


def test_refusal_before_any_projectable_row_raises_checkpoint_error(tmp_path: Path) -> None:
    """A break at the very first row leaves no prefix to degrade to.

    The degraded state's contract (issue #383) is a last-good prefix; with no
    loadable row there is no goal, progress or evidence behind one, so the
    failure is reported as a named error instead of a verdict that names a
    break with nothing before it.
    """
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)
    storage.close()
    _corrupt_payload(db, "r1", 1)

    manager = CheckpointManager(SQLiteStorage(db))
    with pytest.raises(CheckpointError, match="first event refused to load"):
        manager.restore("r1", on_unprojectable="degrade")
    manager.storage.close()


# --- the tolerant storage reads ------------------------------------------- #


def test_read_events_until_corrupt_returns_prefix_and_location(tmp_path: Path) -> None:
    """The native read stops at the first refused row and locates it."""
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)
    storage.close()
    _corrupt_payload(db, "r1", 4)

    engine_storage = SQLiteStorage(db)
    events, refused = engine_storage.read_events_until_corrupt("r1")
    engine_storage.close()

    assert [e.sequence for e in events] == [1, 2, 3]
    assert refused is not None
    assert refused.sequence == 4
    assert refused.event_type == "WORK_COMPLETED"
    assert "failed to load" in refused.detail


def test_read_events_until_corrupt_on_a_clean_log(tmp_path: Path) -> None:
    """A clean log reads exactly like read_events, with no refusal."""
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)
    plain = list(storage.read_events("r1"))
    events, refused = storage.read_events_until_corrupt("r1")
    storage.close()

    assert events == plain
    assert refused is None


def test_default_tolerant_read_locates_the_refusal_by_bisection(tmp_path: Path) -> None:
    """Engines without a native override still degrade (#1907).

    The ``Storage`` base implementation bisects plain windowed reads, so a
    third-party engine whose reads are all-or-nothing inherits the same
    tolerance. The simulated engine below just declines to override, so the
    bisection runs against the standard all-or-nothing reader.
    """

    class _AllOrNothingEngine(SQLiteStorage):
        read_events_until_corrupt = Storage.read_events_until_corrupt

    db = tmp_path / "run.db"
    storage = _AllOrNothingEngine(db)
    _seed_run(storage)
    storage.close()
    _corrupt_payload(db, "r1", 4)

    engine = _AllOrNothingEngine(db)
    events, refused = engine.read_events_until_corrupt("r1")
    engine.close()

    assert [e.sequence for e in events] == [1, 2, 3]
    assert refused is not None
    assert refused.sequence == 4
    # A reader this generic cannot see the refusing row's own type column.
    assert refused.event_type == "unknown event"


def test_archived_row_corruption_stops_the_merged_read(tmp_path: Path) -> None:
    """A corrupted archived row costs the tail, not the whole history.

    Compaction keeps the archived prefix readable as one stream with the live
    log (#239); a refusal inside it stops the merged read at that row, and the
    checkpointed state the anchor covers is unaffected.
    """
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)  # sequences 1..5
    storage.compact_run("r1")  # anchor at 6, sequences 1..5 archived
    storage.close()
    _corrupt_payload(db, "r1", 3, table="events_archive")

    engine = SQLiteStorage(db)
    events, refused = engine.read_all_events_until_corrupt("r1")
    engine.close()

    assert [e.sequence for e in events] == [1, 2]
    assert refused is not None
    assert refused.sequence == 3


def test_assess_survives_a_corrupted_archive(tmp_path: Path) -> None:
    """A poisoned archive also yields a verdict, not a traceback (#1907).

    The restored state is covered by the compaction anchor's checkpoint and
    stays valid; the damage costs the ledger and the archive-aware scans, and
    the verdict escalates rather than asserting a conclusion computed from a
    history that could not be read.
    """
    db = tmp_path / "run.db"
    storage = SQLiteStorage(db)
    _seed_run(storage)
    storage.compact_run("r1")
    storage.close()
    _corrupt_payload(db, "r1", 3, table="events_archive")

    engine = RecoveryEngine(SQLiteStorage(db))
    decision = engine.assess("r1")
    engine.storage.close()

    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any("action ledger unreadable" in reason for reason in decision.rationale)


def test_unreadable_ledger_escalates_instead_of_laundering(tmp_path: Path) -> None:
    """An unreadable action ledger raises the verdict, it does not empty it (#1907).

    A fold over no actions would report "no uncertain side effects" for a run
    whose side effects the engine could not see, exactly the laundering issue
    #1066 fixed for consumed authorities.
    """

    class _UnreadableArchive(SQLiteStorage):
        def read_archived_events(
            self, run_id: str, *, upto: int | None = None, rehydrate: bool = True
        ) -> Sequence[Event]:
            raise CorruptedRecord("archive row refused to load")

    db = tmp_path / "run.db"
    storage = _UnreadableArchive(db)
    _seed_run(storage)
    storage.close()

    engine = RecoveryEngine(_UnreadableArchive(db))
    decision = engine.assess("r1")
    engine.storage.close()

    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any("action ledger unreadable" in reason for reason in decision.rationale)
