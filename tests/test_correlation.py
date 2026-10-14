"""Tests for provenance correlation between observed tool events and action attempts (#785).

Covers:
- Correlation identifier validation (bounds, types, emptiness).
- Action and ActionRecordPayload models.
- ActionLedger claim, complete, reconcile with correlation IDs.
- Disambiguating repeated similar actions.
- Unmatched actions and unmatched observations.
- Ambiguous correlation identifiers (multiple actions with same CID).
- Compaction survival across archive boundary.
- Hash-chain tamper detection.
- Proof that correlated tool completion does not auto-complete an uncertain action.
- CLI observe and evidence-chain subcommands.
"""

from __future__ import annotations

import io
import json
import sqlite3

import pytest
from pydantic import ValidationError

from continuum.actions.ledger import ActionLedger, LedgerError
from continuum.cli.main import main
from continuum.events import Event, EventType
from continuum.models import (
    Action,
    ActionRecordPayload,
    ActionStatus,
    Origin,
    Run,
    validate_correlation_id,
)
from continuum.provenance.correlation import (
    correlate_action_events,
    correlate_evidence,
)
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.sqlite import SQLiteStorage


@pytest.fixture
def storage(tmp_path) -> SQLiteStorage:
    db_path = str(tmp_path / "continuum.db")
    return SQLiteStorage(db_path)


@pytest.fixture
def run_id(storage: SQLiteStorage) -> str:
    run = storage.create_run_started(
        Run(run_id="test_run_1", goal="Test correlation of observed tools with actions"),
        source=Origin.HUMAN,
    )
    return run.run_id


def test_validate_correlation_id() -> None:
    assert validate_correlation_id(None) is None
    assert validate_correlation_id("cid-1234") == "cid-1234"
    assert validate_correlation_id("a" * 128) == "a" * 128

    with pytest.raises(ValueError, match="must be a string"):
        validate_correlation_id(12345)

    with pytest.raises(ValueError, match="must be a string"):
        validate_correlation_id({"cid": "test"})

    with pytest.raises(ValueError, match="1-128 characters and non-empty"):
        validate_correlation_id("")

    with pytest.raises(ValueError, match="1-128 characters and non-empty"):
        validate_correlation_id("   ")

    with pytest.raises(ValueError, match="1-128 characters and non-empty"):
        validate_correlation_id("a" * 129)


def test_action_model_correlation_id() -> None:
    act = Action(
        run_id="run-1",
        action_type="deploy",
        correlation_id="cid-abc-123",
    )
    assert act.correlation_id == "cid-abc-123"

    dumped = act.model_dump(mode="json")
    assert dumped["correlation_id"] == "cid-abc-123"

    loaded = Action.model_validate(dumped)
    assert loaded.correlation_id == "cid-abc-123"

    payload = ActionRecordPayload(
        action_type="deploy",
        correlation_id="cid-abc-123",
    )
    assert payload.correlation_id == "cid-abc-123"

    with pytest.raises(ValidationError):
        Action(run_id="run-1", action_type="deploy", correlation_id="")


def test_ledger_claim_complete_and_mismatch(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim(
        "charge_card",
        {"amount": 100, "customer": "cust_1"},
        correlation_id="cid-claim-1",
    )
    assert outcome.fresh is True
    assert outcome.action.correlation_id == "cid-claim-1"

    # Mismatch on completion raises LedgerError
    with pytest.raises(LedgerError, match="correlation_id mismatch"):
        ledger.complete(outcome.key, correlation_id="cid-different")

    # Matching completion succeeds
    completed = ledger.complete(outcome.key, correlation_id="cid-claim-1", external_id="ch_123")
    assert completed.status is ActionStatus.COMPLETED
    assert completed.correlation_id == "cid-claim-1"


def test_matched_claim_and_observation_pair(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)
    cid = "corr-matched-001"
    outcome = ledger.claim("write_file", {"path": "out.txt"}, correlation_id=cid)

    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {
            "tool": "Write",
            "path": "out.txt",
            "bytes": 42,
            "sha256": "abcdef123456",
            "correlation_id": cid,
        },
        source=Origin.EXTERNAL_AGENT,
    )

    chains = correlate_action_events(storage, run_id)
    assert len(chains) == 1
    chain = chains[0]
    assert chain.action_key == outcome.key
    assert chain.correlation_id == cid
    assert chain.status == "matched"
    assert len(chain.action_events) == 1
    assert len(chain.tool_events) == 1
    assert chain.observations[0]["path"] == "out.txt"


