"""Rewind's failure reporting (issue #1950): the branches that exist so a
rewind *reports* a problem instead of silently destroying data.

Almost every branch driven below was added by a real fix (#1268, #1854,
#1059) and a regression in any of them would look like a clean rewind while
files are mis-handled. The happy paths live in test_rewind_dual_state.py,
test_rewind_carry_forward.py and test_rewind_compacted.py; this file covers
the target-lookup fall-throughs, the missing-file triage, the forced-delete
re-verification, the restore failures, the aggregation that raises without
--force, and the recovery-assessment fallback.

The snapshot store is CWD-relative, so every test chdirs into its own
tmp_path: no snapshot leaks between tests or into the checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from continuum.models import Run, StateCheckpoint
from continuum.storage import SQLiteStorage
from continuum.storage.base import CorruptedRecord

#: A syntactically valid digest that cannot be any file's real one.
_OTHER_DIGEST = "a" * 64


def _observe(
    storage: SQLiteStorage,
    run_id: str,
    path: Path,
    *,
    digest: bool = True,
    snapshot: bool = True,
) -> dict[str, Any]:
    """Append one TOOL_COMPLETED observation of ``path``.

    ``digest=False`` models the hook that read no bytes (a racing writer, an
    unreadable file): the observation carries the path, not the digest
    (#1806). ``snapshot=False`` models a write the store refused to file --
    too large or unreadable at checkpoint time -- so the revert later finds
    no blob for the digest it observed.
    """
    payload = observe_event_payload({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
    if not digest:
        payload.pop("sha256", None)
        payload.pop("bytes", None)
    elif snapshot:
        snapshot_file(path, sha256=payload.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload)
    return payload


def _seed(tmp_path: Path, run_id: str) -> tuple[SQLiteStorage, Path]:
    """an empty run with a workspace dir; the tests add observations and the
    checkpoint themselves."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    storage = SQLiteStorage(str(tmp_path / "rewind.db"))
    storage.create_run(Run(run_id=run_id, goal=run_id))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return storage, workdir


def _checkpoint(storage: SQLiteStorage, run_id: str) -> StateCheckpoint:
    return CheckpointManager(storage).checkpoint(run_id)


# --------------------------------------------------------------------------- #
# target lookup
# --------------------------------------------------------------------------- #


def test_a_checkpoint_of_another_run_falls_through_to_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_checkpoint`` answers by id across runs: an id that resolves to a
    *different* run's checkpoint must keep looking, not be accepted (#1059)."""
    monkeypatch.chdir(tmp_path)
    storage, _ = _seed(tmp_path, "run_a")
    cp_a = _checkpoint(storage, "run_a")
    storage.create_run(Run(run_id="run_b", goal="run_b"))
    storage.append_event("run_b", EventType.RUN_STARTED, {"goal": "g"})
    _checkpoint(storage, "run_b")

    with pytest.raises(RewindError, match="no checkpoint .* for run 'run_b'"):
        resolve_checkpoint(storage, "run_b", cp_a.checkpoint_id)
    with pytest.raises(RewindError, match="no checkpoint .* for run 'run_b'"):
        rewind_to_checkpoint(storage, "run_b", cp_a.checkpoint_id)
    storage.close()


def test_a_version_or_sequence_number_that_matches_nothing_is_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_num")
    file_a = workdir / "a.txt"
    file_a.write_text("one", encoding="utf-8")
    _observe(storage, "run_num", file_a)
    first = _checkpoint(storage, "run_num")
    file_a.write_text("two", encoding="utf-8")
    _observe(storage, "run_num", file_a)
    second = _checkpoint(storage, "run_num")

    # The target can be a source-sequence number, not just an id: the version
    # loop walks and misses, the sequence loop matches. (list_checkpoints
    # reconstructs the rows, so identity is not stable across calls.)
    assert (
        resolve_checkpoint(storage, "run_num", str(second.state.source_sequence)).checkpoint_id
        == second.checkpoint_id
    )
    assert (
        resolve_checkpoint(storage, "run_num", str(first.state.source_sequence)).checkpoint_id
        == first.checkpoint_id
    )

    with pytest.raises(RewindError, match="no checkpoint '99' for run 'run_num'"):
        resolve_checkpoint(storage, "run_num", "99")
    with pytest.raises(RewindError, match="no checkpoint '99' for run 'run_num'"):
        rewind_to_checkpoint(storage, "run_num", "99")
    storage.close()


def test_a_corrupted_checkpoint_is_reported_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint whose body fails validation is CorruptedRecord evidence,
    not a lookup miss: surfacing it points the operator at storage, not a
    typo (#1059)."""
    monkeypatch.chdir(tmp_path)
    storage, _ = _seed(tmp_path, "run_corrupt")

    def boom(_checkpoint_id: str) -> StateCheckpoint:
        raise CorruptedRecord("integrity hash does not match the body")

    monkeypatch.setattr(storage, "get_checkpoint", boom)

    with pytest.raises(RewindError, match="corrupted and cannot be trusted"):
        resolve_checkpoint(storage, "run_corrupt", "checkpoint_deadbeef")
    storage.close()


def test_observations_without_a_usable_path_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TOOL_COMPLETED payload whose ``path`` is absent or not a string
    carries no file evidence: it must not poison the before/after maps."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_pathless")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    _observe(storage, "run_pathless", file_a)
    storage.append_event("run_pathless", EventType.TOOL_COMPLETED, {"tool": "Write"})
    storage.append_event("run_pathless", EventType.TOOL_COMPLETED, {"tool": "Edit", "path": 42})
    cp = _checkpoint(storage, "run_pathless")
    storage.append_event("run_pathless", EventType.TOOL_COMPLETED, {"tool": "Write"})
    storage.append_event(
        "run_pathless", EventType.TOOL_COMPLETED, {"tool": "Edit", "path": {"nested": True}}
    )

    result = rewind_to_checkpoint(storage, "run_pathless", cp.checkpoint_id)

    assert result.ok, f"conflicts={result.conflicts} unrecoverable={result.unrecoverable}"
    assert result.reverted_files == ()
    assert result.deleted_files == ()
    assert file_a.read_text(encoding="utf-8") == "at checkpoint"
    storage.close()


# --------------------------------------------------------------------------- #
# the planning pass: triaging a file that is missing at rewind time
# --------------------------------------------------------------------------- #


def test_a_missing_file_with_a_digestless_before_observation_is_unrecoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is gone and the checkpoint-time observation carries no digest,
    so there is nothing to restore from: unrecoverable, never a delete."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_missing_digestless")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    _observe(storage, "run_missing_digestless", file_a, digest=False)
    cp = _checkpoint(storage, "run_missing_digestless")
    file_a.write_text("after", encoding="utf-8")
    _observe(storage, "run_missing_digestless", file_a)
    file_a.unlink()

    with pytest.raises(RewindError, match="unrecoverable"):
        rewind_to_checkpoint(storage, "run_missing_digestless", cp.checkpoint_id)
    result = rewind_to_checkpoint(storage, "run_missing_digestless", cp.checkpoint_id, force=True)

    assert any(
        "file missing and checkpoint-time observation has no digest" in m
        for m in result.unrecoverable
    ), f"unrecoverable={result.unrecoverable}"
    assert result.deleted_files == ()
    assert not result.ok
    storage.close()


