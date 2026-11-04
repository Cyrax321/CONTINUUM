"""Coverage for rewind file-snapshot conflicts, failures, and fallbacks (#1950)."""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.checkpoint.manager import CheckpointManager
from continuum.checkpoint.rewind import (
    RewindError,
    resolve_checkpoint,
    rewind_to_checkpoint,
)
from continuum.clienthooks import observe_event_payload
from continuum.environment.file_snapshot import (
    file_digest,
    snapshot_file,
    snapshot_path,
)
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_resolve_checkpoint_lookup_fallthroughs(tmp_path: Path) -> None:
    """Cover target-lookup fall-throughs in resolve_checkpoint."""
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run_1", goal="test 1"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "test 1"})
    storage.append_event("run_1", EventType.TASK_UPDATED, {"completed": 1, "total": 2})
    cp1 = CheckpointManager(storage).checkpoint("run_1")
    assert cp1.version == 0
    assert cp1.state.source_sequence == 2

    storage.create_run(Run(run_id="run_2", goal="test 2"))
    storage.append_event("run_2", EventType.RUN_STARTED, {"goal": "test 2"})
    cp2 = CheckpointManager(storage).checkpoint("run_2")

    # Target checkpoint belongs to another run (line 71 -> 79).
    with pytest.raises(RewindError, match=f"no checkpoint {cp2.checkpoint_id!r} for run 'run_1'"):
        resolve_checkpoint(storage, "run_1", cp2.checkpoint_id)

    # Target is an integer matching source_sequence rather than checkpoint version (line 86).
    resolved_by_seq = resolve_checkpoint(storage, "run_1", str(cp1.state.source_sequence))
    assert resolved_by_seq.checkpoint_id == cp1.checkpoint_id

    # Target is an integer version that matches no version or source sequence (lines 81-86 -> 89).
    with pytest.raises(RewindError, match="no checkpoint '999' for run 'run_1'"):
        resolve_checkpoint(storage, "run_1", "999")

    # Corrupted checkpoint record in storage raises CorruptedRecord (lines 73-76).
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE checkpoints SET body = ? WHERE checkpoint_id = ?",
        ('{"broken": "trunc', cp1.checkpoint_id),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RewindError, match="is corrupted and cannot be trusted"):
        resolve_checkpoint(storage, "run_1", cp1.checkpoint_id)

    storage.close()


def test_observation_filtering_guards_ignore_non_path_events(tmp_path: Path) -> None:
    """Cover lines 148->145 and 154->151: non-string, missing, or empty paths in tool events."""
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    run_id = "run_filter"
    storage.create_run(Run(run_id=run_id, goal="filtering"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "filtering"})

    # Pre-checkpoint events with missing, non-string, or empty paths.
    storage.append_event(run_id, EventType.TOOL_COMPLETED, {"result": "no path"})
    storage.append_event(run_id, EventType.TOOL_COMPLETED, {"path": 12345})
    storage.append_event(run_id, EventType.TOOL_COMPLETED, {"path": ""})

    cp = CheckpointManager(storage).checkpoint(run_id)

    # Post-checkpoint events with missing, non-string, or empty paths.
    storage.append_event(run_id, EventType.TOOL_COMPLETED, {"result": "no path after"})
    storage.append_event(run_id, EventType.TOOL_COMPLETED, {"path": ["not", "string"]})
    storage.append_event(run_id, EventType.TOOL_COMPLETED, {"path": ""})
    storage.close()

    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    assert result.ok
    assert result.reverted_files == ()
    assert result.deleted_files == ()
    assert result.conflicts == ()
    assert result.unrecoverable == ()
    storage.close()


