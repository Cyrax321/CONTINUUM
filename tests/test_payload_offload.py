"""Tests for out-of-band blob storage and payload offloading (issues #254, #1418, #1419)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from continuum.cli.exitcodes import ExitCode
from continuum.cli.main import main
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run, utcnow
from continuum.security.hashing import to_json
from continuum.state.semantic import project
from continuum.state.validator import StateValidator
from continuum.storage import (
    CorruptedRecord,
    SQLiteStorage,
    open_storage,
)
from continuum.storage.blob import (
    DEFAULT_PAYLOAD_OFFLOAD_BYTES,
    OFFLOAD_KEY,
    PAYLOAD_OFFLOAD_ENV_VAR,
    audit_blob_descriptor,
    create_offload_descriptor,
    get_blob_dir,
    get_blob_path,
    get_payload_offload_threshold,
    is_offload_descriptor,
    load_blob_payload,
    maybe_offload_payload,
    maybe_rehydrate_payload,
    read_blob,
    write_blob,
)
from continuum.storage.postgres import PostgresStorage


def test_payload_offload_threshold_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold parses from environment variable with safe fallbacks."""
    monkeypatch.delenv(PAYLOAD_OFFLOAD_ENV_VAR, raising=False)
    assert get_payload_offload_threshold() == DEFAULT_PAYLOAD_OFFLOAD_BYTES
    assert get_payload_offload_threshold() == 0

    monkeypatch.setenv(PAYLOAD_OFFLOAD_ENV_VAR, "1024")
    assert get_payload_offload_threshold() == 1024

    monkeypatch.setenv(PAYLOAD_OFFLOAD_ENV_VAR, "0")
    assert get_payload_offload_threshold() == 0

    monkeypatch.setenv(PAYLOAD_OFFLOAD_ENV_VAR, "-50")
    assert get_payload_offload_threshold() == 0

    monkeypatch.setenv(PAYLOAD_OFFLOAD_ENV_VAR, "not-a-number")
    assert get_payload_offload_threshold() == 0


def test_blob_paths_and_descriptor() -> None:
    """Blob paths conform to <storage_dir>/blobs/<sha256>.blob structure."""
    base = Path("/tmp/storage")
    assert get_blob_dir(base) == base / "blobs"

    sample_hash = "a" * 64
    assert get_blob_path(base, sample_hash) == base / "blobs" / f"{sample_hash}.blob"

    descriptor = create_offload_descriptor(sample_hash, 1234, ["a", "b"])
    assert descriptor == {
        OFFLOAD_KEY: sample_hash,
        "size_bytes": 1234,
        "keys": ["a", "b"],
    }
    assert is_offload_descriptor(descriptor)
    assert not is_offload_descriptor({"a": 1})
    assert not is_offload_descriptor(None)
    assert not is_offload_descriptor("string")


def test_write_and_read_blob(tmp_path: Path) -> None:
    """write_blob writes canonical bytes atomically; read_blob verifies digest."""
    payload = {"greeting": "hello world", "numbers": [1, 2, 3]}
    canonical_bytes = to_json(payload).encode("utf-8")
    expected_hash = hashlib.sha256(canonical_bytes).hexdigest()

    blob_path = write_blob(tmp_path, canonical_bytes)
    assert blob_path.exists()
    assert blob_path == tmp_path / "blobs" / f"{expected_hash}.blob"
    assert blob_path.read_bytes() == canonical_bytes

    # Idempotent write
    again = write_blob(tmp_path, canonical_bytes, sha256_hex=expected_hash)
    assert again == blob_path

    # Read blob
    data = read_blob(tmp_path, expected_hash)
    assert data == canonical_bytes

    # Load blob payload
    desc = create_offload_descriptor(expected_hash, len(canonical_bytes), payload.keys())
    loaded = load_blob_payload(tmp_path, desc)
    assert loaded == payload

    # Rehydrate helper
    rehydrated = maybe_rehydrate_payload(desc, storage_dir=tmp_path)
    assert rehydrated == payload

    plain = {"not": "offloaded"}
    assert maybe_rehydrate_payload(plain, storage_dir=tmp_path) == plain