def test_a_missing_file_with_an_observed_before_digest_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is gone though a digest was observed both before and after the
    checkpoint: the rewind cannot know whether the last version was lost, so
    it conflicts rather than guessing."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_missing_digest")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    _observe(storage, "run_missing_digest", file_a)
    cp = _checkpoint(storage, "run_missing_digest")
    file_a.write_text("after", encoding="utf-8")
    _observe(storage, "run_missing_digest", file_a)
    file_a.unlink()

    with pytest.raises(RewindError, match="conflict"):
        rewind_to_checkpoint(storage, "run_missing_digest", cp.checkpoint_id)
    result = rewind_to_checkpoint(storage, "run_missing_digest", cp.checkpoint_id, force=True)

    assert any("file missing but last observed digest" in m for m in result.conflicts), (
        f"conflicts={result.conflicts}"
    )
    assert result.deleted_files == ()
    assert result.reverted_files == ()
    assert not result.ok
    storage.close()


# --------------------------------------------------------------------------- #
# the execution pass: the forced deletes
# --------------------------------------------------------------------------- #


def test_a_concurrent_writer_between_plan_and_delete_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan verified the file, then a concurrent writer changed it: the
    immediate re-verify before the unlink is what turns the delete into a
    reported conflict instead of data loss."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_delete_race")
    cp = _checkpoint(storage, "run_delete_race")
    file_b = workdir / "b.txt"
    file_b.write_text("created after the checkpoint", encoding="utf-8")
    _observe(storage, "run_delete_race", file_b)

    real_digest = file_digest
    calls = {"n": 0}

    def racing(path: str | Path) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_digest(path)  # the planning pass sees the file as observed
        return _OTHER_DIGEST  # the execution re-verify sees the late writer

    monkeypatch.setattr("continuum.checkpoint.rewind.file_digest", racing)

    # Without force the conflict discovered during execution is aggregated and
    # raised after the loop, so nothing on disk was touched.
    with pytest.raises(RewindError, match=r"1 conflict\(s\) and 0 unrecoverable"):
        rewind_to_checkpoint(storage, "run_delete_race", cp.checkpoint_id)
    assert file_b.exists()
    assert file_b.read_text(encoding="utf-8") == "created after the checkpoint"

    calls["n"] = 0  # re-arm: plan clean again, then re-verify and report
    result = rewind_to_checkpoint(storage, "run_delete_race", cp.checkpoint_id, force=True)

    assert any("digest changed before delete" in m for m in result.conflicts), (
        f"conflicts={result.conflicts}"
    )
    assert result.deleted_files == ()
    assert file_b.exists()
    storage.close()


