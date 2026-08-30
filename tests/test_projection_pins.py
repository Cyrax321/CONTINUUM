"""Projector folds pins into SemanticState with anchoring-safe lifecycle (#417).

Properties tested:
- prefix-closed: prefix projection equals incremental fold
- replayable: identical log prefixes produce identical states
- anchoring-safe: compacted logs project identically (pins survive anchoring)
- retraction removes from active set
- unknown retraction degrades gracefully and is noted
"""

from __future__ import annotations

import hashlib

from continuum.checkpoint import CheckpointManager
from continuum.events import EventLog, EventType
from continuum.models import ConstraintPinned, ConstraintRetracted, Run
from continuum.state.semantic import project, project_incremental
from continuum.storage import SQLiteStorage


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _started(log: EventLog, run_id: str = "run_1") -> EventLog:
    log.append(run_id, EventType.RUN_STARTED, {"goal": "g", "total": 10})
    return log


def test_pinned_constraints_appear_in_active_set() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c2", sha256=_digest("world")).model_dump(),
    )
    state = project("run_1", log.events("run_1"))
    assert set(state.pins.keys()) == {"c1", "c2"}
    assert state.pins["c1"].sha256 == _digest("hello")
    assert state.pins["c1"].status == "active"
    assert state.pin("c1") is not None
    assert state.pin("c1").constraint_id == "c1"
    assert state.unmatched_pin_retractions == []


def test_retraction_removes_from_active_set() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c2", sha256=_digest("world")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="c1").model_dump(),
    )
    state = project("run_1", log.events("run_1"))
    assert "c1" not in state.pins
    assert "c2" in state.pins
    assert state.unmatched_pin_retractions == []


def test_retraction_of_unknown_id_degrades_gracefully_and_is_noted() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="never_pinned").model_dump(),
    )
    state = project("run_1", log.events("run_1"))
    assert state.pins == {}
    assert "never_pinned" in state.unmatched_pin_retractions
    # second identical retraction does not duplicate the note
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="never_pinned").model_dump(),
    )
    state2 = project("run_1", log.events("run_1"))
    assert state2.unmatched_pin_retractions.count("never_pinned") == 1


def test_duplicate_pin_overwrites_with_latest_sha() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("first")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("second")).model_dump(),
    )
    state = project("run_1", log.events("run_1"))
    assert state.pins["a"].sha256 == _digest("second")


def test_retract_then_repin_restores_active() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("first")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="a").model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("third")).model_dump(),
    )
    state = project("run_1", log.events("run_1"))
    assert state.pins["a"].sha256 == _digest("third")
    assert state.unmatched_pin_retractions == []


def test_projection_is_prefix_closed_with_pins() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("hello")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="b", sha256=_digest("world")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="a").model_dump(),
    )
    events = list(log.events("run_1"))
    # full
    full = project("run_1", events)
    # prefix upto 2 vs incremental
    p2 = project("run_1", events[:2])
    base, _ = project_incremental("run_1", events[:2])
    # incremental step from base with next event (retraction) should equal prefix 3?
    # Actually test prefix-closed: project(events[:n]) equals incremental fold
    for n in range(1, len(events) + 1):
        prefix = project("run_1", events[:n])
        # incremental: fold first n-1 as base, then 1 step
        if n == 1:
            inc, _ = project_incremental("run_1", events[:1])
        else:
            b, _ = project_incremental("run_1", events[: n - 1])
            inc, _ = project_incremental("run_1", events[n - 1 : n], base=b)
        assert prefix == inc, f"prefix {n} mismatch"
    # also full equals incremental from base
    base2, _ = project_incremental("run_1", events[:2])
    inc_full, _ = project_incremental("run_1", events[2:], base=base2)
    assert inc_full == full
    assert p2.pins == base.pins


def test_projection_is_replayable_with_pins() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("hello")).model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="a").model_dump(),
    )
    first = project("run_1", log.events("run_1"))
    second = project("run_1", log.events("run_1"))
    assert first == second
    # incremental replay also identical
    base, _ = project_incremental("run_1", list(log.events("run_1"))[:2])
    inc1, _ = project_incremental("run_1", list(log.events("run_1"))[2:], base=base)
    assert inc1 == first


def test_pins_survive_compaction_anchoring_safe() -> None:
    run_id = "run_1"
    with SQLiteStorage(":memory:") as store:
        store.create_run(Run(run_id=run_id, goal="g"))
        store.append_event(run_id, EventType.RUN_STARTED, {"goal": "g", "total": 10})
        store.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
        store.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c2", sha256=_digest("world")).model_dump(),
        )
        CheckpointManager(store).checkpoint(run_id)
        store.append_event(run_id, EventType.WORK_COMPLETED, {})
        store.append_event(
            run_id,
            EventType.CONSTRAINT_RETRACTED,
            ConstraintRetracted(constraint_id="c1").model_dump(),
        )
        # before compaction state
        before_events = list(store.read_events(run_id))
        before_state = project(run_id, before_events)
        assert set(before_state.pins.keys()) == {"c2"}

        store.compact_run(run_id)
        live = list(store.read_events(run_id))
        archived = list(store.read_archived_events(run_id))
        combined = sorted([*archived, *live], key=lambda e: e.sequence)
        combined_state = project(run_id, combined)
        # pins survive anchoring
        assert set(combined_state.pins.keys()) == {"c2"}
        assert combined_state.pins["c2"].sha256 == _digest("world")
        # restored via checkpoint also survives
        restored = CheckpointManager(store).restore(run_id)
        assert set(restored.state.pins.keys()) == {"c2"}

        # unmatched retractions also survive if they were in archived tail
        # (add unknown retraction before compaction)
        store2_run = "run_2"
        store.create_run(Run(run_id=store2_run, goal="g"))
        store.append_event(store2_run, EventType.RUN_STARTED, {"goal": "g"})
        store.append_event(
            store2_run,
            EventType.CONSTRAINT_RETRACTED,
            ConstraintRetracted(constraint_id="ghost").model_dump(),
        )
        CheckpointManager(store).checkpoint(store2_run)
        before2 = project(store2_run, list(store.read_events(store2_run)))
        assert "ghost" in before2.unmatched_pin_retractions
        store.compact_run(store2_run)
        archived2 = list(store.read_archived_events(store2_run))
        live2 = list(store.read_events(store2_run))
        combined2 = sorted([*archived2, *live2], key=lambda e: e.sequence)
        after2 = project(store2_run, combined2)
        assert "ghost" in after2.unmatched_pin_retractions


def test_incremental_with_unmatched_preserves_notes() -> None:
    log = _started(EventLog())
    log.append(
        "run_1",
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="ghost").model_dump(),
    )
    log.append(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="a", sha256=_digest("x")).model_dump(),
    )
    events = list(log.events("run_1"))
    base, _ = project_incremental("run_1", events[:2])
    assert "ghost" in base.unmatched_pin_retractions
    full, _ = project_incremental("run_1", events[2:], base=base)
    assert "ghost" in full.unmatched_pin_retractions
    assert "a" in full.pins
    # full equals direct project
    assert full == project("run_1", events)