def test_repeated_similar_actions_disambiguation(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)

    # Two attempts of the same action type with distinct correlation IDs
    cid1 = "corr-attempt-1"
    cid2 = "corr-attempt-2"

    outcome1 = ledger.claim(
        "send_webhook", {"url": "https://api.test/v1"}, key="hook-attempt-1", correlation_id=cid1
    )
    storage.append_event(
        run_id,
        EventType.TOOL_FAILED,
        {"tool": "http_request", "status": 500, "correlation_id": cid1},
        source=Origin.EXTERNAL_AGENT,
    )
    ledger.fail(outcome1.key, "HTTP 500 error", certain=True)

    outcome2 = ledger.claim(
        "send_webhook", {"url": "https://api.test/v1"}, key="hook-attempt-2", correlation_id=cid2
    )
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "http_request", "status": 200, "correlation_id": cid2},
        source=Origin.EXTERNAL_AGENT,
    )
    ledger.complete(outcome2.key, external_id="resp-200", correlation_id=cid2)

    chains = correlate_action_events(storage, run_id)
    assert len(chains) == 2

    chain1 = next(c for c in chains if c.correlation_id == cid1)
    chain2 = next(c for c in chains if c.correlation_id == cid2)

    assert chain1.action_key == outcome1.key
    assert chain1.status == "matched"
    assert chain1.tool_events[0]["type"] == "TOOL_FAILED"

    assert chain2.action_key == outcome2.key
    assert chain2.status == "matched"
    assert chain2.tool_events[0]["type"] == "TOOL_COMPLETED"


def test_unmatched_action_and_unmatched_observation(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)
    ledger.claim("query_db", {"sql": "SELECT 1"}, correlation_id="cid-no-tool")

    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "Bash", "path": "script.sh", "correlation_id": "cid-orphan-tool"},
        source=Origin.EXTERNAL_AGENT,
    )

    chains = correlate_action_events(storage, run_id)
    assert len(chains) == 2

    unmatched_action = next(c for c in chains if c.status == "unmatched_action")
    assert unmatched_action.correlation_id == "cid-no-tool"

    unmatched_obs = next(c for c in chains if c.status == "unmatched_observation")
    assert unmatched_obs.correlation_id == "cid-orphan-tool"


def test_ambiguous_correlation_id(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)
    shared_cid = "cid-shared-conflict"

    # Two distinct keys attempting to bind the same correlation ID
    ledger.claim("task_a", {"foo": 1}, key="key-a", correlation_id=shared_cid)
    ledger.claim("task_b", {"bar": 2}, key="key-b", correlation_id=shared_cid)

    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "custom", "correlation_id": shared_cid},
        source=Origin.EXTERNAL_AGENT,
    )

    chains = correlate_action_events(storage, run_id)
    assert len(chains) == 2
    for c in chains:
        assert c.status == "ambiguous"


def test_compaction_survives_correlation(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)
    cid = "cid-survives-compaction"
    outcome = ledger.claim("backup_data", {"target": "s3"}, correlation_id=cid)

    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "s3_upload", "path": "s3://bucket/data", "correlation_id": cid},
        source=Origin.EXTERNAL_AGENT,
    )
    ledger.complete(outcome.key, external_id="etag-123", correlation_id=cid)

    # Compact past the action and tool events
    events = storage.read_events(run_id)
    max_seq = max(e.sequence for e in events)
    storage.compact_run(run_id, through_sequence=max_seq)

    # Live events table is now compact, events are in events_archive
    chains = correlate_action_events(storage, run_id)
    assert len(chains) == 1
    assert chains[0].correlation_id == cid
    assert chains[0].status == "matched"


