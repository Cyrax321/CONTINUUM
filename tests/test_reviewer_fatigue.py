"""Tests for reviewer fatigue telemetry, audit facts, and oversight bounds (issue #1411)."""

from __future__ import annotations

import argparse
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from continuum.actions import ActionLedger, LedgerError
from continuum.events import Event, EventType
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
    open_parked_count,
    record_batch_approval,
    record_review_parked,
)
from continuum.storage.sqlite import SQLiteStorage


@pytest.fixture
def storage(tmp_path: Path) -> SQLiteStorage:
    db = SQLiteStorage(str(tmp_path / "fatigue_test.db"))
    db.create_run(Run(run_id="run_1", goal="verify fatigue telemetry"))
    db.append_event("run_1", EventType.RUN_STARTED, {"goal": "verify fatigue telemetry"})
    return db


def test_contract_defaults_and_validation() -> None:
    contract = FatigueContract()
    assert contract.min_decision_seconds == DEFAULT_MIN_DECISION_SECONDS
    assert contract.min_batch_size == DEFAULT_MIN_BATCH_SIZE
    assert contract.complexity_floor == DEFAULT_COMPLEXITY_FLOOR

    with pytest.raises(ValueError, match="complexity_floor"):
        FatigueContract(complexity_floor="extreme")


def test_contract_trips_logic() -> None:
    contract = FatigueContract(
        min_decision_seconds=0.5,
        min_batch_size=2,
        complexity_floor="high",
    )

    # Below min batch size -> never trips
    assert not contract.trips(item_count=1, complexity="high", decision_seconds=0.1)

    # Complexity below floor -> never trips (false positive guard)
    assert not contract.trips(item_count=5, complexity="low", decision_seconds=0.1)
    assert not contract.trips(item_count=5, complexity="medium", decision_seconds=0.1)
    assert not contract.trips(item_count=5, complexity="unknown_label", decision_seconds=0.1)

    # Slow decision -> within bound
    assert not contract.trips(item_count=3, complexity="high", decision_seconds=1.0)

    # High complexity, large batch, fast decision -> trips
    assert contract.trips(item_count=3, complexity="high", decision_seconds=0.2)


def test_evaluate_batch_reason_interpolates_complexity() -> None:
    contract = FatigueContract(
        min_batch_size=2, complexity_floor="medium", min_decision_seconds=0.5
    )
    ev = Event(
        run_id="run_1",
        sequence=1,
        type=EventType.REVIEW_BATCH_APPROVED,
        payload={
            "item_count": 3,
            "complexity": "medium",
            "dwell_seconds": 0.6,
            "decision_seconds": 0.2,
        },
    ).sealed()
    reading = evaluate_batch(ev, contract=contract)
    assert reading is not None
    assert reading.fatigued is True
    assert "medium-complexity" in reading.reason

    # Non-fatigued reason
    slow_ev = Event(
        run_id="run_1",
        sequence=2,
        type=EventType.REVIEW_BATCH_APPROVED,
        payload={
            "item_count": 3,
            "complexity": "medium",
            "dwell_seconds": 3.0,
            "decision_seconds": 1.0,
        },
    ).sealed()
    slow_reading = evaluate_batch(slow_ev, contract=contract)
    assert slow_reading is not None
    assert slow_reading.fatigued is False
    assert "within the 0.5s bound" in slow_reading.reason

    # Non-batch event returns None
    other_ev = Event(
        run_id="run_1",
        sequence=3,
        type=EventType.REVIEW_PARKED,
        payload={},
    ).sealed()
    assert evaluate_batch(other_ev, contract=contract) is None

    # Unmeasured dwell returns None
    no_dwell_ev = Event(
        run_id="run_1",
        sequence=4,
        type=EventType.REVIEW_BATCH_APPROVED,
        payload={"item_count": 2},
    ).sealed()
    assert evaluate_batch(no_dwell_ev, contract=contract) is None


