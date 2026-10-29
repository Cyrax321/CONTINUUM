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
- Review-of-#1680 regressions: retry under a new CID, malformed CIDs on retry,
  malformed hook CIDs, keyless action records, uncorrelated observations, and
  TOOL_CALLED status reporting.
- Review-of-#1875 regression: filtering evidence chains by an earlier retry
  attempt's correlation ID.
"""

from __future__ import annotations

import io
import json
import sqlite3

import pytest
from pydantic import ValidationError

from continuum.actions.ledger import ActionLedger, LedgerError, fold_action_events
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


def test_retry_under_new_correlation_id_is_matched_not_ambiguous(
    storage: SQLiteStorage, run_id: str
) -> None:
    """A fail-then-retry on the same key may take a new CID (review of #1680).

    The chain must span both attempt CIDs, stay ``matched`` (not ``ambiguous``),
    and the retry's tool events must not surface as ``unmatched_observation``.
    """
    ledger = ActionLedger(storage, run_id)
    cid_first = "cid-retry-first"
    cid_retry = "cid-retry-second"

    outcome = ledger.claim("send_email", {"to": "ops"}, correlation_id=cid_first)
    storage.append_event(
        run_id,
        EventType.TOOL_FAILED,
        {"tool": "smtp", "status": 500, "correlation_id": cid_first},
        source=Origin.EXTERNAL_AGENT,
    )
    ledger.fail(outcome.key, "SMTP 500", certain=True)

    # Same slot retried under a fresh correlation ID (no explicit key: the
    # exact argument-hash key resolves to the FAILED attempt)
    ledger.claim("send_email", {"to": "ops"}, correlation_id=cid_retry)
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "smtp", "status": 250, "correlation_id": cid_retry},
        source=Origin.EXTERNAL_AGENT,
    )

    chains = correlate_action_events(storage, run_id)
    assert len(chains) == 1, [c.status for c in chains]

    chain = chains[0]
    assert chain.action_key == outcome.key
    assert chain.status == "matched"
    assert chain.correlation_id == cid_retry  # latest attempt wins for display
    # Both attempts' tool events are collected under the one chain
    assert sorted(e["type"] for e in chain.tool_events) == ["TOOL_COMPLETED", "TOOL_FAILED"]
    assert not [c for c in chains if c.status == "unmatched_observation"]
    assert not [c for c in chains if c.status == "ambiguous"]


def test_filter_by_earlier_retry_correlation_id_still_finds_chain(
    storage: SQLiteStorage, run_id: str
) -> None:
    """Filtering by an earlier attempt's CID must not lose the chain (#1875).

    A retry collects one CID per attempt and the chain spans them all, so an
    operator looking up the first (failed) attempt by its own ID must still
    see the chain with that attempt's tool events. The chain used to expose
    only the latest CID, so the equality filter returned nothing.
    """
    ledger = ActionLedger(storage, run_id)
    cid_first = "cid-retry-first"
    cid_retry = "cid-retry-second"

    outcome = ledger.claim("send_email", {"to": "ops"}, correlation_id=cid_first)
    storage.append_event(
        run_id,
        EventType.TOOL_FAILED,
        {"tool": "smtp", "status": 500, "correlation_id": cid_first},
        source=Origin.EXTERNAL_AGENT,
    )
    ledger.fail(outcome.key, "SMTP 500", certain=True)
    ledger.claim("send_email", {"to": "ops"}, correlation_id=cid_retry)
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "smtp", "status": 250, "correlation_id": cid_retry},
        source=Origin.EXTERNAL_AGENT,
    )

    filtered = correlate_action_events(storage, run_id, correlation_id=cid_first)
    assert len(filtered) == 1, [c.status for c in filtered]

    chain = filtered[0]
    assert chain.action_key == outcome.key
    assert chain.status == "matched"
    assert chain.correlation_id == cid_retry  # display stays latest-wins
    assert chain.correlation_ids == [cid_first, cid_retry]
    # The earlier attempt's tool event is included in the filtered chain
    assert sorted(e["type"] for e in chain.tool_events) == ["TOOL_COMPLETED", "TOOL_FAILED"]
    assert "correlation_ids" in chain.to_dict()
    assert chain.to_dict()["correlation_ids"] == [cid_first, cid_retry]

    # The retry's own ID still filters to the same chain, and no other ID does
    assert len(correlate_action_events(storage, run_id, correlation_id=cid_retry)) == 1
    assert not correlate_action_events(storage, run_id, correlation_id="cid-unknown")


@pytest.mark.parametrize("bad_cid", ["", "   ", "a" * 129])
def test_failed_retry_branch_rejects_malformed_correlation_id(
    storage: SQLiteStorage, run_id: str, bad_cid: str
) -> None:
    """Failed and compensated re-claims validate the CID (review of #1680).

    Those branches settle via ``model_copy``, which skips field validators, so
    an unvalidated ID would survive into the event log and break every later
    replay of the run.
    """
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("charge_card", {"amount": 5}, correlation_id="cid-ok")
    ledger.fail(outcome.key, "declined", certain=True)

    # No explicit key: the retry resolves to the FAILED attempt's slot
    with pytest.raises(ValueError, match="correlation_id"):
        ledger.claim("charge_card", {"amount": 5}, correlation_id=bad_cid)


def test_compensated_retry_branch_rejects_malformed_correlation_id(
    storage: SQLiteStorage, run_id: str
) -> None:
    """The COMPENSATED retry path validates the CID too (review of #1680)."""
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("publish_doc", {"doc": "d1"}, correlation_id="cid-ok")
    ledger.complete(outcome.key, external_id="pub-1", correlation_id="cid-ok")
    ledger.compensate(outcome.key, note="retracted")

    with pytest.raises(ValueError, match="correlation_id"):
        ledger.claim("publish_doc", {"doc": "d1"}, correlation_id="")


def test_malformed_hook_correlation_id_does_not_drop_the_observation() -> None:
    """A malformed agent-supplied CID is dropped, not fatal (review of #1680).

    The observe hook must never fail the host tool call, and the observation is
    durable evidence regardless of linkage quality.
    """
    from continuum.clienthooks import observe_event_payload

    payload = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": "x.py", "correlation_id": 42}}
    )
    assert "correlation_id" not in payload
    assert payload["tool"] == "Write"


def test_cli_observe_rejects_explicitly_empty_correlation_id(tmp_path) -> None:
    """An explicitly empty --correlation-id is rejected, not silently ignored (#1680)."""
    db_path = str(tmp_path / "cli_empty_cid.db")
    s = SQLiteStorage(db_path)
    run = s.create_run_started(
        Run(run_id="cli_run_empty_cid", goal="empty cid rejected"), source=Origin.HUMAN
    )
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
            run.run_id,
            "--correlation-id",
            "",
            "--payload-file",
            str(payload_file),
        ],
        out=out,
        err=err,
    )
    assert rc != 0


