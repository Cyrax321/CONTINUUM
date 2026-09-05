"""RISK_OBSERVED ingestion and risk-policy mapping (issue #563)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.recovery.risk import (
    evaluate_risk,
    ingest_risk,
    ingest_risk_json_line,
    is_more_conservative,
    load_risk_policy,
)
from continuum.storage import SQLiteStorage


def test_ingest_risk_records_event(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_ingest.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_ingest"
        store.create_run_started(Run(run_id=run_id, goal="test risk"))
        ok = ingest_risk(store, run_id, {"trigger": "loop", "score": 0.8, "detail": "repetition"})
        assert ok is True
        events = store.read_events(run_id)
        assert events[-1].type == EventType.RISK_OBSERVED
        assert events[-1].source == Origin.EXTERNAL_MONITOR
        assert events[-1].payload["trigger"] == "loop"
        assert events[-1].payload["score"] == 0.8


def test_ingest_risk_fail_open_on_garbage(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_failopen.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_failopen"
        store.create_run_started(Run(run_id=run_id, goal="failopen"))
        # Missing trigger
        assert ingest_risk(store, run_id, {"score": 1}) is False
        # Empty trigger
        assert ingest_risk(store, run_id, {"trigger": ""}) is False
        # Malformed JSON line
        assert ingest_risk_json_line(store, run_id, "not json") is False
        assert ingest_risk_json_line(store, run_id, "") is False
        # No event should have been added beyond RUN_STARTED
        assert len(store.read_events(run_id)) == 1


def test_policy_defaults_and_conservative(tmp_path: Path) -> None:
    # Defaults are loaded when file missing
    missing = tmp_path / "no_policy.json"
    policy = load_risk_policy(missing)
    assert policy["loop"] == "replan"
    assert policy["error_cascade"] == "wait"
    assert policy["meltdown"] == "rollback"
    # Conservative check
    assert (
        is_more_conservative("wait", "replan") is True
    )  # WAIT more severe than REPLAN? Check severity ordering
    # Actually SEVERITY: REPLAN=2, WAIT=3, so WAIT > REPLAN, so more conservative
    # Downgrade should be refused
    assert is_more_conservative("replan", "wait") is False
    assert is_more_conservative("annotate", "wait") is False
    assert is_more_conservative("wait", "annotate") is True


def test_policy_file_validation(tmp_path: Path) -> None:
    # Valid file
    p = tmp_path / "risk-policy.json"
    p.write_text(json.dumps({"loop": "replan", "meltdown": "rollback"}), encoding="utf-8")
    policy = load_risk_policy(p)
    assert policy["loop"] == "replan"
    # Invalid mode
    p.write_text(json.dumps({"loop": "invalid_mode"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be one of"):
        load_risk_policy(p)
    # Downgrade attempt: loop defaults to replan, downgrade to annotate should fail
    p.write_text(json.dumps({"loop": "annotate"}), encoding="utf-8")
    with pytest.raises(ValueError, match="downgrades"):
        load_risk_policy(p)
    # Upgrade is allowed: loop replan -> wait is more severe? Check
    # REPLAN=2, WAIT=3, so wait is more severe, should be allowed
    p.write_text(json.dumps({"loop": "wait"}), encoding="utf-8")
    policy2 = load_risk_policy(p)
    assert policy2["loop"] == "wait"


def test_evaluate_risk_mapping(tmp_path: Path) -> None:
    policy = load_risk_policy(tmp_path / "missing.json")
    assert evaluate_risk("loop", policy) == "replan"
    assert evaluate_risk("error_cascade", policy) == "wait"
    assert evaluate_risk("latency_anomaly", policy) is None
    assert evaluate_risk("meltdown", policy) == "rollback"
    assert evaluate_risk("governance_decay", policy) == "request_human"
    assert evaluate_risk("unknown_trigger", policy) is None
    assert evaluate_risk("", policy) is None


def test_risk_events_are_hash_chained(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_chain.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_chain"
        store.create_run_started(Run(run_id=run_id, goal="chain"))
        ingest_risk(store, run_id, {"trigger": "loop"})
        ingest_risk(store, run_id, {"trigger": "meltdown"})
        report = store.verify_events(run_id)
        assert report.ok is True
        assert report.trusted_through[run_id] == 3