def test_load_fatigue_contract(tmp_path: Path) -> None:
    # Missing path loads defaults
    missing = tmp_path / "absent.json"
    assert load_fatigue_contract(missing) == FatigueContract()

    # Valid config
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {"min_decision_seconds": 0.8, "min_batch_size": 3, "complexity_floor": "medium"}
        ),
        encoding="utf-8",
    )
    loaded = load_fatigue_contract(valid)
    assert loaded.min_decision_seconds == 0.8
    assert loaded.min_batch_size == 3
    assert loaded.complexity_floor == "medium"

    # Malformed JSON
    bad_syntax = tmp_path / "bad.json"
    bad_syntax.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid fatigue contract"):
        load_fatigue_contract(bad_syntax)

    # Non-dict JSON
    non_dict = tmp_path / "arr.json"
    non_dict.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_fatigue_contract(non_dict)


def test_record_review_parked_validation_and_writing(storage: SQLiteStorage) -> None:
    # Valid without risk score
    ev1 = record_review_parked(storage, "run_1", key="k1", action_type="deploy", reason="manual")
    assert ev1.type is EventType.REVIEW_PARKED
    assert ev1.payload["key"] == "k1"
    assert "risk_score" not in ev1.payload

    # Valid with risk score
    ev2 = record_review_parked(
        storage, "run_1", key="k2", action_type="exec", reason="critical", risk_score=0.85
    )
    assert ev2.payload["risk_score"] == 0.85

    # Invalid risk scores
    with pytest.raises(ValueError, match="risk_score"):
        record_review_parked(
            storage, "run_1", key="k3", action_type="exec", reason="r", risk_score=-0.1
        )
    with pytest.raises(ValueError, match="risk_score"):
        record_review_parked(
            storage, "run_1", key="k3", action_type="exec", reason="r", risk_score=1.1
        )
    with pytest.raises(ValueError, match="risk_score"):
        record_review_parked(
            storage, "run_1", key="k3", action_type="exec", reason="r", risk_score="high"
        )  # type: ignore[arg-type]


def test_flag_for_review_idempotency(storage: SQLiteStorage) -> None:
    """ActionLedger.flag_for_review is idempotent and does not duplicate parked rows."""
    ledger = ActionLedger(storage, "run_1")
    outcome = ledger.claim("transfer", {"amt": 500}, key="tx:1")
    key = outcome.action.action_id

    # First flag
    a1 = ledger.flag_for_review(key, reason="large transfer", risk_score=0.9)
    assert a1.status == ActionStatus.REQUIRES_REVIEW
    events1 = [e for e in storage.read_all_events("run_1") if e.type is EventType.REVIEW_PARKED]
    assert len(events1) == 1
    assert open_parked_count(storage, "run_1") == 1

    # Second flag on same action must be a no-op
    a2 = ledger.flag_for_review(key, reason="repeat check", risk_score=0.9)
    assert a2.status == ActionStatus.REQUIRES_REVIEW
    events2 = [e for e in storage.read_all_events("run_1") if e.type is EventType.REVIEW_PARKED]
    assert len(events2) == 1
    assert open_parked_count(storage, "run_1") == 1


def test_open_queue_tracks_action_keys_and_drops_on_reconcile_or_compensate(
    storage: SQLiteStorage,
) -> None:
    """Settling an action via reconcile or compensate removes it from the open queue."""
    ledger = ActionLedger(storage, "run_1")

    # Claim two actions
    o1 = ledger.claim("act1", {}, key="k1")
    o2 = ledger.claim("act2", {}, key="k2")

    ledger.flag_for_review(o1.action.action_id, reason="check 1")
    ledger.flag_for_review(o2.action.action_id, reason="check 2")
    assert open_parked_count(storage, "run_1") == 2

    # Reconciling k1 removes it from open queue
    ledger.reconcile(o1.action.action_id, occurred=True, note="verified")
    assert open_parked_count(storage, "run_1") == 1

    # Compensating k1 (already completed) does not error
    # Reconciling k2 as not occurred also removes it
    ledger.reconcile(o2.action.action_id, occurred=False, note="not found")
    assert open_parked_count(storage, "run_1") == 0


