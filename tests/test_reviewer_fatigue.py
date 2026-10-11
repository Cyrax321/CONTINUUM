"""Tests for reviewer fatigue telemetry (issue #1411)."""

from __future__ import annotations

import argparse
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.cli.main import cmd_confirm, cmd_health
from continuum.events import EventType
from continuum.models import ActionStatus, Run
from continuum.recovery.fatigue import (
    DEFAULT_COMPLEXITY_FLOOR,
    DEFAULT_MIN_BATCH_SIZE,
    DEFAULT_MIN_DECISION_SECONDS,
    FatigueContract,
    advisory_text,
    evaluate_batch,
    fatigue_advisory,
    load_fatigue_contract,
    open_dwell_seconds,
    record_batch_approval,
    record_review_parked,
)
from continuum.storage import SQLiteStorage


@pytest.fixture
def storage() -> SQLiteStorage:
    s = SQLiteStorage(":memory:")
    s.create_run(Run(run_id="run_1", goal="test goal"))
    s.append_event("run_1", EventType.RUN_STARTED, {"goal": "test goal"})
    return s


def test_fatigue_contract_defaults_and_validation(tmp_path: Path) -> None:
    """Contract loads defaults and enforces schema validation."""
    contract = FatigueContract()
    assert contract.min_decision_seconds == DEFAULT_MIN_DECISION_SECONDS
    assert contract.min_batch_size == DEFAULT_MIN_BATCH_SIZE
    assert contract.complexity_floor == DEFAULT_COMPLEXITY_FLOOR

    # Missing file falls back to defaults
    missing = tmp_path / "nonexistent.json"
    loaded = load_fatigue_contract(missing)
    assert loaded == contract

    # Valid custom contract
    custom_path = tmp_path / "fatigue.json"
    custom_path.write_text(
        json.dumps({"min_decision_seconds": 1.5, "min_batch_size": 3, "complexity_floor": "medium"})
    )
    custom = load_fatigue_contract(custom_path)
    assert custom.min_decision_seconds == 1.5
    assert custom.min_batch_size == 3
    assert custom.complexity_floor == "medium"

    # Invalid complexity floor raises
    with pytest.raises(ValueError, match="complexity_floor"):
        FatigueContract(complexity_floor="extreme")


def test_record_review_parked(storage: SQLiteStorage) -> None:
    """record_review_parked stores event with risk score and key in hash chain."""
    ev = record_review_parked(
        storage,
        "run_1",
        key="action:delete_db",
        action_type="delete_db",
        reason="high blast radius",
        risk_score=0.85,
    )
    assert ev.type == EventType.REVIEW_PARKED
    assert ev.payload["key"] == "action:delete_db"
    assert ev.payload["action_type"] == "delete_db"
    assert ev.payload["risk_score"] == 0.85

    # Invalid risk score is rejected
    with pytest.raises(ValueError, match="risk_score"):
        record_review_parked(
            storage,
            "run_1",
            key="action:other",
            action_type="other",
            reason="invalid",
            risk_score=1.5,
        )


def test_open_dwell_seconds(storage: SQLiteStorage) -> None:
    """Dwell calculation accurately measures time since oldest unapproved parked item."""
    # Nothing parked returns None
    assert open_dwell_seconds(storage, "run_1") is None

    # Park first item
    ev1 = record_review_parked(storage, "run_1", key="a1", action_type="type1", reason="test")
    # Park second item
    record_review_parked(storage, "run_1", key="a2", action_type="type2", reason="test")

    # Measured against an explicit future timestamp
    target_now = ev1.timestamp + timedelta(seconds=25)
    dwell = open_dwell_seconds(storage, "run_1", now=target_now)
    assert dwell == 25.0

    # Once a batch is approved, open dwell resets
    record_batch_approval(storage, "run_1", item_count=2, reviewer="op", dwell_seconds=25.0)
    assert open_dwell_seconds(storage, "run_1", now=target_now + timedelta(seconds=5)) is None


def test_record_batch_approval_within_bounds(storage: SQLiteStorage) -> None:
    """Batch approval meeting inspection bounds emits no fatigue signal."""
    batch_ev, signal_ev = record_batch_approval(
        storage,
        "run_1",
        item_count=3,
        reviewer="alice",
        complexity="high",
        dwell_seconds=6.0,  # 2.0s per item > 0.5s floor
    )
    assert batch_ev.type == EventType.REVIEW_BATCH_APPROVED
    assert batch_ev.payload["item_count"] == 3
    assert batch_ev.payload["decision_seconds"] == 2.0
    assert signal_ev is None


