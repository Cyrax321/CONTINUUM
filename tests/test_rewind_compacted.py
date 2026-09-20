"""Rewind must work on a compacted run (issue #1053).

Compaction archives the pre-anchor prefix of the log, including RUN_STARTED
and the pre-checkpoint TOOL_COMPLETED events. Rewind read both its projection
and its tool-event collection from the live tail alone, so on a compacted run
it concluded the run never started and refused outright, and once that was
patched it silently restored nothing -- or, under a partial compaction that
left the post-checkpoint write live, deleted a file it existed to restore.
CheckpointManager.project_current fixed this class of bug for the projection
(issue #648) by folding read_all_events; rewind now does the same.
"""

from __future__ import annotations

import io
from pathlib import Path

from continuum.checkpoint.manager import CheckpointManager
from continuum.checkpoint.rewind import rewind_to_checkpoint
from continuum.cli import main
from continuum.cli.exitcodes import ExitCode
from continuum.clienthooks import observe_event_payload
from continuum.environment.file_snapshot import snapshot_file
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _write_tool_event(storage: SQLiteStorage, run_id: str, path: Path, content: str) -> str:
    """Record a hook-observed write and snapshot its content, returning the digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    payload = observe_event_payload({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
    snapshot_file(path, sha256=payload.get("sha256"))
    storage.append_event(run_id, EventType.TOOL_COMPLETED, payload)
    return payload.get("sha256")


def _seed_run(tmp_path: Path) -> tuple[str, str, Path, Path]:
    """A run with one file written before a checkpoint and two writes after it."""
    db = str(tmp_path / "rewind_compacted.db")
    run_id = "run_rewind_compacted"
    workdir = tmp_path / "work"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="rewind after compaction"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "rewind after compaction"})

    kept = workdir / "kept.txt"
    _write_tool_event(storage, run_id, kept, "at checkpoint")

    cp = CheckpointManager(storage).checkpoint(run_id, reason="before compaction")

    _write_tool_event(storage, run_id, kept, "modified after checkpoint")
    created = workdir / "created.txt"
    _write_tool_event(storage, run_id, created, "new file after checkpoint")
    storage.close()
    return db, cp.checkpoint_id, kept, created


def test_rewind_runs_on_a_compacted_run(tmp_path: Path) -> None:
    """The CLI command stops refusing compacted runs with 'has no goal'."""
    db, checkpoint_id, kept, _ = _seed_run(tmp_path)

    with SQLiteStorage(db) as store:
        store.compact_run("run_rewind_compacted")
    # Compaction did archive RUN_STARTED: the tail no longer carries it.
    with SQLiteStorage(db) as store:
        types = {e.type for e in store.read_events("run_rewind_compacted")}
    assert EventType.RUN_STARTED not in types

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--db", db, "rewind", "run_rewind_compacted", "--to", checkpoint_id], out=out, err=err
    )

    assert code == ExitCode.OK, f"rewind failed: {err.getvalue()}{out.getvalue()}"
    assert "has no goal" not in err.getvalue()
    assert kept.read_text(encoding="utf-8") == "at checkpoint"


def test_dry_run_restores_a_compacted_file_rather_than_deleting_it(tmp_path: Path) -> None:
    """A pre-checkpoint write lands in reverted_files, never deleted_files."""
    db, checkpoint_id, kept, created = _seed_run(tmp_path)

    with SQLiteStorage(db) as store:
        store.compact_run("run_rewind_compacted")

    with SQLiteStorage(db) as store:
        result = rewind_to_checkpoint(store, "run_rewind_compacted", checkpoint_id, dry_run=True)

    assert result.ok, (
        f"unexpected conflicts: {result.conflicts} unrecoverable: {result.unrecoverable}"
    )
    assert str(kept) in result.reverted_files
    # The pre-checkpoint content is what a real rewind would restore.
    assert str(kept) not in result.deleted_files
    # A file created after the checkpoint is still deleted, archive or not.
    assert str(created) in result.deleted_files


def test_partial_compaction_never_takes_the_delete_branch(tmp_path: Path) -> None:
    """Only the pre-checkpoint write archived, the post-checkpoint write live.

    This is the literal data-loss shape from the issue: the before-set came
    back empty from the live tail while the after-set was populated, so the
    restore branch fell through to delete. A dry run must still report the
    file as reverted.
    """
    db, checkpoint_id, kept, _ = _seed_run(tmp_path)

    with SQLiteStorage(db) as store:
        pre_checkpoint = next(
            e.sequence
            for e in store.read_all_events("run_rewind_compacted")
            if e.type is EventType.TOOL_COMPLETED
        )
        # Archive only the pre-checkpoint prefix; the post-checkpoint writes
        # stay in the live tail.
        store.compact_run("run_rewind_compacted", through_sequence=pre_checkpoint)

    with SQLiteStorage(db) as store:
        live_tool = [
            e
            for e in store.read_events("run_rewind_compacted")
            if e.type is EventType.TOOL_COMPLETED
        ]
        archived_tool = [
            e
            for e in store.read_archived_events("run_rewind_compacted")
            if e.type is EventType.TOOL_COMPLETED
        ]
    assert live_tool and archived_tool, "fixture should straddle the archive boundary"

    with SQLiteStorage(db) as store:
        result = rewind_to_checkpoint(store, "run_rewind_compacted", checkpoint_id, dry_run=True)

    assert result.ok, (
        f"unexpected conflicts: {result.conflicts} unrecoverable: {result.unrecoverable}"
    )
    assert str(kept) in result.reverted_files
    assert str(kept) not in result.deleted_files


def test_rewind_on_uncompacted_run_is_unchanged(tmp_path: Path) -> None:
    """The archive-aware reads do not change the uncompacted behaviour."""
    db, checkpoint_id, kept, created = _seed_run(tmp_path)

    with SQLiteStorage(db) as store:
        result = rewind_to_checkpoint(store, "run_rewind_compacted", checkpoint_id, dry_run=True)

    assert result.ok
    assert str(kept) in result.reverted_files
    assert str(created) in result.deleted_files