def test_keyless_action_record_and_uncorrelated_tool_get_chains() -> None:
    """Keyless action records and uncorrelated tools are not dropped (#1680).

    A keyless record with a matching CID correlates; tool events without any CID
    surface as ``missing_correlation`` chains instead of vanishing.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "action": {
                    "action_id": "act-keyless",
                    "action_type": "notify",
                    "status": "started",
                    "correlation_id": "cid-keyless",
                },
            },
        ),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.TOOL_COMPLETED,
            payload={"tool": "slack", "correlation_id": "cid-keyless"},
        ),
        Event(
            run_id="r1",
            sequence=3,
            type=EventType.TOOL_CALLED,
            payload={"tool": "curl", "path": "https://example.com"},
        ),
    ]

    chains = correlate_evidence(events)
    assert len(chains) == 2

    matched = next(c for c in chains if c.status == "matched")
    assert matched.action_key is None
    assert matched.action_id == "act-keyless"
    assert matched.action_type == "notify"
    assert matched.action_status == "started"
    assert matched.correlation_id == "cid-keyless"
    assert len(matched.tool_events) == 1

    uncorrelated = next(c for c in chains if c.status == "missing_correlation")
    assert uncorrelated.action_key is None
    assert uncorrelated.correlation_id is None
    assert len(uncorrelated.tool_events) == 1
    assert uncorrelated.tool_events[0]["tool"] == "curl"


def test_tool_called_is_reported_as_called_not_failed() -> None:
    """A TOOL_CALLED event only says the tool started (review of #1680)."""
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.TOOL_CALLED,
            payload={"tool": "curl", "correlation_id": "cid-pending"},
        ),
    ]

    chains = correlate_evidence(events)
    assert len(chains) == 1
    chain = chains[0]
    assert chain.status == "unmatched_observation"
    assert chain.observations[0]["status"] == "called"