def test_read_blob_integrity_failures(tmp_path: Path) -> None:
    """read_blob raises CorruptedRecord on missing or tampered blobs."""
    missing_hash = "b" * 64
    with pytest.raises(CorruptedRecord, match="missing blob"):
        read_blob(tmp_path, missing_hash)

    # Tampered blob
    payload = {"field": "valid"}
    canonical_bytes = to_json(payload).encode("utf-8")
    real_hash = hashlib.sha256(canonical_bytes).hexdigest()
    blob_path = write_blob(tmp_path, canonical_bytes, real_hash)

    # Corrupt the file on disk
    blob_path.write_bytes(b"tampered-content")
    with pytest.raises(CorruptedRecord, match="corrupted blob"):
        read_blob(tmp_path, real_hash)


def test_audit_blob_descriptor_unit(tmp_path: Path) -> None:
    """audit_blob_descriptor detects missing and tampered blobs, ignoring non-descriptors."""
    # Non-descriptor returns None
    assert (
        audit_blob_descriptor(
            tmp_path,
            {"not": "descriptor"},
            run_id="run_1",
            sequence=1,
            event_id="ev_1",
        )
        is None
    )

    # Valid blob returns None
    payload = {"status": "ok"}
    canonical_bytes = to_json(payload).encode("utf-8")
    expected_hash = hashlib.sha256(canonical_bytes).hexdigest()
    blob_path = write_blob(tmp_path, canonical_bytes, expected_hash)
    descriptor = create_offload_descriptor(expected_hash, len(canonical_bytes), payload.keys())

    assert (
        audit_blob_descriptor(
            tmp_path,
            descriptor,
            run_id="run_1",
            sequence=1,
            event_id="ev_1",
        )
        is None
    )

    # Missing blob returns BLOB_MISSING violation
    blob_path.unlink()
    missing_violation = audit_blob_descriptor(
        tmp_path,
        descriptor,
        run_id="run_1",
        sequence=1,
        event_id="ev_1",
    )
    assert missing_violation is not None
    assert missing_violation.kind == "BLOB_MISSING"
    assert missing_violation.sequence == 1
    assert expected_hash in missing_violation.detail

    # Tampered blob returns BLOB_DIGEST_MISMATCH violation
    blob_path.write_bytes(b"corrupt")
    mismatch_violation = audit_blob_descriptor(
        tmp_path,
        descriptor,
        run_id="run_1",
        sequence=2,
        event_id="ev_2",
        prefix="archived",
    )
    assert mismatch_violation is not None
    assert mismatch_violation.kind == "BLOB_DIGEST_MISMATCH"
    assert mismatch_violation.sequence == 2
    assert "archived:" in mismatch_violation.detail


def test_maybe_offload_payload_thresholds(tmp_path: Path) -> None:
    """maybe_offload_payload respects threshold limits and descriptor formats."""
    small_payload = {"short": "val"}
    canonical_len = len(to_json(small_payload).encode("utf-8"))

    # Disabled (threshold = 0)
    result, offloaded = maybe_offload_payload(small_payload, storage_dir=tmp_path, threshold=0)
    assert not offloaded
    assert result == small_payload

    # Above threshold
    result, offloaded = maybe_offload_payload(
        small_payload, storage_dir=tmp_path, threshold=canonical_len - 1
    )
    assert offloaded
    assert is_offload_descriptor(result)
    assert result["size_bytes"] == canonical_len
    assert result["keys"] == ["short"]
    sha256_hex = result[OFFLOAD_KEY]
    assert (tmp_path / "blobs" / f"{sha256_hex}.blob").exists()

    # Exact threshold (not exceeded)
    result2, offloaded2 = maybe_offload_payload(
        small_payload, storage_dir=tmp_path, threshold=canonical_len
    )
    assert not offloaded2
    assert result2 == small_payload

    # Already offloaded descriptor is not double-offloaded
    result3, offloaded3 = maybe_offload_payload(result, storage_dir=tmp_path, threshold=1)
    assert not offloaded3
    assert result3 == result


