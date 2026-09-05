"""Admissibility reachability and validator mapping (issue #295, sub-issue #559)."""

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery import RecoveryEngine
from continuum.state.validator import check_admissibility
from continuum.storage import SQLiteStorage


def _store_with_checkpoint(run_id: str = "run_559") -> tuple[SQLiteStorage, str]:
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id=run_id, goal="g"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "g", "total": 10})
    mgr = CheckpointManager(store)
    mgr.checkpoint(run_id)
    return store, run_id


def test_admissible_when_no_consumed_after() -> None:
    store, run_id = _store_with_checkpoint("run_adm_ok")
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.do", {"x": 1})
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [],
            "component_ids": [],
            "action_ids": [],
        },
    )
    out2 = ledger.claim("a.do2", {"y": 2})
    ledger.complete(out2.key)
    mgr = CheckpointManager(store)
    restored = mgr.restore(run_id)
    result = check_admissibility(restored.checkpoint, ledger.all())
    assert result.admissible is True
    assert result.blocking == ()
    assert result.reason == ""
    store.close()


def test_inadmissible_via_event_position() -> None:
    store, run_id = _store_with_checkpoint("run_adm_event")
    mgr = CheckpointManager(store)
    cp = store.latest_checkpoint(run_id)
    assert cp is not None
    seq = cp.state.source_sequence
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.downstream", {"v": 1})
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [seq + 5],
            "component_ids": [],
            "action_ids": [],
        },
    )
    restored = mgr.restore(run_id)
    result = check_admissibility(restored.checkpoint, ledger.all())
    assert result.admissible is False
    assert len(result.blocking) == 1
    assert "event position" in result.reason
    assert result.details[0]["chain_position"] >= 1
    assert result.details[0]["consumed_inputs"]["event_positions"] == [seq + 5]
    store.close()


def test_inadmissible_via_component_id() -> None:
    store, run_id = _store_with_checkpoint("run_adm_comp")
    mgr = CheckpointManager(store)
    cp = store.latest_checkpoint(run_id)
    assert cp is not None
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.comp", {})
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [],
            "component_ids": ["decision_unknown"],
            "action_ids": [],
        },
    )
    restored = mgr.restore(run_id)
    result = check_admissibility(restored.checkpoint, ledger.all())
    assert result.admissible is False
    assert "component" in result.reason
    store.close()


def test_inadmissible_via_checkpoint_seq() -> None:
    store, run_id = _store_with_checkpoint("run_adm_seq")
    mgr = CheckpointManager(store)
    cp = store.latest_checkpoint(run_id)
    assert cp is not None
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.seq", {})
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": cp.version + 1,
            "event_positions": [],
            "component_ids": [],
            "action_ids": [],
        },
    )
    restored = mgr.restore(run_id)
    result = check_admissibility(restored.checkpoint, ledger.all())
    assert result.admissible is False
    assert "checkpoint_seq" in result.reason
    store.close()


def test_engine_maps_event_to_repair() -> None:
    store, run_id = _store_with_checkpoint("run_engine_repair")
    CheckpointManager(store)
    cp = store.latest_checkpoint(run_id)
    assert cp is not None
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.repair", {})
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [cp.state.source_sequence + 1],
            "component_ids": [],
            "action_ids": [],
        },
    )
    engine = RecoveryEngine(store)
    decision = engine.assess(run_id)
    assert decision.mode.value == "repair_and_resume"
    assert "inadmissible" in decision.validation.report.reason
    assert any(e.component.value == "action" for e in decision.validation.report.statuses)
    store.close()


def test_engine_maps_action_ids_to_request_human() -> None:
    store, run_id = _store_with_checkpoint("run_engine_human")
    CheckpointManager(store)
    cp = store.latest_checkpoint(run_id)
    assert cp is not None
    ledger = ActionLedger(store, run_id)
    out1 = ledger.claim("a.first", {})
    ledger.complete(out1.key)
    out2 = ledger.claim("a.second", {})
    ledger.complete(
        out2.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [],
            "component_ids": [],
            "action_ids": [out1.action.action_id],
        },
    )
    engine = RecoveryEngine(store)
    decision = engine.assess(run_id)
    assert decision.mode.value == "request_human"
    assert "inadmissible" in decision.validation.report.reason
    store.close()


def test_old_rows_without_consumed_remain_admissible() -> None:
    store, run_id = _store_with_checkpoint("run_old_adm")
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.old", {})
    ledger.complete(out.key)
    mgr = CheckpointManager(store)
    restored = mgr.restore(run_id)
    result = check_admissibility(restored.checkpoint, ledger.all())
    assert result.admissible is True
    store.close()


def test_non_completed_not_blocking() -> None:
    store, run_id = _store_with_checkpoint("run_noncomp")
    mgr = CheckpointManager(store)
    cp = store.latest_checkpoint(run_id)
    assert cp is not None
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.pending", {})
    ledger.fail(out.key, "boom", certain=True)
    restored = mgr.restore(run_id)
    result = check_admissibility(restored.checkpoint, ledger.all())
    assert result.admissible is True
    store.close()