def test_nested_action_fields_are_used_as_fallback() -> None:
    """Action details come from the nested action when top-level ones are absent (#1680)."""
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "action": {
                    "action_id": "act-nested-9",
                    "action_type": "fetch",
                    "status": "started",
                    "correlation_id": "cid-nested",
                },
                "key": "k-nested",
            },
        ),
    ]

    chains = correlate_evidence(events)
    assert len(chains) == 1
    chain = chains[0]
    assert chain.action_id == "act-nested-9"
    assert chain.action_type == "fetch"
    assert chain.action_status == "started"


# -- coverage of the gaps the #1875 review flagged -------------------------


def test_reconcile_accepts_a_matching_correlation_id(storage: SQLiteStorage, run_id: str) -> None:
    """reconcile validates a correlation ID and settles when it matches the claim.

    The claim's ID is the contract; reconcile either confirms it or wrongs it,
    so the ID is validated on the way in and a match is required to settle.
    """
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("charge_card", {"amount": 5}, correlation_id="cid-recon-1")
    ledger.complete(outcome.key, external_id="tx-1", correlation_id="cid-recon-1")

    settled = ledger.reconcile(outcome.key, occurred=True, correlation_id="cid-recon-1")
    assert settled.correlation_id == "cid-recon-1"


def test_reconcile_settles_a_correlation_id_when_the_claim_had_none(
    storage: SQLiteStorage, run_id: str
) -> None:
    """A correlation ID supplied at reconcile time is accepted on an unlinked attempt."""
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("charge_card", {"amount": 5})

    settled = ledger.reconcile(outcome.key, occurred=False, correlation_id="cid-recon-2")
    assert settled.correlation_id == "cid-recon-2"


def test_reconcile_rejects_a_mismatched_correlation_id(storage: SQLiteStorage, run_id: str) -> None:
    """A correlation ID disagreeing with the claim's is refused (review of #1680).

    Silently accepting it would link the reconciliation to an attempt the
    caller cannot name, so the mismatch is an error rather than an override.
    """
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("charge_card", {"amount": 5}, correlation_id="cid-recon-3")
    ledger.complete(outcome.key, external_id="tx-3", correlation_id="cid-recon-3")

    with pytest.raises(LedgerError, match="correlation_id mismatch"):
        ledger.reconcile(outcome.key, occurred=True, correlation_id="cid-other")


def test_fold_action_events_backfills_a_top_level_correlation_id() -> None:
    """Events written before the nested shape carried the ID at the top level.

    The fold backfills it into the action record so an old log still correlates
    after the payload shape moved the field into the nested action.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "k-backfill",
                "action": {
                    "run_id": "r1",
                    "action_id": "act-backfill",
                    "action_type": "fetch",
                    "status": "completed",
                },
                "correlation_id": "cid-legacy-top-level",
            },
        ),
    ]

    folded = fold_action_events(events)

    assert folded["k-backfill"].correlation_id == "cid-legacy-top-level"


def test_fold_action_events_accepts_an_in_memory_action_instance() -> None:
    """The fold also takes events whose payload already carries an Action.

    In-memory callers (before storage serialization) hold the model instance
    rather than its dict form, and the fold must read it without rebuilding.
    """
    action = Action(
        run_id="r1",
        action_type="fetch",
        status=ActionStatus.STARTED,
        correlation_id="cid-instance",
    )
    events = [
        Event.model_construct(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={"key": "k-instance", "action": action},
        ),
    ]

    folded = fold_action_events(events)

    assert folded["k-instance"] is action


def test_fold_action_events_skips_records_with_no_action_body() -> None:
    """An action event carrying no usable action body contributes nothing.

    Action details may be absent from a hand-built or partially written
    record, and the fold must skip it rather than raise on the shape.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={"key": "k-no-body"},
        ),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.ACTION_RECORDED,
            payload={"key": "k-null-body", "action": None},
        ),
    ]

    assert fold_action_events(events) == {}


