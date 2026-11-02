"""A damaged log still gets a verdict, not a traceback (#1907).

``RecoveryEngine.assess`` calls ``CheckpointManager.restore`` with
``on_unprojectable="degrade"`` on purpose: the engine's whole job is to
answer where a run stands, and a poisoned log is precisely the run that
needs an answer (issue #383). But the degrade contract only covered events
that could be *read* and then failed to fold. A row whose payload could not
be parsed at all raised ``CorruptedRecord`` out of ``storage.read_events``
before projection -- or out of the engine's auxiliary readers -- so ``assess``
crashed on the one run it was built for.

``CorruptedRecord`` now locates the refusing row, ``restore`` folds the clean
prefix preceding it and marks the state INVALID at the refused sequence, and
``assess`` degrades its auxiliary readers instead of letting the read error
escape.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import Event, EventType
from continuum.models import RecoveryMode, Run, StateStatus
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage
from continuum.storage.base import CorruptedRecord


def _build_log(tmp_path: Path, run_id: str = "damaged") -> Path:
    """A run whose first two events fold cleanly and whose third is corrupt.

    Corruption happens below the storage layer, the way bit rot or a crashed
    flush actually lands: the payload column holds invalid JSON afterwards.
    """
    db = tmp_path / "damaged.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g", "total": 3})
    storage.append_event(run_id, EventType.WORK_COMPLETED, {"doc": 0})
    storage.append_event(run_id, EventType.WORK_COMPLETED, {"doc": 1})
    storage.close()
    _corrupt_event(db, run_id, sequence=3)
    return db


def _corrupt_event(
    db: Path, run_id: str, *, sequence: int, payload: str = '{"broken": "trunc'
) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE events SET payload = ? WHERE run_id = ? AND sequence = ?",
            (payload, run_id, sequence),
        )
        conn.commit()
    finally:
        conn.close()


def _engine(db: Path) -> RecoveryEngine:
    return RecoveryEngine(SQLiteStorage(db))


# --- the storage refusal locates the damage -------------------------------- #


def test_corrupted_record_locates_the_refusing_row(tmp_path: Path) -> None:
    db = _build_log(tmp_path)
    storage = SQLiteStorage(db)
    try:
        with pytest.raises(CorruptedRecord) as excinfo:
            storage.read_events("damaged")
    finally:
        storage.close()
    assert excinfo.value.run_id == "damaged"
    assert excinfo.value.sequence == 3


def test_corrupted_record_without_a_row_has_no_location(tmp_path: Path) -> None:
    """Refusals raised outside a per-row read carry no location."""
    exc = CorruptedRecord("checkpoint failed to load")
    assert exc.run_id is None
    assert exc.sequence is None


# --- restore ---------------------------------------------------------------- #


def test_restore_degrades_on_corrupted_tail(tmp_path: Path) -> None:
    db = _build_log(tmp_path)
    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("damaged", on_unprojectable="degrade")
    finally:
        storage.close()
    state = restored.state
    assert state.status is StateStatus.INVALID
    assert state.unprojectable_at_sequence == 3
    reason = state.unprojectable_reason
    assert reason is not None
    assert "corrupted event record" in reason
    # The clean prefix folded: the refusal point is named, not the whole log.
    assert state.source_sequence == 2
    assert state.goal is not None


def test_restore_raises_by_default_on_corrupted_tail(tmp_path: Path) -> None:
    """Write paths keep the loud failure: they must never trust a partial fold."""
    db = _build_log(tmp_path)
    storage = SQLiteStorage(db)
    try:
        with pytest.raises(CorruptedRecord):
            CheckpointManager(storage).restore("damaged")
    finally:
        storage.close()


def test_restore_degrades_when_a_checkpoint_exists(tmp_path: Path) -> None:
    """The checkpointed path degrades the same way as the no-checkpoint one."""
    db = tmp_path / "checkpointed.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 3})
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 0})
    env = capture("r", StaticProvider(dataset="v1"))
    CheckpointManager(storage).checkpoint("r", environment=env)
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 1})
    storage.close()
    _corrupt_event(db, "r", sequence=4)

    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("r", on_unprojectable="degrade")
    finally:
        storage.close()
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 4


def test_restore_raises_when_nothing_is_readable(tmp_path: Path) -> None:
    """A corrupted first event leaves no prefix to report (build_degrade's guard)."""
    db = tmp_path / "dead.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"})
    storage.close()
    _corrupt_event(db, "r", sequence=1)

    storage = SQLiteStorage(db)
    try:
        with pytest.raises(CorruptedRecord):
            CheckpointManager(storage).restore("r", on_unprojectable="degrade")
    finally:
        storage.close()


# --- assess ----------------------------------------------------------------- #


def test_assess_returns_advisory_verdict_on_corrupted_event(tmp_path: Path) -> None:
    """The issue's reproduction: assess answers instead of crashing."""
    db = _build_log(tmp_path)
    decision = _engine(db).assess("damaged")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.restored.state.status is StateStatus.INVALID
    assert decision.restored.state.unprojectable_at_sequence == 3
    rationale = "; ".join(decision.rationale)
    assert "sequence 3" in rationale
    assert "action ledger unreadable" in rationale


