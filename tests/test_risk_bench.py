"""Risk bench fault injection and no duplicate effects (issue #564)."""

from __future__ import annotations

from pathlib import Path

from continuum.events import EventType
from continuum.models import RecoveryMode, Run
from continuum.recovery import RecoveryEngine
from continuum.recovery.risk import ingest_risk
from continuum.storage import SQLiteStorage

from benchmarks.fault_injection.risk_bench import (
    RISK_FAULTS,
    assert_no_duplicate_after_abort,
    run_risk_fault_suite,
)


def test_risk_faults_choose_correct_mode(tmp_path: Path) -> None:
    for fault in RISK_FAULTS:
        db = str(tmp_path / f"{fault.trigger}.db")
        with SQLiteStorage(db) as store:
            run_id = f"run_{fault.trigger}"
            store.create_run_started(Run(run_id=run_id, goal="bench"))
            result = run_risk_fault_suite(store, run_id, fault.trigger)
            expected = fault.expected_mode
            if expected is None:
                assert result["actual_mode"] == "resume"
                assert result["triggering_risks"] == []
            else:
                assert result["actual_mode"] == expected
                assert len(result["triggering_risks"]) > 0
                engine = RecoveryEngine(store)
                decision = engine.assess(run_id)
                assert fault.trigger in [
                    e.payload.get("trigger")
                    for e in store.read_events(run_id)
                    if e.type == EventType.RISK_OBSERVED
                ]
                assert len(decision.contract.triggering_risks) > 0


def test_no_duplicate_effects_after_abort(tmp_path: Path) -> None:
    assert assert_no_duplicate_after_abort(str(tmp_path / "dup.db")) is True


def test_risk_does_not_cause_duplicate_side_effect(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_dup.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_dup"
        store.create_run_started(Run(run_id=run_id, goal="dup test"))
        from continuum.actions.ledger import ActionLedger

        ledger = ActionLedger(store, run_id)
        claim1 = ledger.claim("payment.charge", {"amount": 100}, key="charge:100")
        assert claim1.fresh is True
        ingest_risk(store, run_id, {"trigger": "side_effect_duplicate"})
        engine = RecoveryEngine(store)
        decision = engine.assess(run_id)
        assert decision.mode == RecoveryMode.ABORT
        ledger.complete(claim1.key, result={"id": "ch_1"})
        claim2 = ledger.claim("payment.charge", {"amount": 100}, key="charge:100")
        assert claim2.fresh is False
        assert claim2.result is not None