def test_a_non_dict_event_payload_is_treated_as_uncorrelated() -> None:
    """A tool event whose payload is not a mapping correlates nothing.

    ``model_construct`` is how such an event reaches a reader that does not
    re-validate: the extractor must read it as "no identifier" rather than
    crash on the lookup.
    """
    events = [
        Event.model_construct(
            run_id="r1",
            sequence=1,
            type=EventType.TOOL_COMPLETED,
            payload=None,
        ),
    ]

    chains = correlate_evidence(events)

    assert len(chains) == 1
    chain = chains[0]
    assert chain.status == "missing_correlation"
    assert chain.correlation_ids == []


def test_a_malformed_correlation_id_in_an_event_is_treated_as_absent() -> None:
    """An identifier that fails the contract is ignored, not fatal to the chain.

    The event streams a conformance runner reads are written by other
    implementations, so an out-of-contract ID degrades the chain to
    uncorrelated instead of dropping the action.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "k-bad-cid",
                "action": {
                    "action_id": "act-bad-cid",
                    "action_type": "fetch",
                    "status": "started",
                    "correlation_id": "x" * 129,
                },
            },
        ),
    ]

    chains = correlate_evidence(events)

    assert len(chains) == 1
    chain = chains[0]
    assert chain.status == "missing_correlation"
    assert chain.correlation_ids == []


def test_a_later_action_event_without_a_correlation_id_keeps_the_earlier_attempts() -> None:
    """A settle event that carries no ID does not wipe the attempt's identifiers.

    Compensations and completions are separate events from the claim, and only
    the claim (and a retry's claim) names an identifier; the chain must keep
    the attempt's collected IDs so its tool events still match.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "k-multi",
                "action_id": "act-multi",
                "action_type": "fetch",
                "status": "started",
                "correlation_id": "cid-keep",
            },
        ),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.ACTION_COMPENSATED,
            payload={
                "key": "k-multi",
                "action_id": "act-multi",
                "action_type": "fetch",
                "status": "compensated",
            },
        ),
    ]

    chains = correlate_evidence(events, correlation_id="cid-keep")

    assert len(chains) == 1
    chain = chains[0]
    assert chain.correlation_ids == ["cid-keep"]
    assert chain.action_status == "compensated"


def test_a_later_action_event_fills_in_missing_action_id_and_type() -> None:
    """A key's later events fill id and type the first event lacked.

    The fold keeps the first event's shape but the chain's display fields
    should not stay empty merely because an early hand-built record omitted
    them.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "k-fill",
                "action": {"status": "started"},
            },
        ),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.ACTION_COMPENSATED,
            payload={
                "key": "k-fill",
                "action_id": "act-fill",
                "action_type": "fetch",
                "status": "compensated",
            },
        ),
    ]

    chains = correlate_evidence(events)

    assert len(chains) == 1
    chain = chains[0]
    assert chain.action_id == "act-fill"
    assert chain.action_type == "fetch"


def test_a_keyless_action_record_without_a_correlation_id_is_reported() -> None:
    """A keyless record with no ID is its own missing_correlation chain.

    It was previously dropped entirely; with no identifier it cannot match
    tool events, but the record is still evidence and must surface.
    """
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "action": {"action_id": "act-keyless-nocid", "status": "started"},
            },
        ),
    ]

    chains = correlate_evidence(events)

    assert len(chains) == 1
    chain = chains[0]
    assert chain.action_key is None
    assert chain.status == "missing_correlation"


def test_filtering_by_action_key_returns_only_that_chain() -> None:
    """The action_key filter narrows the projection to one key's chain."""
    events = [
        Event(
            run_id="r1",
            sequence=1,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "k-alpha",
                "action_id": "act-alpha",
                "action_type": "fetch",
                "status": "started",
                "correlation_id": "cid-a",
            },
        ),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "k-beta",
                "action_id": "act-beta",
                "action_type": "fetch",
                "status": "started",
                "correlation_id": "cid-b",
            },
        ),
    ]

    chains = correlate_evidence(events, action_key="k-beta")

    assert len(chains) == 1
    assert chains[0].action_key == "k-beta"