def test_open_dwell_seconds_with_naive_and_aware_timestamps(storage: SQLiteStorage) -> None:
    assert open_dwell_seconds(storage, "run_1") is None

    # Park an item
    record_review_parked(storage, "run_1", key="k1", action_type="a1", reason="r")

    # Aware now
    now_aware = datetime.now(UTC)
    dwell = open_dwell_seconds(storage, "run_1", now=now_aware)
    assert dwell is not None
    assert dwell >= 0.0

    # Naive now
    now_naive = datetime.now().replace(tzinfo=None)
    dwell_naive = open_dwell_seconds(storage, "run_1", now=now_naive)
    assert dwell_naive is not None
    assert dwell_naive >= 0.0


def test_record_batch_approval_validation_and_claimed_reviewer(storage: SQLiteStorage) -> None:
    with pytest.raises(ValueError, match="item_count must be at least 1"):
        record_batch_approval(storage, "run_1", item_count=0, reviewer="admin")

    with pytest.raises(ValueError, match="reviewer must be a non-empty string"):
        record_batch_approval(storage, "run_1", item_count=2, reviewer="")

    # Claimed reviewer is recorded separately
    batch, signal = record_batch_approval(
        storage,
        "run_1",
        item_count=2,
        reviewer="trusted-operator",
        claimed_reviewer="untrusted-client",
        complexity="high",
        dwell_seconds=0.2,
    )
    assert batch.payload["reviewer"] == "trusted-operator"
    assert batch.payload["claimed_reviewer"] == "untrusted-client"
    assert signal is not None
    assert signal.payload["reviewer"] == "trusted-operator"
    assert signal.payload["claimed_reviewer"] == "untrusted-client"

    # When dwell_seconds is None and no items are open, dwell and decision_seconds are None
    storage.create_run(Run(run_id="run_empty", goal="empty run"))
    batch_unmeasured, sig_none = record_batch_approval(
        storage,
        "run_empty",
        item_count=2,
        reviewer="trusted-operator",
    )
    assert batch_unmeasured.payload["dwell_seconds"] is None
    assert batch_unmeasured.payload["decision_seconds"] is None
    assert sig_none is None