def test_tampering_detection(storage: SQLiteStorage, run_id: str) -> None:
    ledger = ActionLedger(storage, run_id)
    cid = "cid-tamper-check"
    ledger.claim("critical_op", {"param": 1}, correlation_id=cid)

    report_before = storage.verify_events(run_id)
    assert report_before.ok

    # Directly tamper with correlation_id in SQLite
    conn = sqlite3.connect(storage.path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sequence, payload FROM events WHERE run_id = ? AND type = 'ACTION_RECORDED'",
        (run_id,),
    )
    row = cursor.fetchone()
    seq, payload_json = row[0], row[1]
    payload = json.loads(payload_json)
    payload["correlation_id"] = "cid-tampered-value"
    cursor.execute(
        "UPDATE events SET payload = ? WHERE run_id = ? AND sequence = ?",
        (json.dumps(payload), run_id, seq),
    )
    conn.commit()
    conn.close()

    # Integrity verification must catch the tampered record
    report_after = storage.verify_events(run_id)
    assert not report_after.ok
    assert any("TAMPERED" in v.kind for v in report_after.violations)


def test_correlation_does_not_auto_complete_uncertain_action(
    storage: SQLiteStorage, run_id: str
) -> None:
    ledger = ActionLedger(storage, run_id)
    cid = "cid-proof-no-auto-complete"
    outcome = ledger.claim("transfer_funds", {"amount": 500}, correlation_id=cid)

    # Host observes a completed tool event
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "stripe_charge", "correlation_id": cid},
        source=Origin.EXTERNAL_AGENT,
    )

    # The ledger status remains STARTED (not automatically completed)
    current_action = ledger.get(outcome.key)
    assert current_action is not None
    assert current_action.status is ActionStatus.STARTED

    # Recovery assessment requires review / reconciliation, proving safe default
    assessment = RecoveryEngine(storage).assess(run_id)
    assert assessment.safe is False
    assert any(a.correlation_id == cid for a in assessment.uncertain_actions)


def test_cli_observe_and_evidence_chain(tmp_path) -> None:
    db_path = str(tmp_path / "cli_test.db")
    s = SQLiteStorage(db_path)
    run = s.create_run_started(
        Run(run_id="cli_run_1", goal="CLI evidence chain test"), source=Origin.HUMAN
    )
    run_id = run.run_id

    ledger = ActionLedger(s, run_id)
    cid = "cid-cli-test-99"
    ledger.claim("build_project", {"clean": True}, correlation_id=cid)

    # Call continuum observe via CLI with --correlation-id
    payload_file = tmp_path / "obs.json"
    payload_file.write_text(
        json.dumps({"tool_name": "make", "tool_input": {"file_path": "build.log"}}),
        encoding="utf-8",
    )

    out = io.StringIO()
    err = io.StringIO()
    rc = main(
        [
            "--db",
            db_path,
            "observe",
            "--run-id",
            run_id,
            "--correlation-id",
            cid,
            "--payload-file",
            str(payload_file),
        ],
        out=out,
        err=err,
    )
    assert rc == 0

    # Call continuum evidence-chain via CLI
    out_chain = io.StringIO()
    rc_chain = main(["--db", db_path, "evidence-chain", run_id], out=out_chain, err=err)
    assert rc_chain == 0
    text = out_chain.getvalue()
    assert "Evidence chain for run" in text
    assert cid in text
    assert "MATCHED" in text

    # Call continuum evidence-chain with --json
    out_json = io.StringIO()
    rc_json = main(["--db", db_path, "--json", "evidence-chain", run_id], out=out_json, err=err)
    assert rc_json == 0
    data = json.loads(out_json.getvalue())
    assert data["run_id"] == run_id
    assert data["count"] == 1
    assert data["chains"][0]["correlation_id"] == cid
    assert data["chains"][0]["status"] == "matched"


def test_correlate_evidence_direct() -> None:
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "action": {
                    "action_id": "act-1",
                    "action_type": "fetch",
                    "status": "started",
                    "correlation_id": "cid-direct-1",
                },
                "key": "k1",
            },
        ),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.TOOL_COMPLETED,
            payload={"tool": "http", "correlation_id": "cid-direct-1"},
        ),
        Event(
            run_id="r1",
            sequence=3,
            type=EventType.ACTION_RECORDED,
            payload={
                "action": {
                    "action_id": "act-2",
                    "action_type": "compute",
                    "status": "started",
                },
                "key": "k2",
            },
        ),
    ]

    chains = correlate_evidence(events)
    assert len(chains) == 2
    matched = next(c for c in chains if c.correlation_id == "cid-direct-1")
    assert matched.status == "matched"
    assert matched.action_key == "k1"

    missing = next(c for c in chains if c.status == "missing_correlation")
    assert missing.action_key == "k2"