def test_an_unlink_that_leaves_the_file_behind_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform that reports the file as still existing after unlink must
    not let the rewind claim a deletion that never happened."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_undeleted")
    cp = _checkpoint(storage, "run_undeleted")
    file_b = workdir / "b.txt"
    file_b.write_text("created after the checkpoint", encoding="utf-8")
    _observe(storage, "run_undeleted", file_b)

    monkeypatch.setattr(Path, "unlink", lambda self: None)  # a filesystem that won't remove it

    result = rewind_to_checkpoint(storage, "run_undeleted", cp.checkpoint_id, force=True)

    assert any("failed to delete" in m for m in result.conflicts), f"conflicts={result.conflicts}"
    assert result.deleted_files == ()
    assert file_b.exists()
    storage.close()


def test_a_delete_that_raises_oserror_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError mid-delete (full disk, permission) lands in the conflict
    list; it must not escape the rewind as a traceback."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_delete_fails")
    cp = _checkpoint(storage, "run_delete_fails")
    file_b = workdir / "b.txt"
    file_b.write_text("created after the checkpoint", encoding="utf-8")
    _observe(storage, "run_delete_fails", file_b)

    def boom(self: Path) -> None:
        raise OSError(28, "no space left on device")

    monkeypatch.setattr(Path, "unlink", boom)

    result = rewind_to_checkpoint(storage, "run_delete_fails", cp.checkpoint_id, force=True)

    assert any("delete failed" in m for m in result.conflicts), f"conflicts={result.conflicts}"
    assert result.deleted_files == ()
    assert file_b.exists()
    storage.close()


# --------------------------------------------------------------------------- #
# the execution pass: the planned restores
# --------------------------------------------------------------------------- #


def test_a_revert_whose_snapshot_never_landed_is_unrecoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The before-observation has a digest, but no blob is stored for it: the
    snapshot was pruned by a retention sweep, or never filed because the file
    was too large or unreadable at capture time. Report it, and never restore
    from anything but the snapshot (#1268)."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_no_snapshot")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    before = _observe(storage, "run_no_snapshot", file_a)
    cp = _checkpoint(storage, "run_no_snapshot")
    file_a.write_text("after", encoding="utf-8")
    _observe(storage, "run_no_snapshot", file_a)
    # the blob for the checkpoint-time digest is gone
    snapshot_path(before["sha256"]).unlink()

    result = rewind_to_checkpoint(storage, "run_no_snapshot", cp.checkpoint_id, force=True)

    assert any("no snapshot for digest" in m for m in result.unrecoverable), (
        f"unrecoverable={result.unrecoverable}"
    )
    assert result.reverted_files == ()
    # the workspace file was never overwritten from anywhere but a snapshot
    assert file_a.read_text(encoding="utf-8") == "after"
    storage.close()


def test_a_concurrent_writer_between_plan_and_restore_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same re-verification, on the restore side: the content moved after the
    plan, so restoring would clobber the late write."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_restore_race")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    _observe(storage, "run_restore_race", file_a)
    cp = _checkpoint(storage, "run_restore_race")
    file_a.write_text("after", encoding="utf-8")
    _observe(storage, "run_restore_race", file_a)

    real_digest = file_digest
    calls = {"n": 0}

    def racing(path: str | Path) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_digest(path)  # the planning pass sees the file as observed
        return _OTHER_DIGEST  # the execution re-verify sees the late writer

    monkeypatch.setattr("continuum.checkpoint.rewind.file_digest", racing)

    result = rewind_to_checkpoint(storage, "run_restore_race", cp.checkpoint_id, force=True)

    assert any("digest changed before restore" in m for m in result.conflicts), (
        f"conflicts={result.conflicts}"
    )
    assert result.reverted_files == ()
    assert file_a.read_text(encoding="utf-8") == "after"
    storage.close()