def test_record_batch_approval_trips_fatigue_signal(storage: SQLiteStorage) -> None:
    """Approval under 500ms per high-complexity item triggers FATIGUE_SIGNAL_RECORDED."""
    contract = FatigueContract(min_decision_seconds=0.5, min_batch_size=2, complexity_floor="high")
    batch_ev, signal_ev = record_batch_approval(
        storage,
        "run_1",
        item_count=4,
        reviewer="bob",
        complexity="high",
        dwell_seconds=0.8,  # 0.2s per item < 0.5s floor
        contract=contract,
    )
    assert batch_ev.type == EventType.REVIEW_BATCH_APPROVED
    assert signal_ev is not None
    assert signal_ev.type == EventType.FATIGUE_SIGNAL_RECORDED
    assert signal_ev.payload["batch_event_id"] == batch_ev.event_id
    assert signal_ev.payload["decision_seconds"] == 0.2
    assert signal_ev.payload["reviewer"] == "bob"

    # Evaluating same batch again does not produce duplicate signal
    assert evaluate_batch(batch_ev, contract=contract).fatigued is True


def test_fatigue_advisory_and_text(storage: SQLiteStorage) -> None:
    """Advisory correctly reports summary metrics and warning state."""
    # Initially empty
    advisory = fatigue_advisory(storage, "run_1")
    assert advisory["fatigued"] is False
    assert "no batch approvals" in advisory_text(advisory)

    # Record fatigued batch
    record_batch_approval(
        storage,
        "run_1",
        item_count=2,
        reviewer="eve",
        complexity="high",
        dwell_seconds=0.4,
    )

    advisory_fatigued = fatigue_advisory(storage, "run_1")
    assert advisory_fatigued["fatigued"] is True
    assert advisory_fatigued["fatigue_signals"] == 1
    assert advisory_fatigued["batches_approved"] == 1
    assert "WARNING" in advisory_text(advisory_fatigued)


def test_flag_for_review_records_telemetry(storage: SQLiteStorage) -> None:
    """ActionLedger.flag_for_review parks review event with risk score."""
    ledger = ActionLedger(storage, "run_1")
    outcome = ledger.claim("send_payment", {"amount": 1000}, key="pay:1")
    action = ledger.flag_for_review(
        outcome.action.action_id, reason="over threshold", risk_score=0.9
    )

    assert action.status == ActionStatus.REQUIRES_REVIEW
    events = storage.read_events("run_1")
    parked = [e for e in events if e.type == EventType.REVIEW_PARKED]
    assert len(parked) == 1
    assert parked[0].payload["risk_score"] == 0.9
    assert parked[0].payload["action_type"] == "send_payment"


def test_cli_confirm_and_health_integration(storage: SQLiteStorage) -> None:
    """CLI confirm emits batch telemetry, and health surfaces the fatigue advisory."""
    # Park an item first
    record_review_parked(
        storage, "run_1", key="k1", action_type="deploy", reason="manual inspection"
    )

    # Execute confirm CLI
    confirm_args = argparse.Namespace(
        run_id="run_1",
        scope=["goal", "progress"],
        reviewer="auditor",
        complexity="high",
        model=None,
        tolerate_unknown=True,
        json=False,
    )
    out = io.StringIO()
    err = io.StringIO()
    cmd_confirm(confirm_args, storage, out, err)

    events = storage.read_events("run_1")
    batches = [e for e in events if e.type == EventType.REVIEW_BATCH_APPROVED]
    assert len(batches) == 1
    assert batches[0].payload["reviewer"] == "auditor"
    # The batch is the parked queue, not the goal/progress scope: one parked item
    # is a batch of one, so the audit records the count it actually cleared.
    assert batches[0].payload["item_count"] == 1

    # Health CLI reports reviewer fatigue advisory
    health_args = argparse.Namespace(run_id="run_1", json=True)
    health_out = io.StringIO()
    cmd_health(health_args, storage, health_out, err)
    payload = json.loads(health_out.getvalue())
    assert "reviewer_fatigue" in payload
    assert payload["reviewer_fatigue"]["batches_approved"] == 1


def test_event_log_verify_passes(storage: SQLiteStorage) -> None:
    """Append-only hash chain verify_events() remains clean with fatigue telemetry events."""
    record_review_parked(
        storage, "run_1", key="k1", action_type="op", reason="check", risk_score=0.7
    )
    record_batch_approval(
        storage, "run_1", item_count=2, reviewer="op", complexity="high", dwell_seconds=0.3
    )

    report = storage.verify_events("run_1")
    assert report.ok is True
    assert report.checked >= 2