def test_sqlite_append_event_offloading(tmp_path: Path) -> None:
    """SQLiteStorage offloads oversized payloads and maintains hash chain integrity."""
    db_file = tmp_path / "test.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_test", goal="test payload offloading")
        store.create_run_started(run)

        # 1. Append small event (below threshold)
        small_payload = {"key": "small"}
        assert len(to_json(small_payload).encode("utf-8")) < threshold
        ev1 = store.append_event(run.run_id, EventType.TOOL_CALLED, small_payload)
        assert ev1.payload == small_payload
        assert not is_offload_descriptor(ev1.payload)

        # 2. Append large event (exceeding threshold)
        large_payload = {
            "large_text": "x" * 200,
            "metadata": {"nested": "details" * 10},
        }
        canonical_bytes = to_json(large_payload).encode("utf-8")
        assert len(canonical_bytes) > threshold
        expected_hash = hashlib.sha256(canonical_bytes).hexdigest()

        ev2 = store.append_event(run.run_id, EventType.TOOL_COMPLETED, large_payload)
        assert is_offload_descriptor(ev2.payload)
        assert ev2.payload[OFFLOAD_KEY] == expected_hash
        assert ev2.payload["size_bytes"] == len(canonical_bytes)
        assert sorted(ev2.payload["keys"]) == ["large_text", "metadata"]

        # Check blob file exists on disk
        blob_file = tmp_path / "blobs" / f"{expected_hash}.blob"
        assert blob_file.exists()
        assert blob_file.read_bytes() == canonical_bytes

        # Cryptographic tamper evidence: hash covers descriptor
        assert ev2.hash == ev2.digest()
        assert ev2.prev_hash == ev1.hash

        # 3. Append another event to continue chain
        ev3 = store.append_event(run.run_id, EventType.RUN_COMPLETED, {"outcome": "ok"})
        assert ev3.prev_hash == ev2.hash
        assert ev3.hash == ev3.digest()

        # 4. Verify hash chain integrity
        report = store.verify_events(run.run_id)
        assert report.ok
        assert report.checked == 4  # RUN_STARTED + 3 appended events
        assert len(report.violations) == 0