def test_a_restore_that_fails_is_unrecoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restore_file`` returning False is reported, and the workspace keeps
    whatever was on disk."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_restore_fails")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    _observe(storage, "run_restore_fails", file_a)
    cp = _checkpoint(storage, "run_restore_fails")
    file_a.write_text("after", encoding="utf-8")
    _observe(storage, "run_restore_fails", file_a)

    monkeypatch.setattr("continuum.checkpoint.rewind.restore_file", lambda path, sha: False)

    result = rewind_to_checkpoint(storage, "run_restore_fails", cp.checkpoint_id, force=True)

    assert any("restore failed" in m for m in result.unrecoverable), (
        f"unrecoverable={result.unrecoverable}"
    )
    assert result.reverted_files == ()
    assert file_a.read_text(encoding="utf-8") == "after"
    storage.close()


def test_a_restore_whose_content_disagrees_with_the_snapshot_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore reported success but the bytes do not hash to the digest
    the checkpoint recorded: that is a conflict, not a completed revert."""
    monkeypatch.chdir(tmp_path)
    storage, workdir = _seed(tmp_path, "run_restore_lies")
    file_a = workdir / "a.txt"
    file_a.write_text("at checkpoint", encoding="utf-8")
    _observe(storage, "run_restore_lies", file_a)
    cp = _checkpoint(storage, "run_restore_lies")
    file_a.write_text("after", encoding="utf-8")
    _observe(storage, "run_restore_lies", file_a)

    def lying_restore(path: str | Path, sha: str) -> bool:
        Path(path).write_text("content that is not the snapshot", encoding="utf-8")
        return True

    monkeypatch.setattr("continuum.checkpoint.rewind.restore_file", lying_restore)

    result = rewind_to_checkpoint(storage, "run_restore_lies", cp.checkpoint_id, force=True)

    assert any("after restore digest" in m for m in result.conflicts), (
        f"conflicts={result.conflicts}"
    )
    assert result.reverted_files == ()
    assert file_a.read_text(encoding="utf-8") == "content that is not the snapshot"
    storage.close()


# --------------------------------------------------------------------------- #
# the assessment tail
# --------------------------------------------------------------------------- #


def test_a_recovery_assessment_failure_degrades_to_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewind itself succeeded; the recovery verdict cannot be computed.
    The result must still answer, with the verdict degraded rather than the
    whole rewind raising after the work is already done."""
    monkeypatch.chdir(tmp_path)
    storage, _ = _seed(tmp_path, "run_assess_boom")
    cp = _checkpoint(storage, "run_assess_boom")
    storage.close()

    class _BoomEngine:
        def __init__(self, storage: Any) -> None:
            pass

        def assess(self, run_id: str) -> Any:
            raise RuntimeError("verdict engine offline")

    monkeypatch.setattr("continuum.checkpoint.rewind.RecoveryEngine", _BoomEngine)

    storage = SQLiteStorage(str(tmp_path / "rewind.db"))
    result = rewind_to_checkpoint(storage, "run_assess_boom", cp.checkpoint_id)

    assert result.ok, f"conflicts={result.conflicts} unrecoverable={result.unrecoverable}"
    assert result.resume_mode == "unknown"
    assert result.resume_safe is False
    storage.close()