def test_missing_file_triage_unrecoverable_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover lines 192-200: missing-file triage for unrecoverable and conflict cases."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    run_id = "run_triage"
    storage.create_run(Run(run_id=run_id, goal="triage"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "triage"})

    path_unrec = str(tmp_path / "unrec.txt")
    path_conflict = str(tmp_path / "conflict.txt")

    # Pre-checkpoint: unrec has no digest; conflict has digest.
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"path": path_unrec, "tool_name": "Write"},
    )
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"path": path_conflict, "sha256": "1" * 64, "tool_name": "Write"},
    )

    cp = CheckpointManager(storage).checkpoint(run_id)

    # Post-checkpoint: both observed with expected digests.
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"path": path_unrec, "sha256": "2" * 64, "tool_name": "Edit"},
    )
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {"path": path_conflict, "sha256": "3" * 64, "tool_name": "Edit"},
    )
    storage.close()

    # Neither file exists on disk (current_digest is None).
    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)
    assert not result.ok

    # Lines 192-196: unrecoverable when pre-checkpoint observation had no digest.
    assert any("checkpoint-time observation has no digest" in u for u in result.unrecoverable)
    # Lines 197-200: conflict when pre-checkpoint observation had a digest.
    assert any("file missing but last observed digest" in c for c in result.conflicts)
    storage.close()


def test_forced_delete_reverification_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover lines 253-262: forced delete re-verification failures and OSError."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    run_id = "run_delete_failures"
    storage.create_run(Run(run_id=run_id, goal="delete failures"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "delete failures"})

    cp = CheckpointManager(storage).checkpoint(run_id)

    # Files created after checkpoint.
    f_changed = tmp_path / "del_changed.txt"
    f_failed = tmp_path / "del_failed.txt"
    f_oserror = tmp_path / "del_oserror.txt"

    f_changed.write_text("initial changed", encoding="utf-8")
    f_failed.write_text("initial failed", encoding="utf-8")
    f_oserror.write_text("initial oserror", encoding="utf-8")

    p_changed = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(f_changed)}}
    )
    p_failed = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(f_failed)}}
    )
    p_oserror = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(f_oserror)}}
    )

    storage.append_event(run_id, EventType.TOOL_COMPLETED, p_changed)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, p_failed)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, p_oserror)
    storage.close()

    # Modify f_changed right before rewind execution so digest changed before delete (lines 253-255).
    orig_file_digest = file_digest
    digest_call_count = 0

    def mock_file_digest(p: str | Path) -> str | None:
        nonlocal digest_call_count
        if str(p) == str(f_changed):
            digest_call_count += 1
            # Return true digest during planning (count == 1), then changed digest during execution.
            if digest_call_count > 1:
                return "0" * 64
        return orig_file_digest(p)

    monkeypatch.setattr("continuum.checkpoint.rewind.file_digest", mock_file_digest)

    # Monkeypatch unlink to simulate failed to delete (p.exists() True) and OSError.
    orig_unlink = Path.unlink

    def mock_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == f_failed:
            # No-op unlink: file still exists (lines 257-259).
            return
        if self == f_oserror:
            # Raise OSError: lines 261-262.
            raise OSError("permission denied")
        orig_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", mock_unlink)

    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)

    assert any("digest changed before delete" in c for c in result.conflicts)
    assert any("failed to delete" in c for c in result.conflicts)
    assert any("delete failed: permission denied" in c for c in result.conflicts)
    storage.close()