def test_sqlite_open_storage_and_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTINUUM_PAYLOAD_OFFLOAD_BYTES is picked up via open_storage."""
    monkeypatch.setenv(PAYLOAD_OFFLOAD_ENV_VAR, "40")
    db_file = tmp_path / "env_test.db"

    with open_storage(f"sqlite:///{db_file}") as store:
        assert store.payload_offload_bytes == 40
        run = Run(run_id="run_env", goal="test env var threshold")
        store.create_run_started(run)

        large_payload = {"data": "a" * 100}
        event = store.append_event(run.run_id, EventType.EVIDENCE_ADDED, large_payload)
        assert is_offload_descriptor(event.payload)

        # Blobs directory defaults to db parent directory
        blob_path = store.storage_dir / "blobs" / f"{event.payload[OFFLOAD_KEY]}.blob"
        assert blob_path.exists()


def test_sqlite_action_index_with_offloaded_event(tmp_path: Path) -> None:
    """Action index remains correctly populated when ACTION_* payload is offloaded."""
    db_file = tmp_path / "action_test.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_act", goal="test action index offload")
        store.create_run_started(run)

        action = Action(
            action_id="act_1",
            run_id=run.run_id,
            action_type="shell",
            arguments={"cmd": "echo " + "z" * 100},  # Oversized payload
            status=ActionStatus.STARTED,
            created_at=utcnow(),
        )
        payload = {
            "key": "action:id:123",
            "action": action.model_dump(mode="json"),
        }
        assert len(to_json(payload).encode("utf-8")) > threshold

        event = store.append_event(run.run_id, EventType.ACTION_RECORDED, payload)
        assert is_offload_descriptor(event.payload)

        # Action index should have indexed the action entry
        found = store.foreign_action("action:id:123", exclude_run="other_run")
        assert found is not None
        assert found.action_id == "act_1"

        # Action index rebuild should succeed without drift
        drift = store.action_index_drift()
        assert drift == 0
        corrections = store.rebuild_action_index()
        assert corrections == 0


def test_postgres_append_event_offload_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgresStorage wires maybe_offload_payload into append_event."""
    mock_psycopg = MagicMock()
    mock_rows = MagicMock()
    mock_psycopg.rows = mock_rows
    mock_conn = MagicMock()
    mock_psycopg.connect.return_value = mock_conn

    # Mock head event query
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"sequence": 1, "hash": "prevhash"}
    mock_conn.execute.return_value = mock_cursor

    monkeypatch.setitem(sys.modules, "psycopg", mock_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", mock_rows)

    with patch("continuum.storage.postgres._require_psycopg", return_value=mock_psycopg):
        store = PostgresStorage(
            "postgresql://localhost/dummy",
            storage_dir=tmp_path,
            payload_offload_bytes=30,
        )
        assert store.payload_offload_bytes == 30
        assert store.storage_dir == tmp_path

        large_payload = {"details": "w" * 100}
        event = store.append_event("run_pg", EventType.TOOL_CALLED, large_payload)

        assert is_offload_descriptor(event.payload)
        sha256_hex = event.payload[OFFLOAD_KEY]
        blob_file = tmp_path / "blobs" / f"{sha256_hex}.blob"
        assert blob_file.exists()

        # Hash chain covers the offloaded descriptor
        assert event.hash == event.digest()


def test_sqlite_read_events_transparent_rehydration(tmp_path: Path) -> None:
    """read_events and read_all_events rehydrate payloads by default, returning raw descriptors when rehydrate=False."""
    db_file = tmp_path / "rehydrate_test.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_rehydrate", goal="test payload rehydration")
        store.create_run_started(run)

        small_payload = {"short": "value"}
        large_payload = {"large_text": "x" * 200, "meta": "test"}

        store.append_event(run.run_id, EventType.TOOL_CALLED, small_payload)
        ev2 = store.append_event(run.run_id, EventType.TOOL_COMPLETED, large_payload)
        assert is_offload_descriptor(ev2.payload)

        # Default read_events rehydrates transparently
        events = store.read_events(run.run_id)
        assert len(events) == 3
        assert events[1].payload == small_payload
        assert events[2].payload == large_payload
        assert not is_offload_descriptor(events[2].payload)

        # rehydrate=False returns the offload descriptor
        raw_events = store.read_events(run.run_id, rehydrate=False)
        assert len(raw_events) == 3
        assert raw_events[1].payload == small_payload
        assert is_offload_descriptor(raw_events[2].payload)
        assert raw_events[2].payload[OFFLOAD_KEY] == ev2.payload[OFFLOAD_KEY]

        # read_all_events respects rehydrate flag
        all_events = store.read_all_events(run.run_id)
        assert all_events[2].payload == large_payload

        all_raw = store.read_all_events(run.run_id, rehydrate=False)
        assert is_offload_descriptor(all_raw[2].payload)