def test_cli_evidence_chain_on_a_run_with_no_evidence(tmp_path) -> None:
    """An empty run reports no chains rather than an empty header."""
    db_path = str(tmp_path / "cli_empty_chain.db")
    s = SQLiteStorage(db_path)
    run = s.create_run_started(
        Run(run_id="cli_run_empty_chain", goal="no evidence yet"), source=Origin.HUMAN
    )

    out = io.StringIO()
    err = io.StringIO()
    rc = main(["--db", db_path, "evidence-chain", run.run_id], out=out, err=err)

    assert rc == 0
    text = out.getvalue()
    assert "Evidence chain for run" in text
    assert "(no correlated actions or observations recorded)" in text


def test_cli_evidence_chain_unmatched_only_hides_matched_chains(tmp_path) -> None:
    """--unmatched-only drops matched chains from the report."""
    db_path = str(tmp_path / "cli_unmatched_only.db")
    s = SQLiteStorage(db_path)
    run = s.create_run_started(
        Run(run_id="cli_run_unmatched", goal="unmatched filtering"), source=Origin.HUMAN
    )
    run_id = run.run_id

    ledger = ActionLedger(s, run_id)
    ledger.claim("build_project", {"clean": True}, correlation_id="cid-matched")
    # Correlate the claim so the run holds one matched chain.
    s.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "make", "correlation_id": "cid-matched"},
        source=Origin.EXTERNAL_AGENT,
    )

    # An observation with an identifier no action carries: unmatched.
    s.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "make", "correlation_id": "cid-unknown"},
        source=Origin.EXTERNAL_AGENT,
    )

    out_all = io.StringIO()
    err = io.StringIO()
    rc_all = main(["--db", db_path, "evidence-chain", run_id], out=out_all, err=err)
    assert rc_all == 0
    assert "[ok] MATCHED:" in out_all.getvalue()
    assert "UNMATCHED_OBSERVATION" in out_all.getvalue()

    out_filtered = io.StringIO()
    rc_filtered = main(
        ["--db", db_path, "evidence-chain", run_id, "--unmatched-only"],
        out=out_filtered,
        err=err,
    )
    assert rc_filtered == 0
    text = out_filtered.getvalue()
    assert "UNMATCHED_OBSERVATION" in text
    assert "[ok] MATCHED:" not in text


def test_cli_evidence_chain_renders_chains_missing_one_side(tmp_path) -> None:
    """The human report omits the side a chain has no events for.

    An unmatched action has no tool events, and an unmatched observation has
    no action events, so neither should render a hollow section header. Each
    kind gets its own run so the assertion is not blurred by the other chain.
    """
    err = io.StringIO()

    def report(db_path: str, run_id: str) -> str:
        out = io.StringIO()
        rc = main(["--db", db_path, "evidence-chain", run_id], out=out, err=err)
        assert rc == 0
        return out.getvalue()

    # An action with an identifier and no tool events: unmatched_action.
    db_action = str(tmp_path / "cli_one_sided_action.db")
    s = SQLiteStorage(db_action)
    run = s.create_run_started(
        Run(run_id="cli_run_one_sided_action", goal="orphan action"), source=Origin.HUMAN
    )
    s.append_event(
        run.run_id,
        EventType.ACTION_RECORDED,
        {
            "key": "k-one-sided",
            "action_id": "act-one-sided",
            "action_type": "fetch",
            "status": "started",
            "correlation_id": "cid-orphan-action",
        },
        source=Origin.DETERMINISTIC,
    )
    action_text = report(db_action, run.run_id)
    assert "UNMATCHED_ACTION" in action_text
    assert "tools:" not in action_text

    # A tool event with an identifier no action carries: unmatched_observation.
    db_tool = str(tmp_path / "cli_one_sided_tool.db")
    s = SQLiteStorage(db_tool)
    run = s.create_run_started(
        Run(run_id="cli_run_one_sided_tool", goal="orphan observation"),
        source=Origin.HUMAN,
    )
    s.append_event(
        run.run_id,
        EventType.TOOL_COMPLETED,
        {"tool": "curl", "correlation_id": "cid-orphan-tool"},
        source=Origin.EXTERNAL_AGENT,
    )
    tool_text = report(db_tool, run.run_id)
    assert "UNMATCHED_OBSERVATION" in tool_text
    assert "actions:" not in tool_text
