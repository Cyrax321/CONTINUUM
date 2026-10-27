"""Atomic dual-state rewind (issue #292): 6 acceptance criteria."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from continuum.checkpoint.rewind import RewindError, resolve_checkpoint, rewind_to_checkpoint
from continuum.cli import main
from continuum.cli.exitcodes import ExitCode
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_rewind_reverts_hook_tracked_writes(tmp_path: Path) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_rewind_1"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="test rewind"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.close()
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("version at checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_a = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a)
    from continuum.checkpoint.manager import CheckpointManager

    manager = CheckpointManager(storage)
    cp = manager.checkpoint(run_id)
    checkpoint_id = cp.checkpoint_id
    storage.close()
    file_b = workdir / "b.txt"
    file_b.write_text("new file after checkpoint", encoding="utf-8")
    payload_b = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_b)}}
    )
    snapshot_file(file_b, sha256=payload_b.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_b)
    file_a.write_text("modified after checkpoint", encoding="utf-8")
    payload_a2 = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a2.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a2)
    storage.close()
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "rewind", run_id, "--to", checkpoint_id], out=out, err=err)
    assert code == ExitCode.OK, f"rewind failed: {err.getvalue()} {out.getvalue()}"
    assert file_a.read_text(encoding="utf-8") == "version at checkpoint"
    assert not file_b.exists()


def test_external_modifications_detected_as_conflicts(tmp_path: Path) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_conflict"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="conflict"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "conflict"})
    storage.close()
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("base", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_a = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a)
    from continuum.checkpoint.manager import CheckpointManager

    manager = CheckpointManager(storage)
    cp = manager.checkpoint(run_id)
    storage.close()
    file_a.write_text("after", encoding="utf-8")
    payload_after = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_after.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_after)
    storage.close()
    file_a.write_text("external tamper", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "rewind", run_id, "--to", cp.checkpoint_id], out=out, err=err)
    assert code == ExitCode.ERROR
    assert "conflict" in err.getvalue().lower() or "conflict" in out.getvalue().lower()
    assert file_a.read_text(encoding="utf-8") == "external tamper"


def test_post_rewind_state_passes_validation(tmp_path: Path) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_validate"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="validate"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "validate"})
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("v1", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_a = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a)
    from continuum.checkpoint.manager import CheckpointManager

    manager = CheckpointManager(storage)
    cp = manager.checkpoint(run_id)
    storage.close()
    file_a.write_text("v2", encoding="utf-8")
    payload_a2 = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a2.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a2)
    storage.close()
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "rewind", run_id, "--to", cp.checkpoint_id], out=out, err=err)
    assert code == ExitCode.OK
    out2, err2 = io.StringIO(), io.StringIO()
    code2 = main(["--db", db, "validate", run_id], out=out2, err=err2)
    assert code2 in (ExitCode.OK, 10, 20, 30)
    out3, err3 = io.StringIO(), io.StringIO()
    main(["--db", db, "--json", "resume", run_id], out=out3, err=err3)
    data = json.loads(out3.getvalue())
    assert data["run_id"] == run_id


def test_untracked_file_restore_or_listed(tmp_path: Path) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_untracked"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="untracked"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "untracked"})
    from continuum.checkpoint.manager import CheckpointManager

    manager = CheckpointManager(storage)
    cp = manager.checkpoint(run_id)
    storage.close()
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_b = workdir / "new.txt"
    file_b.write_text("new", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_b = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_b)}}
    )
    snapshot_file(file_b, sha256=payload_b.get("sha256"))
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_b)
    storage.close()
    file_b.unlink()
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "rewind", run_id, "--to", cp.checkpoint_id], out=out, err=err)
    assert code == ExitCode.OK
    assert not file_b.exists()


def test_integration_hard_kill_and_rewind(tmp_path: Path) -> None:
    import subprocess
    import sys

    db = str(tmp_path / "integration.db")
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    storage = SQLiteStorage(db)
    run_id = "run_integration"
    storage.create_run(Run(run_id=run_id, goal="integration"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "integration"})
    file_a.write_text("before", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_a = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a)
    from continuum.checkpoint.manager import CheckpointManager

    manager = CheckpointManager(storage)
    cp = manager.checkpoint(run_id)
    storage.close()
    script = f"""
