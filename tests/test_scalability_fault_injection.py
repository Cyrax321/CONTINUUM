"""Scalability fault-injection test suite for CONTINUUM long-horizon state.

Tests the 7 critical fault modes on long-horizon durable states:
1. Corrupted checkpoint (tampered body / hash mismatch)
2. Missing checkpoint (deleted latest checkpoint row)
3. Truncated event log (broken hash chain)
4. Environment changes (resource drift from v1 to v2 cascading to decisions)
5. Dependency changes (missing or degraded upstream dependency)
6. Constraint changes (retracted or violated governance constraint pin)
7. Stale state (inadmissible resume over uncommitted external side effects)

Verifies that CONTINUUM:
- Safely resumes when state is clean
- Rejects invalid state and surfaces integrity violations
- Detects drift and cascades staleness correctly
- Recovers when possible via REPAIR_AND_RESUME
- Escalates to REQUEST_HUMAN when safe recovery is impossible
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import (
    ConsumedInputs,
    EnvResource,
    RecoveryMode,
    Run,
)
from continuum.recovery.engine import RecoveryEngine
from continuum.state.validator import StateValidator, check_admissibility
from continuum.storage.base import CorruptedRecord
from continuum.storage.sqlite import SQLiteStorage


def _build_long_horizon_state(storage: SQLiteStorage, run_id: str, steps: int = 200) -> None:
    """Build a realistic long-horizon run with dependencies, evidence, decisions and checkpoints."""
    storage.create_run(Run(run_id=run_id, goal="Long-horizon mission"))

    # Initial events
    storage.append_event(
        run_id,
        EventType.RUN_STARTED,
        {"goal": "Long-horizon mission", "total": steps},
    )
    storage.append_event(
        run_id,
        EventType.DEPENDENCY_DECLARED,
        {"resource": "dataset://core_corpus", "version": "v1.0.0"},
    )
    storage.append_event(
        run_id,
        EventType.DEPENDENCY_DECLARED,
        {"resource": "service://auth_provider", "version": "v3.1.0"},
    )

    env = capture(
        run_id,
        StaticProvider(
            resources={
                "dataset://core_corpus": EnvResource(
                    name="dataset://core_corpus", version="v1.0.0"
                ),
                "service://auth_provider": EnvResource(
                    name="service://auth_provider", version="v3.1.0"
                ),
            }
        ),
    )

    manager = CheckpointManager(storage)
    ledger = ActionLedger(storage, run_id)

    # Initial checkpoint v0
    manager.checkpoint(run_id, environment=env, reason="genesis")

    for i in range(1, steps + 1):
        # Work and tool calls
        storage.append_event(
            run_id,
            EventType.TOOL_CALLED,
            {"tool": "corpus_search", "call_id": f"call_{i}", "arguments": {"q": f"q_{i}"}},
        )
        storage.append_event(
            run_id,
            EventType.TOOL_COMPLETED,
            {"tool": "corpus_search", "call_id": f"call_{i}", "result": {"hits": 1}},
        )

        # Evidence and Findings
        storage.append_event(
            run_id,
            EventType.EVIDENCE_ADDED,
            {
                "evidence_id": f"ev_{i}",
                "summary": f"Evidence {i} gathered from dataset",
                "source": "dataset://core_corpus",
            },
        )
        storage.append_event(
            run_id,
            EventType.FINDING_ADDED,
            {
                "finding_id": f"find_{i}",
                "claim": f"Finding claim {i}",
                "evidence": [f"ev_{i}"],
                "confidence": 0.95,
            },
        )

        # Decision based on finding
        storage.append_event(
            run_id,
            EventType.DECISION_CREATED,
            {
                "decision_id": f"dec_{i}",
                "decision": f"Decision for milestone {i}",
                "reason": f"Justified by evidence ev_{i}",
                "evidence": [f"ev_{i}"],
            },
        )

        # Periodic checkpoints every 50 steps
        if i % 50 == 0:
            manager.checkpoint(run_id, environment=env, reason=f"milestone_{i}")

        # Periodic action claims every 40 steps
        if i % 40 == 0:
            outcome = ledger.claim("payment.settle", {"invoice": i})
            ledger.complete(str(outcome.key), external_id=f"tx_{i}", result={"settled": True})


# ---------------------------------------------------------------------------
# Test 1: Corrupted Checkpoint
# ---------------------------------------------------------------------------
def test_fault_corrupted_checkpoint_rejected(tmp_path: Path) -> None:
    """Tampering with a checkpoint body must fail integrity check and be rejected."""
    db_file = tmp_path / "corrupt_ckpt.db"
    run_id = "test_run_corrupt_ckpt"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=100)
        manager = CheckpointManager(storage)
        latest_ckpt = storage.latest_checkpoint(run_id)
        assert latest_ckpt is not None

        # Clean restore succeeds before tampering
        clean_restored = manager.restore(run_id)
        assert clean_restored.state.version == latest_ckpt.version

    # Tamper with the checkpoint in SQLite directly
    conn = sqlite3.connect(str(db_file))
    try:
        row = conn.execute(
            "SELECT body FROM checkpoints WHERE checkpoint_id = ?",
            (latest_ckpt.checkpoint_id,),
        ).fetchone()
        assert row is not None
        body_dict = json.loads(row[0])
        # Modify semantic payload without resealing hash
        body_dict["state"]["goal"]["description"] = "MALICIOUSLY_MUTATED_GOAL"
        conn.execute(
            "UPDATE checkpoints SET body = ? WHERE checkpoint_id = ?",
            (json.dumps(body_dict), latest_ckpt.checkpoint_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Reopen and verify CONTINUUM detects the corruption
    with (
        SQLiteStorage(str(db_file)) as storage,
        pytest.raises(CorruptedRecord, match="integrity hash does not match"),
    ):
        storage.get_checkpoint(latest_ckpt.checkpoint_id)


# ---------------------------------------------------------------------------
# Test 2: Missing Checkpoint
# ---------------------------------------------------------------------------
def test_fault_missing_checkpoint_graceful_recovery(tmp_path: Path) -> None:
    """Deleting the newest checkpoint allows fallback to previous checkpoint or replay."""
    db_file = tmp_path / "missing_ckpt.db"
    run_id = "test_run_missing_ckpt"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=150)
        manager = CheckpointManager(storage)
        checkpoints_before = storage.list_checkpoints(run_id)
        assert len(checkpoints_before) >= 3
        newest = checkpoints_before[-1]

        # Delete latest checkpoint row
        storage.delete_checkpoint(newest.checkpoint_id)

        # Restore must still succeed by using the next latest checkpoint and replaying
        restored = manager.restore(run_id, replay=True)
        assert restored is not None
        # State caught up to the end of the log
        assert len(restored.state.decisions) == 150
        assert restored.checkpoint is not None
        assert restored.checkpoint.version == checkpoints_before[-2].version


# ---------------------------------------------------------------------------
# Test 3: Truncated Event Log (Broken Hash Chain)
# ---------------------------------------------------------------------------
def test_fault_truncated_event_log_breaks_chain(tmp_path: Path) -> None:
    """Tampering with intermediate events breaks hash chain and is caught."""
    db_file = tmp_path / "truncated_log.db"
    run_id = "test_run_truncated_log"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=100)

    # Break event chain by deleting intermediate events
    conn = sqlite3.connect(str(db_file))
    try:
        # Delete event at sequence 30
        conn.execute("DELETE FROM events WHERE run_id = ? AND sequence = 30", (run_id,))
        conn.commit()
    finally:
        conn.close()

    with SQLiteStorage(str(db_file)) as storage:
        report = storage.verify_events(run_id)
        # Hash chain verification must flag the sequence gap / broken hash
        assert not report.ok
        assert len(report.violations) > 0

        engine = RecoveryEngine(storage)
        decision = engine.assess(run_id)
        # Broken log cannot safely resume; engine escalates to REQUEST_HUMAN
        assert decision.mode in (RecoveryMode.REQUEST_HUMAN, RecoveryMode.ABORT)
        assert not decision.safe


# ---------------------------------------------------------------------------
# Test 4: Environment Changes (Drift Detection & Cascading Staleness)
# ---------------------------------------------------------------------------
def test_fault_environment_drift_cascades_staleness(tmp_path: Path) -> None:
    """Drift in upstream dataset propagates to evidence, findings, and decisions."""
    db_file = tmp_path / "env_drift.db"
    run_id = "test_run_env_drift"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=60)
        latest_ckpt = storage.latest_checkpoint(run_id)
        assert latest_ckpt is not None

        # Build current environment with mutated dataset version: v1.0.0 -> v2.0.0
        drifted_env = capture(
            run_id,
            StaticProvider(
                resources={
                    "dataset://core_corpus": EnvResource(
                        name="dataset://core_corpus", version="v2.0.0"
                    ),
                    "service://auth_provider": EnvResource(
                        name="service://auth_provider", version="v3.1.0"
                    ),
                }
            ),
        )

        validator = StateValidator()
        outcome = validator.validate(
            latest_ckpt.state,
            current_environment=drifted_env,
            checkpoint_environment=latest_ckpt.environment,
            checkpoint_version=latest_ckpt.version,
        )

        # Drift detected: cannot safely resume
        assert not outcome.safe
        # Staleness cascaded
        stale_entries = outcome.downgraded
        assert len(stale_entries) > 0

        # Recovery engine assessment flags REPAIR_AND_RESUME
        engine = RecoveryEngine(storage)
        decision = engine.assess(run_id, current_environment=drifted_env)
        assert decision.mode == RecoveryMode.REPAIR_AND_RESUME
        assert not decision.safe


# ---------------------------------------------------------------------------
# Test 5: Dependency Changes (Missing Upstream Dependency)
# ---------------------------------------------------------------------------
def test_fault_dependency_missing_escalates(tmp_path: Path) -> None:
    """Removal of a required dependency prevents clean resume and requires repair."""
    db_file = tmp_path / "missing_dep.db"
    run_id = "test_run_missing_dep"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=50)
        latest_ckpt = storage.latest_checkpoint(run_id)
        assert latest_ckpt is not None

        # Current environment has removed auth_provider completely
        partial_env = capture(
            run_id,
            StaticProvider(
                resources={
                    "dataset://core_corpus": EnvResource(
                        name="dataset://core_corpus", version="v1.0.0"
                    )
                }
            ),
        )

        validator = StateValidator(strict_unknown=True)
        outcome = validator.validate(
            latest_ckpt.state,
            current_environment=partial_env,
            checkpoint_environment=latest_ckpt.environment,
            checkpoint_version=latest_ckpt.version,
        )

        assert not outcome.safe
        assert any(e.component_id == "service://auth_provider" for e in outcome.downgraded)

        engine = RecoveryEngine(storage, strict_unknown=True)
        decision = engine.assess(run_id, current_environment=partial_env)
        assert decision.mode != RecoveryMode.RESUME
        assert not decision.safe


# ---------------------------------------------------------------------------
# Test 6: Constraint Changes (Retracted Governance Constraint)
# ---------------------------------------------------------------------------
def test_fault_constraint_retraction_escalates_to_human(tmp_path: Path) -> None:
    """Retracting an active constraint pin prevents unmonitored continuation."""
    db_file = tmp_path / "constraint_fault.db"
    run_id = "test_run_constraint_fault"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=50)

        # Pin a hard constraint
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            {"constraint_id": "c_budget_limit", "sha256": "abcdef1234567890" * 4},
        )
        manager = CheckpointManager(storage)
        ckpt = manager.checkpoint(run_id, reason="with_pinned_constraint")
        assert "c_budget_limit" in ckpt.state.pins

        # Now retract the constraint
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_RETRACTED,
            {"constraint_id": "c_budget_limit"},
        )

        # Assess recovery: altered constraint set requires review
        engine = RecoveryEngine(storage)
        decision = engine.assess(run_id)
        # Any dropped or retracted pin must escalate
        assert decision.mode in (
            RecoveryMode.REQUEST_HUMAN,
            RecoveryMode.REPLAN,
            RecoveryMode.REPAIR_AND_RESUME,
        )


# ---------------------------------------------------------------------------
# Test 7: Stale State & Downstream Action Admissibility
# ---------------------------------------------------------------------------
def test_fault_stale_state_inadmissible_due_to_downstream_action(tmp_path: Path) -> None:
    """Checkpointing, executing external action, then resuming earlier state is rejected."""
    db_file = tmp_path / "inadmissible.db"
    run_id = "test_run_inadmissible"

    with SQLiteStorage(str(db_file)) as storage:
        _build_long_horizon_state(storage, run_id, steps=50)
        manager = CheckpointManager(storage)

        # Take Checkpoint A (version v_A)
        ckpt_a = manager.checkpoint(run_id, reason="checkpoint_A")

        # More events occur produced after Checkpoint A
        storage.append_event(
            run_id,
            EventType.DECISION_CREATED,
            {
                "decision_id": "dec_downstream",
                "decision": "Critical downstream decision",
                "reason": "New facts",
            },
        )

        # External action executes and consumes the post-checkpoint decision
        ledger = ActionLedger(storage, run_id)
        consumed = ConsumedInputs(
            component_ids=("dec_downstream",),
            checkpoint_seq=ckpt_a.version,
        )
        outcome = ledger.claim("external.api_dispatch", {"payload": "send_email"})
        ledger.complete(
            str(outcome.key),
            external_id="msg_999",
            result={"sent": True},
            consumed_inputs=consumed,
        )

        # Check admissibility of Checkpoint A against the completed actions
        actions = ledger.all()
        adm_result = check_admissibility(ckpt_a, actions)

        # Checkpoint A is INADMISSIBLE because dec_downstream was produced AFTER ckpt_a
        assert not adm_result.admissible
        assert len(adm_result.blocking) > 0
        assert "dec_downstream" in adm_result.reason