def test_read_events_missing_blob_fails_closed(tmp_path: Path) -> None:
    """read_events fails closed with CorruptedRecord identifying sequence and blob hash when blob is missing."""
    db_file = tmp_path / "missing_blob.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_missing", goal="test missing blob fails closed")
        store.create_run_started(run)

        large_payload = {"data": "y" * 150}
        ev = store.append_event(run.run_id, EventType.EVIDENCE_ADDED, large_payload)
        sha256_hex = ev.payload[OFFLOAD_KEY]
        blob_file = tmp_path / "blobs" / f"{sha256_hex}.blob"
        assert blob_file.exists()

        # Delete blob file
        blob_file.unlink()

        # Reading with rehydration fails closed
        with pytest.raises(CorruptedRecord) as exc_info:
            store.read_events(run.run_id)

        msg = str(exc_info.value)
        assert f"sequence {ev.sequence}" in msg
        assert sha256_hex in msg
        assert "missing or corrupt blob" in msg

        # Reading without rehydration succeeds, allowing raw inspection
        raw_events = store.read_events(run.run_id, rehydrate=False)
        assert len(raw_events) == 2
        assert is_offload_descriptor(raw_events[1].payload)


def test_read_events_corrupted_blob_fails_closed(tmp_path: Path) -> None:
    """read_events fails closed with CorruptedRecord when a blob's content on disk has been tampered with."""
    db_file = tmp_path / "corrupt_blob.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_corrupt", goal="test corrupted blob fails closed")
        store.create_run_started(run)

        large_payload = {"data": "z" * 150}
        ev = store.append_event(run.run_id, EventType.FINDING_ADDED, large_payload)
        sha256_hex = ev.payload[OFFLOAD_KEY]
        blob_file = tmp_path / "blobs" / f"{sha256_hex}.blob"
        assert blob_file.exists()

        # Tamper with blob content
        blob_file.write_bytes(b"tampered payload content")

        with pytest.raises(CorruptedRecord) as exc_info:
            store.read_events(run.run_id)

        msg = str(exc_info.value)
        assert f"sequence {ev.sequence}" in msg
        assert sha256_hex in msg


def test_downstream_project_and_validation_with_rehydrated_events(tmp_path: Path) -> None:
    """Downstream projection and validation execute transparently over rehydrated offloaded events."""
    db_file = tmp_path / "project_test.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_proj", goal="test downstream project")
        store.create_run_started(run)

        evidence_payload = {
            "evidence_id": "ev_large_1",
            "summary": "Large summary: " + "e" * 120,
            "source": "remote_sensor",
        }
        finding_payload = {
            "finding_id": "find_large_1",
            "claim": "Claim with details: " + "f" * 120,
            "evidence": ["ev_large_1"],
        }
        store.append_event(run.run_id, EventType.EVIDENCE_ADDED, evidence_payload)
        store.append_event(run.run_id, EventType.FINDING_ADDED, finding_payload)

        # Read events (transparently rehydrated)
        events = store.read_events(run.run_id)
        assert len(events) == 3

        state = project(run.run_id, events)
        assert state.status.value == "valid"
        ev_map = {e.evidence_id: e for e in state.evidence}
        assert "ev_large_1" in ev_map
        assert ev_map["ev_large_1"].summary == evidence_payload["summary"]

        find_map = {f.finding_id: f for f in state.findings}
        assert "find_large_1" in find_map
        assert find_map["find_large_1"].claim == finding_payload["claim"]

        validator = StateValidator()
        outcome = validator.validate(state)
        assert outcome.safe
        assert outcome.state.status.value == "valid"


