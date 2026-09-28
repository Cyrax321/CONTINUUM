"""Tests for out-of-band blob storage and payload offloading (issues #254, #1418)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run, utcnow
from continuum.security.hashing import to_json
from continuum.storage import (
    CorruptedRecord,
    SQLiteStorage,
    open_storage,
)
from continuum.storage.blob import (
    DEFAULT_PAYLOAD_OFFLOAD_BYTES,
    OFFLOAD_KEY,
    PAYLOAD_OFFLOAD_ENV_VAR,
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