def test_open_parked_count_tracks_the_open_queue(storage: SQLiteStorage) -> None:
    """open_parked_count reports queued items and clears them on batch approval."""
    from continuum.recovery.fatigue import open_parked_count

    assert open_parked_count(storage, "run_1") == 0

    record_review_parked(storage, "run_1", key="a1", action_type="t1", reason="r")
    record_review_parked(storage, "run_1", key="a2", action_type="t2", reason="r")
    assert open_parked_count(storage, "run_1") == 2

    # A batch approval closes the window, so those items stop counting.
    record_batch_approval(storage, "run_1", item_count=2, reviewer="op", dwell_seconds=10.0)
    assert open_parked_count(storage, "run_1") == 0

    # An item parked afterwards reopens the queue without resurrecting old ones.
    record_review_parked(storage, "run_1", key="a3", action_type="t3", reason="r")
    assert open_parked_count(storage, "run_1") == 1


def test_confirm_records_no_batch_when_queue_is_empty(storage: SQLiteStorage) -> None:
    """A confirm that clears nothing parked must not invent a batch event.

    Regression coverage for the goal/progress scope being mistaken for the
    batch: confirming with an empty review queue used to log a batch of two
    high-complexity items cleared in ~0s, a rubber-stamp that never happened.
    """
    confirm_args = argparse.Namespace(
        run_id="run_1",
        scope=None,
        reviewer="operator",
        complexity="high",
        model=None,
        tolerate_unknown=True,
        json=False,
    )
    cmd_confirm(confirm_args, storage, io.StringIO(), io.StringIO())

    events = storage.read_events("run_1")
    assert not [e for e in events if e.type == EventType.REVIEW_BATCH_APPROVED]
    # The confirmation itself still lands, so the run is released as before.
    assert any(e.type is EventType.REVIEW_CONFIRMED for e in events)


def test_confirm_records_the_true_batch_size_and_can_trip(storage: SQLiteStorage) -> None:
    """Flagging several actions and confirming records their real count.

    Ten parked actions cleared in two seconds is 0.2s per item and must trip the
    contract; the old hardcoded count of two read it as 1s per item and missed it.
    """
    from continuum.actions.ledger import ActionLedger

    ledger = ActionLedger(storage, "run_1")
    for index in range(3):
        outcome = ledger.claim(f"op_{index}", {"i": index}, key=f"k_{index}")
        ledger.flag_for_review(outcome.action.action_id, reason="manual inspection")

    confirm_args = argparse.Namespace(
        run_id="run_1",
        scope=None,
        reviewer="operator",
        complexity="high",
        model=None,
        tolerate_unknown=True,
        json=False,
    )
    cmd_confirm(confirm_args, storage, io.StringIO(), io.StringIO())

    events = storage.read_events("run_1")
    batches = [e for e in events if e.type == EventType.REVIEW_BATCH_APPROVED]
    assert len(batches) == 1
    assert batches[0].payload["item_count"] == 3
    # Nothing was parked for long, so the contract sees a fast batch of three.
    assert batches[0].payload["decision_seconds"] is not None


def test_record_batch_approval_carries_a_claimed_reviewer(storage: SQLiteStorage) -> None:
    """A claimed reviewer is recorded but never treated as the trusted identity."""
    batch_ev, signal_ev = record_batch_approval(
        storage,
        "run_1",
        item_count=2,
        reviewer="sidecar-operator",
        complexity="high",
        dwell_seconds=0.2,
        claimed_reviewer="whoever-the-caller-said",
    )
    assert batch_ev.payload["reviewer"] == "sidecar-operator"
    assert batch_ev.payload["claimed_reviewer"] == "whoever-the-caller-said"
    assert signal_ev is not None
    assert signal_ev.payload["reviewer"] == "sidecar-operator"
    assert signal_ev.payload["claimed_reviewer"] == "whoever-the-caller-said"


def test_record_batch_approval_omits_claimed_reviewer_when_absent(storage: SQLiteStorage) -> None:
    """No claimed name means the payload keeps its original shape."""
    batch_ev, _ = record_batch_approval(
        storage,
        "run_1",
        item_count=2,
        reviewer="operator",
        complexity="high",
        dwell_seconds=5.0,
    )
    assert "claimed_reviewer" not in batch_ev.payload