def test_compaction_preserves_blobs_and_rehydrates_archive(tmp_path: Path) -> None:
    """Compaction moves event records to archive without deleting blobs, and read_archived_events rehydrates."""
    db_file = tmp_path / "compact_blob.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_compact", goal="test compaction blob preservation")
        store.create_run_started(run)

        payload1 = {"item": "first", "data": "1" * 100}
        payload2 = {"item": "second", "data": "2" * 100}
        payload3 = {"item": "third", "data": "3" * 100}

        ev1 = store.append_event(run.run_id, EventType.WORK_COMPLETED, payload1)
        ev2 = store.append_event(run.run_id, EventType.WORK_COMPLETED, payload2)
        ev3 = store.append_event(run.run_id, EventType.WORK_COMPLETED, payload3)

        blob1 = tmp_path / "blobs" / f"{ev1.payload[OFFLOAD_KEY]}.blob"
        blob2 = tmp_path / "blobs" / f"{ev2.payload[OFFLOAD_KEY]}.blob"
        blob3 = tmp_path / "blobs" / f"{ev3.payload[OFFLOAD_KEY]}.blob"

        assert blob1.exists()
        assert blob2.exists()
        assert blob3.exists()

        # Compact run through sequence 3
        store.compact_run(run.run_id, through_sequence=3)

        # Blobs on disk must remain preserved
        assert blob1.exists()
        assert blob2.exists()
        assert blob3.exists()

        # read_archived_events rehydrates payloads
        archived = store.read_archived_events(run.run_id)
        assert len(archived) >= 3
        arch_seq_map = {e.sequence: e.payload for e in archived}
        assert arch_seq_map[ev1.sequence] == payload1
        assert arch_seq_map[ev2.sequence] == payload2

        # read_archived_events with rehydrate=False returns descriptors
        raw_archived = store.read_archived_events(run.run_id, rehydrate=False)
        raw_map = {e.sequence: e.payload for e in raw_archived}
        assert is_offload_descriptor(raw_map[ev1.sequence])

        # Deep verification on compacted run audits both archive and live blobs
        report = store.verify_events(run.run_id, deep=True)
        assert report.ok

        # Delete an archived blob: shallow verification still passes
        blob1.unlink()
        shallow_report = store.verify_events(run.run_id, deep=False)
        assert shallow_report.ok

        # Deep verification catches missing archived blob
        deep_report = store.verify_events(run.run_id, deep=True)
        assert not deep_report.ok
        assert any(
            v.kind == "BLOB_MISSING" and v.sequence == ev1.sequence and "archived" in v.detail
            for v in deep_report.violations
        )


def test_verify_events_deep_flag(tmp_path: Path) -> None:
    """verify_events with deep=True checks on-disk blobs; shallow checks only hash chain."""
    db_file = tmp_path / "verify_deep.db"
    threshold = 50

    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=threshold,
    ) as store:
        run = Run(run_id="run_vdeep", goal="test deep verify flag")
        store.create_run_started(run)

        large_payload = {"data": "q" * 120}
        ev = store.append_event(run.run_id, EventType.TOOL_COMPLETED, large_payload)
        sha256_hex = ev.payload[OFFLOAD_KEY]
        blob_path = tmp_path / "blobs" / f"{sha256_hex}.blob"
        assert blob_path.exists()

        # Deep verify clean run
        report = store.verify_events(run.run_id, deep=True)
        assert report.ok
        assert len(report.violations) == 0

        # Delete blob file
        blob_path.unlink()

        # Shallow verify passes (database event chain hash is unchanged)
        shallow_report = store.verify_events(run.run_id, deep=False)
        assert shallow_report.ok

        # Deep verify detects missing blob
        deep_report = store.verify_events(run.run_id, deep=True)
        assert not deep_report.ok
        assert len(deep_report.violations) == 1
        violation = deep_report.violations[0]
        assert violation.kind == "BLOB_MISSING"
        assert violation.sequence == ev.sequence
        assert sha256_hex in violation.detail

        # Restore blob with tampered contents
        blob_path.write_bytes(b"tampered content")

        tamper_report = store.verify_events(run.run_id, deep=True)
        assert not tamper_report.ok
        assert len(tamper_report.violations) == 1
        tamper_violation = tamper_report.violations[0]
        assert tamper_violation.kind == "BLOB_DIGEST_MISMATCH"
        assert tamper_violation.sequence == ev.sequence
        assert sha256_hex in tamper_violation.detail