import os
from pathlib import Path
from continuum.storage import SQLiteStorage
from continuum.events import EventType
from continuum.clienthooks import observe_event_payload
from continuum.environment.file_snapshot import snapshot_file
db = r"{db}"
workdir = Path(r"{workdir}")
file_a = workdir / "a.txt"
file_a.write_text("after", encoding="utf-8")
payload = observe_event_payload({{"tool_name": "Edit", "tool_input": {{"file_path": str(file_a)}}}})
snapshot_file(file_a, sha256=payload.get("sha256"))
storage = SQLiteStorage(db)
storage.append_event("{run_id}", EventType.TOOL_COMPLETED, payload)
storage.close()
os._exit(0)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert proc.returncode == 0
    assert file_a.read_text(encoding="utf-8") == "after"
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "rewind", run_id, "--to", cp.checkpoint_id], out=out, err=err)
    assert code == ExitCode.OK
    assert file_a.read_text(encoding="utf-8") == "before"
    out2, err2 = io.StringIO(), io.StringIO()
    main(["--db", db, "--json", "resume", run_id], out=out2, err=err2)
    data = json.loads(out2.getvalue())
    assert data["run_id"] == run_id


def test_docs_updated() -> None:
    root = Path(__file__).resolve().parents[1]
    arch = (root / "references/architecture.md").read_text(encoding="utf-8")
    cli = (root / "references/cli.md").read_text(encoding="utf-8")
    assert "rewind" in arch.lower()
    assert "rewind" in cli.lower()


def test_rewind_unknown_checkpoint_raises_rewind_error(tmp_path: Path) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_unknown_cp"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="unknown cp"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})

    with pytest.raises(RewindError, match=r"no checkpoint 'nonexistent' for run 'run_unknown_cp'"):
        resolve_checkpoint(storage, run_id, "nonexistent")

    with pytest.raises(RewindError, match=r"no checkpoint 'nonexistent' for run 'run_unknown_cp'"):
        rewind_to_checkpoint(storage, run_id, "nonexistent")
    storage.close()


def test_rewind_conflicts_without_force_raises_rewind_error(tmp_path: Path) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_conflict_raise"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="conflict raise"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "conflict raise"})

    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("base content", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_a = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a)

    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)

    file_a.write_text("modified after cp", encoding="utf-8")
    payload_a2 = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_a2.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_a2)

    # External tamper causing digest mismatch
    file_a.write_text("external tamper", encoding="utf-8")

    # Without force: raises RewindError
    with pytest.raises(RewindError, match="conflict"):
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=False)

    # With force: succeeds without raising RewindError
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)
    assert len(result.conflicts) > 0
    assert not result.ok
    storage.close()


def test_rewind_precondition_unexpected_error_raises_rewind_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "rewind.db")
    run_id = "run_precondition_err"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="precondition err"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})

    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)

    import continuum.recovery.gate as gate_mod

    def _broken_preconditions(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated gate precondition failure")

    monkeypatch.setattr(gate_mod, "check_preconditions", _broken_preconditions)

    with pytest.raises(RewindError, match="simulated gate precondition failure") as exc_info:
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    storage.close()


def test_rewind_reports_a_poisoned_digest_rather_than_following_it(
    tmp_path: Path,
) -> None:
    # Issue #1268: a TOOL_COMPLETED payload that records a digest its writer did
    # not compute reaches snapshot_path on the read side, and a traversal string
    # there names a file outside .continuum/file-snapshots. Rewind must report
    # that as unrecoverable instead of copying whatever the path points at over
    # a workspace file -- the command whose purpose is to restore trusted content
    # must not become a file-read primitive.
    db = str(tmp_path / "rewind.db")
    run_id = "run_poisoned_digest"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="poisoned digest"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_before = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    poison = "../../../../evil"
    # The write side already refuses to file content under a key that is not its
    # digest, so a poisoned payload can only live in the log, never in the store.
    assert snapshot_file(file_a, sha256=poison) is None
    payload_before["sha256"] = poison
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_before)
    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)
    # The file is unchanged after the checkpoint, so the observed digest matches
    # the payload and no conflict diverts the flow away from the restore branch.
    payload_after = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_after)

    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)

    assert any("not a SHA-256 digest" in m for m in result.unrecoverable), (
        f"poisoned digest should be reported, got {result.unrecoverable}"
    )
    # The workspace file was never overwritten with out-of-store content.
    assert file_a.read_text(encoding="utf-8") == "at checkpoint"
    storage.close()