def test_planned_reverts_execution_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover lines 271-292: snapshot missing, digest changed, restore failed, and digest mismatch."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    run_id = "run_revert_failures"
    storage.create_run(Run(run_id=run_id, goal="revert failures"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "revert failures"})

    f_no_snap = tmp_path / "no_snap.txt"
    f_changed = tmp_path / "revert_changed.txt"
    f_failed = tmp_path / "revert_failed.txt"
    f_mismatch = tmp_path / "revert_mismatch.txt"

    for f in (f_no_snap, f_changed, f_failed, f_mismatch):
        f.write_text(f"before checkpoint {f.name}", encoding="utf-8")
        payload = observe_event_payload({"tool_name": "Write", "tool_input": {"file_path": str(f)}})
        snapshot_file(f, sha256=payload.get("sha256"))
        storage.append_event(run_id, EventType.TOOL_COMPLETED, payload)

    cp = CheckpointManager(storage).checkpoint(run_id)

    for f in (f_no_snap, f_changed, f_failed, f_mismatch):
        f.write_text(f"after checkpoint {f.name}", encoding="utf-8")
        payload = observe_event_payload({"tool_name": "Edit", "tool_input": {"file_path": str(f)}})
        snapshot_file(f, sha256=payload.get("sha256"))
        storage.append_event(run_id, EventType.TOOL_COMPLETED, payload)

    storage.close()

    # Case 1 (lines 271-275): delete snapshot for f_no_snap before rewind.
    before_digest_no_snap = file_digest(tmp_path / "does_not_matter")  # dummy
    # Lookup the before digest of f_no_snap from events.
    storage = SQLiteStorage(db)
    events = storage.read_all_events(run_id)
    for e in events:
        if e.sequence <= cp.state.source_sequence and e.payload.get("path") == str(f_no_snap):
            before_digest_no_snap = e.payload.get("sha256")
            break
    assert before_digest_no_snap is not None
    snap_path = snapshot_path(before_digest_no_snap)
    if snap_path.exists():
        snap_path.unlink()

    # Case 2 (lines 281-283): digest changed before restore.
    # Case 4 (lines 287-292): after restore digest != expected.
    orig_file_digest = file_digest
    digest_calls: dict[str, int] = {}

    def mock_file_digest(p: str | Path) -> str | None:
        p_str = str(p)
        digest_calls[p_str] = digest_calls.get(p_str, 0) + 1
        if p_str == str(f_changed) and digest_calls[p_str] > 1:
            return "4" * 64
        if p_str == str(f_mismatch) and digest_calls[p_str] >= 3:
            return "5" * 64
        return orig_file_digest(p)

    monkeypatch.setattr("continuum.checkpoint.rewind.file_digest", mock_file_digest)

    # Case 3 (lines 284-286): restore_file fails.
    from continuum.checkpoint import rewind as rewind_module

    orig_restore = rewind_module.restore_file

    def mock_restore(dest: str | Path, sha256: str) -> bool:
        if str(dest) == str(f_failed):
            return False
        return orig_restore(dest, sha256)

    monkeypatch.setattr("continuum.checkpoint.rewind.restore_file", mock_restore)

    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)

    assert any("no snapshot for digest" in u for u in result.unrecoverable)
    assert any("digest changed before restore" in c for c in result.conflicts)
    assert any("restore failed for" in u for u in result.unrecoverable)
    assert any("after restore digest" in c for c in result.conflicts)
    storage.close()


def test_execution_pass_rewind_error_when_force_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover line 295: RewindError raised when execution discovers unrecoverable with force=False."""
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    run_id = "run_exec_error"
    storage.create_run(Run(run_id=run_id, goal="exec error"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "exec error"})

    f_a = tmp_path / "a.txt"
    f_a.write_text("before", encoding="utf-8")
    payload_before = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(f_a)}}
    )
    snapshot_file(f_a, sha256=payload_before.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_before)

    cp = CheckpointManager(storage).checkpoint(run_id)

    f_a.write_text("after", encoding="utf-8")
    payload_after = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(f_a)}}
    )
    snapshot_file(f_a, sha256=payload_after.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_after)
    storage.close()

    # Delete the checkpoint-era snapshot so planning succeeds (cleanly planned revert)
    # but execution discovers the missing snapshot.
    before_digest = payload_before.get("sha256")
    assert isinstance(before_digest, str)
    snap_path = snapshot_path(before_digest)
    if snap_path.exists():
        snap_path.unlink()

    storage = SQLiteStorage(db)
    with pytest.raises(RewindError, match="has 0 conflict\\(s\\) and 1 unrecoverable file\\(s\\)"):
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=False)
    storage.close()


def test_recovery_engine_failure_fallback_returns_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover lines 315-317: RecoveryEngine failure fallback sets mode to unknown and safe to False."""
    db = str(tmp_path / "rewind.db")
    storage = SQLiteStorage(db)
    run_id = "run_engine_fallback"
    storage.create_run(Run(run_id=run_id, goal="fallback"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "fallback"})
    cp = CheckpointManager(storage).checkpoint(run_id)
    storage.close()

    from continuum.recovery.engine import RecoveryEngine

    def mock_assess(self: RecoveryEngine, run_id: str) -> None:
        raise RuntimeError("simulated engine assessment crash")

    monkeypatch.setattr(RecoveryEngine, "assess", mock_assess)

    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    assert result.resume_mode == "unknown"
    assert result.resume_safe is False
    storage.close()