def test_cli_verify_deep(tmp_path: Path) -> None:
    """CLI continuum verify --deep reports deep blob verification status and exits corrupted on failure."""
    db_file = tmp_path / "cli_deep.db"
    with SQLiteStorage(
        f"sqlite:///{db_file}",
        storage_dir=tmp_path,
        payload_offload_bytes=50,
    ) as store:
        run_obj = Run(run_id="run_cli_deep", goal="test cli deep verify")
        store.create_run_started(run_obj)
        ev = store.append_event(run_obj.run_id, EventType.TOOL_COMPLETED, {"data": "d" * 120})
        sha256_hex = ev.payload[OFFLOAD_KEY]
        blob_path = tmp_path / "blobs" / f"{sha256_hex}.blob"
        assert blob_path.exists()

    def run_cli(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    # Successful deep verify
    code, out, _ = run_cli("--db", str(db_file), "verify", "run_cli_deep", "--deep")
    assert code == ExitCode.OK
    assert "Deep blob integrity verified." in out

    # Successful deep verify in JSON mode
    code, out, _ = run_cli("--db", str(db_file), "--json", "verify", "run_cli_deep", "--deep")
    assert code == ExitCode.OK
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["deep"] is True

    # Tamper with blob
    blob_path.write_bytes(b"tampered")

    code, out, _ = run_cli("--db", str(db_file), "verify", "run_cli_deep", "--deep")
    assert code == ExitCode.CORRUPTED
    assert "INTEGRITY FAILURE" in out
    assert "BLOB_DIGEST_MISMATCH" in out

    # JSON output on failure
    code, out, _ = run_cli("--db", str(db_file), "--json", "verify", "run_cli_deep", "--deep")
    assert code == ExitCode.CORRUPTED
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["deep"] is True
    assert any(v["kind"] == "BLOB_DIGEST_MISMATCH" for v in payload["violations"])


def test_postgres_rehydration_and_deep_verify_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgresStorage rehydrates payloads and audits blobs during deep verification."""
    mock_psycopg = MagicMock()
    mock_rows = MagicMock()
    mock_psycopg.rows = mock_rows
    mock_conn = MagicMock()
    mock_psycopg.connect.return_value = mock_conn

    monkeypatch.setitem(sys.modules, "psycopg", mock_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", mock_rows)

    with patch("continuum.storage.postgres._require_psycopg", return_value=mock_psycopg):
        store = PostgresStorage(
            "postgresql://localhost/dummy",
            storage_dir=tmp_path,
            payload_offload_bytes=30,
        )

        # Write a real blob to tmp_path
        payload_data = {"key": "pg_value" * 10}
        canonical_bytes = to_json(payload_data).encode("utf-8")
        blob_path = write_blob(tmp_path, canonical_bytes)
        sha256_hex = blob_path.stem

        descriptor = create_offload_descriptor(
            sha256_hex, len(canonical_bytes), payload_data.keys()
        )

        mock_row = {
            "event_id": "ev_pg_1",
            "run_id": "run_pg",
            "sequence": 1,
            "type": "TOOL_COMPLETED",
            "timestamp": "2026-09-28T00:00:00Z",
            "payload": descriptor,
            "causer_event_id": None,
            "source": "deterministic",
            "prev_hash": None,
            "hash": "somehash",
        }

        # _row_to_event rehydrates
        ev_rehydrated = store._row_to_event(mock_row, rehydrate=True)
        assert ev_rehydrated.payload == payload_data

        # _row_to_event with rehydrate=False returns descriptor
        ev_raw = store._row_to_event(mock_row, rehydrate=False)
        assert ev_raw.payload == descriptor

        # read_events mock
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [mock_row]
        mock_conn.execute.return_value = mock_cursor

        events = store.read_events("run_pg")
        assert len(events) == 1
        assert events[0].payload == payload_data

        raw_events = store.read_events("run_pg", rehydrate=False)
        assert raw_events[0].payload == descriptor

        # Corrupted blob during _row_to_event rehydration raises CorruptedRecord
        blob_path.unlink()
        with pytest.raises(CorruptedRecord, match="missing or corrupt blob"):
            store._row_to_event(mock_row, rehydrate=True)