def test_record_batch_approval_malformed_contract_fallback(
    storage: SQLiteStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed contract file falls back to defaults with contract_error in payload."""
    broken_cfg = tmp_path / "fatigue.json"
    broken_cfg.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr("continuum.recovery.fatigue.DEFAULT_FATIGUE_PATH", broken_cfg)

    batch, signal = record_batch_approval(
        storage,
        "run_1",
        item_count=3,
        reviewer="op",
        complexity="high",
        dwell_seconds=0.3,
    )
    assert batch.type is EventType.REVIEW_BATCH_APPROVED
    assert "contract_error" in batch.payload
    # Still evaluates and trips on fallback default
    assert signal is not None
    assert signal.type is EventType.FATIGUE_SIGNAL_RECORDED


def test_re_evaluating_existing_batch_is_idempotent(storage: SQLiteStorage) -> None:
    """Re-evaluating an existing batch with record_batch_approval avoids duplicate signals."""
    batch, sig1 = record_batch_approval(
        storage,
        "run_1",
        item_count=2,
        reviewer="op",
        complexity="high",
        dwell_seconds=0.2,
    )
    assert sig1 is not None

    # Re-evaluating the same batch returns (batch, None) because _already_signalled is True
    re_batch, sig2 = record_batch_approval(
        storage,
        "run_1",
        item_count=2,
        reviewer="op",
        batch_event=batch,
    )
    assert re_batch.event_id == batch.event_id
    assert sig2 is None


def test_fatigue_advisory_and_advisory_text(storage: SQLiteStorage) -> None:
    # Initially no batch approvals
    adv0 = fatigue_advisory(storage, "run_1")
    assert adv0["status"] == "ok"
    assert adv0["batches_approved"] == 0
    assert "no batch approvals recorded yet" in advisory_text(adv0)

    # Clean approvals
    record_batch_approval(
        storage, "run_1", item_count=2, reviewer="op", complexity="high", dwell_seconds=5.0
    )
    adv_ok = fatigue_advisory(storage, "run_1")
    assert adv_ok["fatigued"] is False
    assert "ok, 1 batch approval(s)" in advisory_text(adv_ok)

    # Fatigued approval
    record_batch_approval(
        storage, "run_1", item_count=3, reviewer="op", complexity="high", dwell_seconds=0.2
    )
    adv_warn = fatigue_advisory(storage, "run_1")
    assert adv_warn["fatigued"] is True
    assert "WARNING, 1 signal(s) recorded" in advisory_text(adv_warn)


def test_fatigue_advisory_storage_failure_degradation() -> None:
    """Storage read failure degrades to explicit unavailable status."""
    failing_storage = MagicMock()
    failing_storage.read_all_events.side_effect = RuntimeError("disk unreadable")

    adv = fatigue_advisory(failing_storage, "run_1")
    assert adv["status"] == "unavailable"
    assert "disk unreadable" in adv["error"]
    assert "Reviewer fatigue: unavailable (disk unreadable)." in advisory_text(adv)


def test_action_ledger_reconcile_and_reconcile_batch(storage: SQLiteStorage) -> None:
    ledger = ActionLedger(storage, "run_1")

    # Park 3 actions
    outcomes = [ledger.claim(f"task_{i}", {"i": i}, key=f"t_{i}") for i in range(3)]
    for o in outcomes:
        ledger.flag_for_review(o.action.action_id, reason="needs review")

    assert open_parked_count(storage, "run_1") == 3

    # Reconcile single action
    ledger.reconcile(
        outcomes[0].action.action_id, occurred=True, note="cleared 1", reviewer="operator"
    )
    events = storage.read_all_events("run_1")
    batches = [e for e in events if e.type is EventType.REVIEW_BATCH_APPROVED]
    assert len(batches) == 1
    assert batches[0].payload["item_count"] == 1
    assert open_parked_count(storage, "run_1") == 2

    # Reconcile batch for remaining 2 actions
    batch_res = ledger.reconcile_batch(
        [
            (outcomes[1].action.action_id, True),
            {"key": outcomes[2].action.action_id, "occurred": False, "note": "rejected"},
        ],
        reviewer="lead-reviewer",
        complexity="high",
    )
    assert len(batch_res) == 2
    assert open_parked_count(storage, "run_1") == 0

    all_batches = [
        e for e in storage.read_all_events("run_1") if e.type is EventType.REVIEW_BATCH_APPROVED
    ]
    assert len(all_batches) == 2
    assert all_batches[0].payload["dwell_seconds"] is not None
    assert all_batches[0].payload["dwell_seconds"] >= 0.0
    assert all_batches[1].payload["item_count"] == 2
    assert all_batches[1].payload["reviewer"] == "lead-reviewer"
    assert all_batches[1].payload["dwell_seconds"] is not None
    assert all_batches[1].payload["dwell_seconds"] >= 0.0


def test_reconcile_batch_fast_approval_emits_fatigue_signal(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rapid reconcile_batch of parked items properly captures dwell and emits a fatigue signal."""
    contract = FatigueContract(min_batch_size=2, min_decision_seconds=10.0, complexity_floor="high")
    monkeypatch.setattr("continuum.recovery.fatigue.load_fatigue_contract", lambda: contract)

    ledger = ActionLedger(storage, "run_1")
    o1 = ledger.claim("task_1", {}, key="k1")
    o2 = ledger.claim("task_2", {}, key="k2")
    o3 = ledger.claim("task_3", {}, key="k3")
    ledger.flag_for_review(o1.action.action_id, reason="needs review 1")
    ledger.flag_for_review(o2.action.action_id, reason="needs review 2")
    ledger.flag_for_review(o3.action.action_id, reason="needs review 3")
    assert open_parked_count(storage, "run_1") == 3

    # Fast batch approval of o1 and o2 under the 10.0s bound
    res = ledger.reconcile_batch(
        [(o1.key, True), (o2.key, True)],
        reviewer="lead-reviewer",
        complexity="high",
    )
    assert len(res) == 2
    # o3 is still open and was not cleared
    assert open_parked_count(storage, "run_1") == 1

    events = storage.read_all_events("run_1")
    batch_events = [e for e in events if e.type is EventType.REVIEW_BATCH_APPROVED]
    assert len(batch_events) == 1
    batch_ev = batch_events[0]
    assert batch_ev.payload["item_count"] == 2
    assert batch_ev.payload["action_keys"] == [o1.key, o2.key]
    assert batch_ev.payload["dwell_seconds"] is not None
    assert batch_ev.payload["decision_seconds"] is not None

    signals = [e for e in events if e.type is EventType.FATIGUE_SIGNAL_RECORDED]
    assert len(signals) == 1
    signal_ev = signals[0]
    assert signal_ev.payload["batch_event_id"] == batch_ev.event_id
    assert signal_ev.payload["item_count"] == 2
    assert signal_ev.payload["complexity"] == "high"
    assert signal_ev.payload["reviewer"] == "lead-reviewer"
    assert "under the 10.0s inspection bound" in signal_ev.payload["reason"]


def test_dashboard_and_sidecar_reconcile(storage: SQLiteStorage) -> None:
    from continuum.dashboard.hitl import reconcile_action as dashboard_reconcile
    from continuum.serve.server import SidecarServer

    ledger = ActionLedger(storage, "run_1")
    o1 = ledger.claim("dash_op", {}, key="d1")
    o2 = ledger.claim("side_op", {}, key="s1")
    ledger.flag_for_review(o1.action.action_id, reason="flag 1")
    ledger.flag_for_review(o2.action.action_id, reason="flag 2")

    # Dashboard reconcile
    dashboard_reconcile(storage, "run_1", o1.key, occurred=True)
    a1 = ledger.get(o1.key)
    assert a1 is not None
    assert a1.status == ActionStatus.COMPLETED

    # Sidecar reconcile
    server = SidecarServer(storage=storage)
    from continuum.serve.server import _h_reconcile_action

    _h_reconcile_action(
        server,
        {
            "run_id": "run_1",
            "action_key": o2.key,
            "occurred": True,
            "complexity": "high",
        },
    )
    a2 = ledger.get(o2.key)
    assert a2 is not None
    assert a2.status == ActionStatus.COMPLETED


def test_cli_health_command_with_fatigue_and_degradation(storage: SQLiteStorage) -> None:
    from continuum.cli.main import cmd_health

    # Record parked and batch
    record_review_parked(storage, "run_1", key="k1", action_type="deploy", reason="check")
    record_batch_approval(
        storage, "run_1", item_count=1, reviewer="op", complexity="high", dwell_seconds=5.0
    )

    out = io.StringIO()
    err = io.StringIO()
    args = argparse.Namespace(run_id="run_1", json=False)
    cmd_health(args, storage, out, err)
    output = out.getvalue()
    assert "Reviewer fatigue: ok" in output

    # Health command with failing storage
    failing = MagicMock()
    failing.read_events.return_value = []
    failing.get_run.return_value = MagicMock()
    failing.read_all_events.side_effect = RuntimeError("io failure")

    out2 = io.StringIO()
    cmd_health(args, failing, out2, err)
    assert "Reviewer fatigue: unavailable" in out2.getvalue()


def test_storage_failure_during_flag_and_reconcile_does_not_strand_action(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If telemetry logging fails, flag_for_review and reconcile still complete."""
    ledger = ActionLedger(storage, "run_1")
    outcome = ledger.claim("transfer", {}, key="safe:1")

    # Mock record_review_parked to raise
    def mock_park(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("telemetry write failed")

    monkeypatch.setattr("continuum.recovery.fatigue.record_review_parked", mock_park)
    action = ledger.flag_for_review(outcome.action.action_id, reason="needs review")
    assert action.status == ActionStatus.REQUIRES_REVIEW

    # Mock record_batch_approval to raise
    def mock_batch(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("batch write failed")

    monkeypatch.setattr("continuum.recovery.fatigue.record_batch_approval", mock_batch)
    reconciled = ledger.reconcile(outcome.action.action_id, occurred=True, reviewer="operator")
    assert reconciled.status == ActionStatus.COMPLETED


def test_fatigue_edge_branches_and_coverage(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from continuum.recovery.fatigue import _open_parked_events, _rank

    # _rank with non-string
    assert _rank(123) == -1
    assert _rank(None) == -1

    # record_batch_approval with explicit contract
    custom_contract = FatigueContract(min_batch_size=2, min_decision_seconds=0.1)
    b, s = record_batch_approval(
        storage, "run_1", item_count=2, reviewer="op", contract=custom_contract, dwell_seconds=1.0
    )
    assert b is not None

    # fatigue_advisory when load_fatigue_contract fails
    def broken_load():
        raise RuntimeError("broken config")

    monkeypatch.setattr("continuum.recovery.fatigue.load_fatigue_contract", broken_load)
    adv = fatigue_advisory(storage, "run_1")
    assert adv["status"] == "ok"

    # advisory_text when fatigued but median_seconds is None
    warn_unmeasured = {
        "status": "ok",
        "fatigued": True,
        "batches_approved": 1,
        "fatigue_signals": 1,
        "median_decision_seconds": None,
        "threshold_seconds": 0.5,
    }
    rendered = advisory_text(warn_unmeasured)
    assert "unmeasured" in rendered
    assert "WARNING" in rendered

    # _open_parked_events handling batch approval without action_keys
    ev_park1 = Event(
        run_id="run_1", sequence=1, type=EventType.REVIEW_PARKED, payload={"key": "k1"}
    ).sealed()
    ev_park_dup = Event(
        run_id="run_1", sequence=2, type=EventType.REVIEW_PARKED, payload={"key": "k1"}
    ).sealed()
    ev_reconcile_unknown = Event(
        run_id="run_1", sequence=3, type=EventType.ACTION_RECONCILED, payload={"key": "unknown"}
    ).sealed()
    ev_batch_clear = Event(
        run_id="run_1", sequence=4, type=EventType.REVIEW_BATCH_APPROVED, payload={"item_count": 1}
    ).sealed()

    events = [ev_park1, ev_park_dup, ev_reconcile_unknown, ev_batch_clear]
    assert _open_parked_events(events) == []

    # oldest event with naive timestamp
    naive_parked = Event(
        run_id="run_1",
        sequence=5,
        type=EventType.REVIEW_PARKED,
        timestamp=datetime.now().replace(tzinfo=None),
        payload={"key": "k_naive"},
    ).sealed()
    mock_st = MagicMock()
    mock_st.read_all_events.return_value = [naive_parked]
    dwell = open_dwell_seconds(mock_st, "run_1")
    assert dwell is not None
    assert dwell >= 0.0


def test_action_ledger_reconcile_without_reviewer_omits_telemetry(storage: SQLiteStorage) -> None:
    """Automated settlements (reviewer=None) do not emit review approval telemetry."""
    ledger = ActionLedger(storage, "run_1")
    o = ledger.claim("task_auto", {}, key="k_auto")
    ledger.flag_for_review(o.action.action_id, reason="needs review")
    assert open_parked_count(storage, "run_1") == 1

    # Automated reconciliation without reviewer
    ledger.reconcile(o.action.action_id, occurred=True, note="automated probe")
    assert open_parked_count(storage, "run_1") == 0

    events = storage.read_all_events("run_1")
    batches = [e for e in events if e.type is EventType.REVIEW_BATCH_APPROVED]
    assert len(batches) == 0


def test_action_ledger_reconcile_batch_mid_batch_failure_preserves_telemetry(
    storage: SQLiteStorage,
) -> None:
    """If reconcile_batch fails mid-batch, telemetry for earlier settled items is preserved."""
    ledger = ActionLedger(storage, "run_1")
    o1 = ledger.claim("task_batch_1", {}, key="kb_1")
    o2 = ledger.claim("task_batch_2", {}, key="kb_2")
    ledger.flag_for_review(o1.action.action_id, reason="needs review 1")
    ledger.flag_for_review(o2.action.action_id, reason="needs review 2")
    assert open_parked_count(storage, "run_1") == 2

    with pytest.raises(LedgerError):
        ledger.reconcile_batch(
            [
                (o1.action.action_id, True),
                ("non_existent_key_raises_error", True),
                (o2.action.action_id, True),
            ],
            reviewer="lead-reviewer",
            complexity="high",
        )

    # First item was settled and recorded approval telemetry
    assert open_parked_count(storage, "run_1") == 1
    events = storage.read_all_events("run_1")
    batches = [e for e in events if e.type is EventType.REVIEW_BATCH_APPROVED]
    assert len(batches) == 1
    assert batches[0].payload["item_count"] == 1
    assert batches[0].payload["action_keys"] == [o1.key]
    assert batches[0].payload["reviewer"] == "lead-reviewer"
    assert batches[0].payload["dwell_seconds"] is not None