def test_rewind_pre_checkpoint_observation_without_digest_is_never_a_delete(
    tmp_path: Path,
) -> None:
    # Issue #1806: a pre-checkpoint TOOL_COMPLETED that records the path but no
    # digest (the PostToolUse hook races the writer, the file can be held
    # exclusively or unreadable, so observe_event_payload records the path
    # without bytes/sha256) used to take the "created after the checkpoint"
    # branch and unlink a file the checkpoint-era log recorded a write to, with
    # zero conflicts and ok=True. The observation is a weaker fact, not an
    # absent one: the file was already there and cannot be restored, so it must
    # be left in place and reported, never deleted.
    db = str(tmp_path / "rewind.db")
    run_id = "run_digestless_before"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="digestless before"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("content at checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload

    payload_before = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    # The hook read failed, so the observation carries the path only.
    payload_before.pop("sha256", None)
    payload_before.pop("bytes", None)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_before)
    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)
    payload_after = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_after)
    storage.close()

    # Without force the rewind refuses rather than silently losing the file.
    storage = SQLiteStorage(db)
    with pytest.raises(RewindError, match="unrecoverable"):
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)
    assert any("no digest" in m for m in result.unrecoverable), (
        f"digest-less observation should be reported, got {result.unrecoverable}"
    )
    # The file is still on disk, untouched: never a delete.
    assert file_a.read_text(encoding="utf-8") == "content at checkpoint"
    assert result.deleted_files == ()
    storage.close()


def test_rewind_post_checkpoint_observation_without_digest_requires_force(
    tmp_path: Path,
) -> None:
    # Issue #1854 supersedes the #1806 resolution here. #1806 made a
    # digest-less post-checkpoint observation delete unconditionally, but a
    # digest the observation does not carry is exactly the evidence that the
    # file was created after the checkpoint, and without it the checkpoint-era
    # record says nothing about the path. Unlinking it can therefore delete a
    # file that pre-existed the checkpoint, so the rewind refuses and only
    # --force authorizes the risk.
    db = str(tmp_path / "rewind.db")
    run_id = "run_digestless_after"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="digestless after"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)
    storage.close()
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_b = workdir / "b.txt"
    file_b.write_text("created after checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload

    payload_b = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_b)}}
    )
    payload_b.pop("sha256", None)
    payload_b.pop("bytes", None)
    storage = SQLiteStorage(db)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_b)
    storage.close()

    storage = SQLiteStorage(db)
    with pytest.raises(RewindError, match="deletion requires --force"):
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    assert file_b.exists(), " refusal must leave the unverified file in place"
    storage.close()

    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)
    assert result.ok, f"conflicts={result.conflicts} unrecoverable={result.unrecoverable}"
    assert result.deleted_files == (str(file_b),)
    assert not file_b.exists()
    storage.close()