def test_assess_includes_a_repair_step_for_the_damaged_log(tmp_path: Path) -> None:
    """The degraded fold must reach the plan (#383), also for an unreadable row."""
    db = _build_log(tmp_path)
    decision = _engine(db).assess("damaged")
    assert decision.plan.steps
    assert any(step.requires_human for step in decision.plan.steps)


def test_assess_survives_a_corrupted_action_ledger(tmp_path: Path) -> None:
    """An in-flight side effect before the damage cannot be enumerated, so the
    verdict names the unreadable ledger instead of guessing it away."""
    db = tmp_path / "inflight.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 2})
    from continuum.actions import ActionLedger

    claim = ActionLedger(storage, "r").claim("stripe.charge", {"amount": 10})
    assert claim.fresh is True
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 0})
    storage.close()
    _corrupt_event(db, "r", sequence=3)

    decision = _engine(db).assess("r")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any("action ledger unreadable" in line for line in decision.rationale)


def test_assess_on_a_clean_log_is_unchanged(tmp_path: Path) -> None:
    """The fix must not change the verdict a healthy run gets."""
    db = tmp_path / "clean.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 2})
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 0})
    storage.close()

    decision = _engine(db).assess("r")
    assert decision.mode is RecoveryMode.RESUME
    assert decision.restored.state.status is StateStatus.VALID


# --- the bound cases: located, unlocated, and unrecoverable refusals ------- #


def _checkpointed_db(tmp_path: Path, *, run_id: str = "r") -> Path:
    """A run sealed at sequence 2 (annotation at 3), with two live events after.

    Two post-checkpoint events let the pending window be non-empty when the
    corrupted row is not the first one after the annotation.
    """
    db = tmp_path / "checkpointed.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g", "total": 3})
    storage.append_event(run_id, EventType.WORK_COMPLETED, {"doc": 0})
    env = capture(run_id, StaticProvider(dataset="v1"))
    CheckpointManager(storage).checkpoint(run_id, environment=env)
    storage.append_event(run_id, EventType.WORK_COMPLETED, {"doc": 1})
    storage.append_event(run_id, EventType.WORK_COMPLETED, {"doc": 2})
    storage.close()
    return db


def test_restore_marks_the_checkpoint_itself_when_nothing_follows(tmp_path: Path) -> None:
    """Damage on the checkpoint's own annotation row leaves an empty pending
    window after the cursor falls back, so the sealed checkpoint's state
    carries the refusal mark (#1907)."""
    db = _checkpointed_db(tmp_path)
    # seq 3 is the STATE_CHECKPOINTED annotation: an unguarded read of it in
    # _cursor_for would escape before the pending read is ever attempted.
    _corrupt_event(db, "r", sequence=3)

    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("r", on_unprojectable="degrade")
    finally:
        storage.close()
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 3
    assert restored.replayed is False
    assert restored.pending_events == 0


def test_restore_marks_the_incrementally_folded_state_when_the_tail_is_corrupt(
    tmp_path: Path,
) -> None:
    """A readable event between the checkpoint and the damage still folds, and
    the refusal marks the state the incremental projection produced."""
    db = _checkpointed_db(tmp_path)
    _corrupt_event(db, "r", sequence=5)  # seq 4 survives the clean prefix

    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("r", on_unprojectable="degrade")
    finally:
        storage.close()
    assert restored.state.status is StateStatus.INVALID
    assert restored.state.unprojectable_at_sequence == 5
    assert restored.replayed is True
    assert restored.pending_events == 1


def test_restore_raises_by_default_when_the_annotation_row_is_corrupt(tmp_path: Path) -> None:
    """The cursor fallback is degrade-only: write paths keep the loud failure."""
    db = _checkpointed_db(tmp_path)
    _corrupt_event(db, "r", sequence=3)

    storage = SQLiteStorage(db)
    try:
        with pytest.raises(CorruptedRecord):
            CheckpointManager(storage).restore("r")
    finally:
        storage.close()


def test_restore_raises_by_default_when_a_checkpoint_exists(tmp_path: Path) -> None:
    """The checkpointed pending read keeps the loud failure under the default."""
    db = _checkpointed_db(tmp_path)
    _corrupt_event(db, "r", sequence=4)

    storage = SQLiteStorage(db)
    try:
        with pytest.raises(CorruptedRecord):
            CheckpointManager(storage).restore("r")
    finally:
        storage.close()


def test_restore_flattens_a_multiline_refusal_reason(tmp_path: Path) -> None:
    """A schema validation error spans several lines; the state field may not.

    ``unprojectable_reason`` is joined into single-line rationale strings, so
    a pydantic ``ValidationError`` is folded to its first line at the mark.
    """
    db = _build_log(tmp_path)
    # Valid JSON that is not a valid event payload: pydantic reports it over
    # multiple lines, unlike the truncated-JSON corruption above.
    _corrupt_event(db, "damaged", sequence=3, payload="[1, 2, 3]")

    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("damaged", on_unprojectable="degrade")
    finally:
        storage.close()
    reason = restored.state.unprojectable_reason
    assert reason is not None
    assert "corrupted event record" in reason
    assert "\n" not in reason


