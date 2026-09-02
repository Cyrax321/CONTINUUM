"""End-to-end admissibility: crash downstream refuse (issue #295, sub-issue #560).

Simulates a run that crashes mid-way, downstream committed work completes
consuming state after the checkpoint, and a rewind-style resume is refused
with a contract that names each blocking commitment and its chain position.
"""

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage


def test_crash_downstream_refused_with_contract() -> None:
    store = SQLiteStorage(":memory:")
    run_id = "run_e2e_adm"
    store.create_run(Run(run_id=run_id, goal="e2e admissibility"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "e2e admissibility"})
    # some work
    store.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "summary": "s"})
    store.append_event(run_id, EventType.FINDING_ADDED, {"finding_id": "f1", "claim": "c", "evidence": ["ev1"]})
    mgr = CheckpointManager(store)
    cp1 = mgr.checkpoint(run_id)
    assert cp1 is not None
    seq1 = cp1.state.source_sequence
    ver1 = cp1.version
    # more work after checkpoint
    store.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev2", "summary": "s2"})
    store.append_event(run_id, EventType.FINDING_ADDED, {"finding_id": "f2", "claim": "c2", "evidence": ["ev2"]})
    # checkpoint again to have a later checkpoint that is still before downstream
    cp2 = mgr.checkpoint(run_id)
    assert cp2 is not None
    seq2 = cp2.state.source_sequence
    # downstream committed effect that consumes state after cp1 and cp2
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("downstream.publish", {"doc": "report"})
    # consume event position after cp2 (so both checkpoints inadmissible)
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [seq2 + 1],
            "component_ids": [],
            "action_ids": [],
        },
    )
    # also test component_ids path: need a valid component that is after checkpoint
    # use f2 which is after cp1 but before cp2? Actually f2 is before cp2, so not after cp2, but after cp1
    # For cp2, f2 is not after, so not blocking via component for cp2. For event we already block cp2.
    # For completeness, test that cp1 is also blocked via component f2
    # Add another downstream that consumes component f2
    out2 = ledger.claim("downstream.publish2", {"doc": "report2"})
    ledger.complete(
        out2.key,
        consumed_inputs={
            "checkpoint_seq": 0,
            "event_positions": [],
            "component_ids": ["f2"],
            "action_ids": [],
        },
    )
    engine = RecoveryEngine(store)
    decision = engine.assess(run_id)
    # Must not be RESUME; must be REPAIR or REQUEST_HUMAN
    assert decision.mode.value != "resume"
    assert decision.mode.value in ("repair_and_resume", "request_human")
    # Contract must name blocking commitments with chain positions
    contract = decision.contract
    # invalidated should contain action entries with positions
    assert any("action:" in inv for inv in contract.invalidated)
    assert any("at position" in inv for inv in contract.invalidated)
    # evidence should contain blocking commitment details
    assert any("blocking commitment" in ev for ev in contract.evidence)
    assert any("chain_position" in ev or "at position" in ev for ev in contract.evidence)
    # reason must be machine-readable and list blocking
    assert "inadmissible" in contract.reason
    assert "blocking commitment" in contract.reason or "blocking" in contract.reason
    # Validation report must have ACTION entry
    assert any(e.component.value == "action" for e in decision.validation.report.statuses)
    store.close()


def test_admissible_checkpoint_still_resumes() -> None:
    store = SQLiteStorage(":memory:")
    run_id = "run_e2e_ok"
    store.create_run(Run(run_id=run_id, goal="ok"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "ok"})
    mgr = CheckpointManager(store)
    mgr.checkpoint(run_id)
    # downstream with empty consumed_inputs is admissible
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("a.ok", {})
    ledger.complete(out.key)
    engine = RecoveryEngine(store)
    decision = engine.assess(run_id)
    # Should be RESUME or REPAIR due to other reasons, but not blocked by admissibility
    # Since no blocking, and no other issues, should be RESUME
    assert decision.mode.value == "resume"
    assert decision.contract.recovery_status.value == "safe_to_resume"
    store.close()


def test_rewind_style_resume_refused() -> None:
    # Simulate trying to resume from an earlier checkpoint after downstream work
    # Engine always uses latest checkpoint, but we can manually check earlier
    # checkpoint admissibility via check_admissibility
    from continuum.state.validator import check_admissibility

    store = SQLiteStorage(":memory:")
    run_id = "run_rewind"
    store.create_run(Run(run_id=run_id, goal="rewind"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "rewind"})
    mgr = CheckpointManager(store)
    cp1 = mgr.checkpoint(run_id)
    assert cp1 is not None
    # downstream work
    ledger = ActionLedger(store, run_id)
    out = ledger.claim("downstream.work", {})
    ledger.complete(
        out.key,
        consumed_inputs={
            "checkpoint_seq": cp1.version + 1,
            "event_positions": [],
            "component_ids": [],
            "action_ids": [],
        },
    )
    # Even though cp1 is an older checkpoint, check_admissibility should mark it inadmissible
    result = check_admissibility(cp1, ledger.all())
    assert result.admissible is False
    assert "checkpoint_seq" in result.reason
    # Latest checkpoint is also inadmissible because downstream consumed after it? Actually downstream consumed checkpoint_seq > cp1.version, but cp1.version is older, latest checkpoint version is same as cp1 if no new checkpoint after downstream? Let's create a new checkpoint after downstream? No, downstream is after cp1, so cp1 is inadmissible.
    # Engine assess on latest (cp1) should be refused
    engine = RecoveryEngine(store)
    decision = engine.assess(run_id)
    assert decision.mode.value != "resume"
    store.close()