def test_rewind_missing_digestless_pre_checkpoint_file_is_unrecoverable(
    tmp_path: Path,
) -> None:
    # Issue #1854 case 2: a path with a digest-less pre-checkpoint observation
    # whose file is missing from disk used to be appended to deleted_files and
    # reported as ok=True. No deletion happened and no restore is possible, so
    # claiming the path as deleted reported work the rewind never did and hid
    # the path behind a clean verdict. It is unrecoverable instead.
    db = str(tmp_path / "rewind.db")
    run_id = "run_digestless_missing"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="digestless missing"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_c = workdir / "c.txt"
    file_c.write_text("at checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload

    payload_before = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_c)}}
    )
    payload_before.pop("sha256", None)
    payload_before.pop("bytes", None)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_before)
    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)
    file_c.unlink()
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        observe_event_payload({"tool_name": "Edit", "tool_input": {"file_path": str(file_c)}}),
    )
    storage.close()

    storage = SQLiteStorage(db)
    with pytest.raises(RewindError, match="cannot restore"):
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    storage.close()

    # Forced past the refusal, the path is still reported as unrecoverable and
    # never as deleted: nothing was unlinked and nothing was restored.
    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id, force=True)
    assert not result.ok
    assert result.deleted_files == ()
    assert any("cannot restore" in note for note in result.unrecoverable), (
        f"unrecoverable={result.unrecoverable}"
    )
    storage.close()


def test_rewind_classifies_before_mutating_any_file(tmp_path: Path) -> None:
    # Issue #1854 case 3: the digest-less paths are classified before the
    # filesystem is touched. An earlier deletable path used to be unlinked
    # before a later digest-less path raised RewindError, leaving the workspace
    # half-rewound under a failure exit. The refusal must happen with every
    # file still in place.
    db = str(tmp_path / "rewind.db")
    run_id = "run_preflight"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="preflight rewind"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)
    storage.close()
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_deletable = workdir / "deletable.txt"
    file_deletable.write_text("created after checkpoint", encoding="utf-8")
    file_unverified = workdir / "unverified.txt"
    file_unverified.write_text("also created after checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload

    payload_deletable = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_deletable)}}
    )
    payload_unverified = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_unverified)}}
    )
    payload_unverified.pop("sha256", None)
    payload_unverified.pop("bytes", None)
    storage = SQLiteStorage(db)
    # The digest-less observation is appended first so the old single-pass loop
    # processed and unlinked the deletable file before the refusal fired.
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_unverified)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_deletable)
    storage.close()

    storage = SQLiteStorage(db)
    with pytest.raises(RewindError, match="deletion requires --force"):
        rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    assert file_deletable.exists(), (
        "a refusal must not leave earlier deletable files already unlinked"
    )
    assert file_unverified.exists()
    storage.close()


def test_rewind_restores_when_post_checkpoint_observation_has_no_digest(
    tmp_path: Path,
) -> None:
    # Issue #1806, the restore branch of the same comparison: a digest-bearing
    # pre-checkpoint observation plus a digest-less post-checkpoint one made
    # `file_digest(path) != expected_after` compare a real digest against None,
    # conflict forever, and never restore the file even with --force. Without
    # an observed post-checkpoint digest there is nothing to compare the
    # current content against, and the restore itself is still verified against
    # the snapshot digest afterwards.
    db = str(tmp_path / "rewind.db")
    run_id = "run_digestless_restore"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="digestless restore"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    workdir = tmp_path / "work"
    workdir.mkdir()
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    from continuum.clienthooks import observe_event_payload
    from continuum.environment.file_snapshot import snapshot_file

    payload_before = observe_event_payload(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_a)}}
    )
    snapshot_file(file_a, sha256=payload_before.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_before)
    from continuum.checkpoint.manager import CheckpointManager

    cp = CheckpointManager(storage).checkpoint(run_id)
    file_a.write_text("modified after checkpoint", encoding="utf-8")
    payload_after = observe_event_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": str(file_a)}}
    )
    payload_after.pop("sha256", None)
    payload_after.pop("bytes", None)
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload_after)
    storage.close()

    storage = SQLiteStorage(db)
    result = rewind_to_checkpoint(storage, run_id, cp.checkpoint_id)
    assert result.ok, f"conflicts={result.conflicts} unrecoverable={result.unrecoverable}"
    assert file_a.read_text(encoding="utf-8") == "at checkpoint"
    assert result.reverted_files == (str(file_a),)
    storage.close()