class _UnlocatingStorage(SQLiteStorage):
    """A real storage whose live read refuses without locating the row.

    The bound case of a read failure: no sequence to fold up to, so no clean
    prefix can be separated from the damaged tail.
    """

    def read_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        upto: int | None = None,
        rehydrate: bool = True,
    ) -> Sequence[Event]:
        del run_id, after_sequence, upto, rehydrate
        raise CorruptedRecord("unreadable page (no row context)")


def test_restore_reraises_a_refusal_that_locates_no_row(tmp_path: Path) -> None:
    """Degrade cannot invent a boundary the refusal did not name (#1907)."""
    db = tmp_path / "refusing.db"
    _UnlocatingStorage(db).create_run(Run(run_id="damaged", goal="g"))

    with pytest.raises(CorruptedRecord):
        CheckpointManager(_UnlocatingStorage(db)).restore("damaged", on_unprojectable="degrade")


def test_assess_tolerates_an_unlocated_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the all-events scan cannot even name the damage, it sees no
    history rather than a silently bounded one, and assess still answers."""
    db = _build_log(tmp_path)
    engine = _engine(db)

    def refuse(*args: object, **kwargs: object) -> Sequence[Event]:
        raise CorruptedRecord("unreadable page (no row context)")

    monkeypatch.setattr(engine.storage, "read_all_events", refuse)
    decision = engine.assess("damaged")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN


def test_assess_tolerates_a_refusal_that_cannot_be_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The salvage re-read can itself fail: the scan degrades to empty and
    assess still answers, because losing evidence never downgrades a verdict."""
    db = _build_log(tmp_path)
    engine = _engine(db)

    def refuse(*args: object, **kwargs: object) -> Sequence[Event]:
        raise CorruptedRecord("events table: unreadable page", sequence=2)

    monkeypatch.setattr(engine.storage, "read_all_events", refuse)
    decision = engine.assess("damaged")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN


# --- earlier unprojectable break location preserved over later corruption --- #


def test_restore_preserves_earlier_unprojectable_break_before_corrupted_row(
    tmp_path: Path,
) -> None:
    """When an event in the clean prefix cannot be projected, the fold stops
    there: a later corrupted row must not overwrite that earlier break sequence
    or event type with the corruption refusal (issue #1907)."""
    db = tmp_path / "early_break.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 3})
    # Seq 2 is unprojectable: completed exceeds total
    storage.append_event("r", EventType.TASK_UPDATED, {"completed": 999, "total": 3})
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 1})
    storage.close()
    _corrupt_event(db, "r", sequence=4)

    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("r", on_unprojectable="degrade")
    finally:
        storage.close()

    state = restored.state
    assert state.status is StateStatus.INVALID
    # Earliest break at seq 2 is preserved, not clobbered by corrupted row at seq 4
    assert state.unprojectable_at_sequence == 2
    assert state.unprojectable_event_type == "TASK_UPDATED"
    assert "exceeds total" in (state.unprojectable_reason or "")
    assert state.source_sequence == 1


def test_restore_preserves_earlier_unprojectable_break_in_checkpoint_replay(
    tmp_path: Path,
) -> None:
    """The incremental replay path also keeps the earlier unprojectable break
    from pending replay when a subsequent pending row is unparseable."""
    db = tmp_path / "early_break_chk.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 3})
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 0})
    env = capture("r", StaticProvider(dataset="v1"))
    CheckpointManager(storage).checkpoint("r", environment=env)
    # Checkpoint annotation is at seq 3.
    # Seq 4 is unprojectable during replay:
    storage.append_event("r", EventType.TASK_UPDATED, {"completed": 999, "total": 3})
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 2})
    storage.close()
    _corrupt_event(db, "r", sequence=6)

    storage = SQLiteStorage(db)
    try:
        restored = CheckpointManager(storage).restore("r", on_unprojectable="degrade")
    finally:
        storage.close()

    state = restored.state
    assert state.status is StateStatus.INVALID
    assert state.unprojectable_at_sequence == 4
    assert state.unprojectable_event_type == "TASK_UPDATED"


def test_assess_targets_earlier_unprojectable_break_when_later_row_is_corrupt(
    tmp_path: Path,
) -> None:
    """Assess diagnosis and repair planning target the earliest break where
    folding stopped, rather than pointing the operator to the later corruption."""
    db = tmp_path / "early_break_assess.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 3})
    storage.append_event("r", EventType.TASK_UPDATED, {"completed": 999, "total": 3})
    storage.append_event("r", EventType.WORK_COMPLETED, {"doc": 1})
    storage.close()
    _corrupt_event(db, "r", sequence=4)

    decision = _engine(db).assess("r")
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.restored.state.status is StateStatus.INVALID
    assert decision.restored.state.unprojectable_at_sequence == 2
    assert any(step.target == "sequence_2" for step in decision.plan.steps)
    assert any(step.kind.value == "repair_log" for step in decision.plan.steps)
