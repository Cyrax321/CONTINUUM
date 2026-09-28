"""Instant resume, scoped confirm, slim subset (issue #394)."""

from __future__ import annotations

import argparse
import io
import json
import os
import time
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.mcp.server import build_server
from continuum.models import Origin, Run
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


def test_banner_appears_only_when_interrupted_run_exists(tmp_path: Path) -> None:
    """Checkpoint writes resume.json; briefing is silent otherwise."""
    import os

    orig_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        # No file initially, hook should be silent (fast path in main)
        import io

        from continuum.cli.main import main as cli_main

        out = io.StringIO()
        err = io.StringIO()
        # briefing with no active run and no resume.json should be silent via fast path
        # It returns OK with no output because file doesn't exist
        code = cli_main(["briefing"], out=out, err=err)
        assert code == 0
        assert out.getvalue() == ""

        # Now create a run and checkpoint, which should write the file
        from continuum.storage import SQLiteStorage

        db = tmp_path / "continuum.db"
        storage = SQLiteStorage(str(db))
        storage.create_run(Run(run_id="r1", goal="do X"))
        storage.append_event(
            "r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT
        )
        storage.append_event(
            "r1",
            EventType.TASK_UPDATED,
            {"completed": 1, "failed": 0},
            source=Origin.EXTERNAL_AGENT,
        )
        mgr = CheckpointManager(storage)
        mgr.checkpoint("r1")
        storage.close()

        resume = Path(".continuum/resume.json")
        assert resume.exists()
        data = json.loads(resume.read_text(encoding="utf-8"))
        assert data["run_id"] == "r1"
        assert "checkpoint_id" in data

        # Briefing now should inject banner and not be silent
        out2 = io.StringIO()
        err2 = io.StringIO()
        code2 = cli_main(["--db", str(db), "briefing"], out=out2, err=err2)
        assert code2 == 0
        output = out2.getvalue()
        assert "Interrupted run r1" in output
        assert "continuum resume r1" in output

        # After completing the run, file should be removed or not show banner for that run
        storage2 = SQLiteStorage(str(db))
        # Simulate complete via CLI
        out3 = io.StringIO()
        err3 = io.StringIO()
        cli_main(["--db", str(db), "complete", "r1"], out=out3, err=err3)
        storage2.close()
        # File should be gone or not refer to r1
        if resume.exists():
            data2 = json.loads(resume.read_text(encoding="utf-8"))
            assert data2.get("run_id") != "r1"

    finally:
        os.chdir(orig_cwd)


def test_banner_notes_a_run_not_in_the_database_instead_of_advertising_it(
    tmp_path: Path,
) -> None:
    """Issue #1063: a stale resume.json must not advertise a ghost run."""
    import io
    import os

    from continuum.cli.main import main as cli_main

    orig_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        Path(".continuum").mkdir(parents=True, exist_ok=True)
        Path(".continuum/resume.json").write_text(json.dumps({"run_id": "ghost"}), encoding="utf-8")

        db = tmp_path / "continuum.db"
        storage = SQLiteStorage(str(db))
        storage.create_run(Run(run_id="real", goal="live run"))
        storage.append_event(
            "real", EventType.RUN_STARTED, {"goal": "live run"}, source=Origin.EXTERNAL_AGENT
        )
        storage.close()

        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["--db", str(db), "briefing"], out=out, err=err)
        assert code == 0
        output = out.getvalue()
        # The banner's resume command could only fail with a not-found error;
        # the ghost gets a one-line note instead.
        assert "Interrupted run ghost – resume pending" not in output
        assert "continuum resume ghost" not in output
        assert "ghost is no longer in the database" in output
        # The briefing body beneath the note covers the live run.
        assert "CONTINUUM active run: real" in output
    finally:
        os.chdir(orig_cwd)


def test_banner_omitted_when_interrupted_run_is_completed(tmp_path: Path) -> None:
    """A completed run has no pending interruption to surface."""
    import io
    import os

    from continuum.cli.main import main as cli_main

    orig_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        db = tmp_path / "continuum.db"
        storage = SQLiteStorage(str(db))
        storage.create_run(Run(run_id="done", goal="finished"))
        storage.append_event(
            "done", EventType.RUN_STARTED, {"goal": "finished"}, source=Origin.EXTERNAL_AGENT
        )
        from continuum.models import RunStatus

        storage.update_run(storage.get_run("done").touch(status=RunStatus.COMPLETED))
        storage.close()
        # The file the last checkpoint wrote survives the completion.
        Path(".continuum").mkdir(parents=True, exist_ok=True)
        Path(".continuum/resume.json").write_text(json.dumps({"run_id": "done"}), encoding="utf-8")

        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["--db", str(db), "briefing"], out=out, err=err)
        assert code == 0
        output = out.getvalue()
        assert "Interrupted run done – resume pending" not in output
        assert "no longer in the database" not in output
    finally:
        os.chdir(orig_cwd)


def test_banner_latency_is_fast(tmp_path: Path) -> None:
    """Reading resume.json out of band is well under a second."""
    import os

    orig_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        Path(".continuum").mkdir(parents=True, exist_ok=True)
        Path(".continuum/resume.json").write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")
        start = time.perf_counter()
        data = json.loads(Path(".continuum/resume.json").read_text(encoding="utf-8"))
        elapsed = time.perf_counter() - start
        assert data["run_id"] == "r1"
        assert elapsed < 0.5, f"resume.json read took {elapsed:.3f}s, expected <0.5s"
        # Also test the CLI fast path for missing file is fast
        Path(".continuum/resume.json").unlink()
        import io

        from continuum.cli.main import main as cli_main

        out = io.StringIO()
        err = io.StringIO()
        start2 = time.perf_counter()
        code = cli_main(["briefing"], out=out, err=err)
        elapsed2 = time.perf_counter() - start2
        assert code == 0
        assert elapsed2 < 0.5, f"briefing fast path took {elapsed2:.3f}s"
    finally:
        os.chdir(orig_cwd)


def test_scoped_confirm_leaves_unrelated_uncertainty_intact(tmp_path: Path) -> None:
    """Goal confirmed, uncertain side effect still blocks (issue #394)."""
    from continuum.actions import ActionLedger

    db = tmp_path / "db.sqlite"
    storage = SQLiteStorage(str(db))
    storage.create_run(Run(run_id="r1", goal="do X"))
    storage.append_event(
        "r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT
    )
    storage.append_event(
        "r1", EventType.TASK_UPDATED, {"completed": 1, "failed": 0}, source=Origin.EXTERNAL_AGENT
    )
    ledger = ActionLedger(storage, "r1")
    outcome = ledger.claim("test.write_file", {"file": "/tmp/foo"})
    ledger.fail(outcome.key, "timeout", certain=False)

    # Before confirm, both goal/progress and uncertain block
    dec = RecoveryEngine(storage).assess("r1")
    assert dec.mode.value == "request_human"
    # Progress and goal are both REQUIRES_REVIEW, plus uncertain

    # Confirm only goal
    storage.append_event(
        "r1", EventType.REVIEW_CONFIRMED, {"components": ["goal"]}, source=Origin.HUMAN
    )
    dec2 = RecoveryEngine(storage).assess("r1")
    # Goal should be valid, progress still REQUIRES_REVIEW, and uncertain still blocks
    statuses = {e.component.value: e.status.value for e in dec2.validation.report.statuses}
    assert statuses.get("goal") == "valid"
    assert statuses.get("progress") == "requires_review"
    assert len(dec2.uncertain_actions) == 1
    assert dec2.mode.value == "request_human"

    # Full confirm of progress as well should still be blocked by uncertain
    storage.append_event(
        "r1", EventType.REVIEW_CONFIRMED, {"components": ["progress"]}, source=Origin.HUMAN
    )
    dec3 = RecoveryEngine(storage).assess("r1")
    statuses3 = {e.component.value: e.status.value for e in dec3.validation.report.statuses}
    assert statuses3.get("goal") == "valid"
    assert statuses3.get("progress") == "valid"
    assert len(dec3.uncertain_actions) == 1
    assert dec3.mode.value == "request_human"

    storage.close()


def test_slim_subset_lists_exactly_read_only_trio(monkeypatch) -> None:
    """Slim lists exactly validate/resume/list_actions; mutating refuses."""
    monkeypatch.setenv("CONTINUUM_MCP_SLIM", "1")
    storage = SQLiteStorage(":memory:")
    server, _ = build_server(storage=storage)
    names = {tool.name for tool in server._tool_manager._tools.values()}
    assert names == {"continuum_resume", "continuum_validate", "continuum_list_actions"}

    # Mutating calls should refuse identically to today (i.e., not be present, so call fails)
    # The server should not have record_progress or checkpoint
    assert "continuum_record_progress" not in names
    assert "continuum_checkpoint" not in names
    monkeypatch.delenv("CONTINUUM_MCP_SLIM", raising=False)
    # Full mode should have mutating tools
    server2, _ = build_server(storage=SQLiteStorage(":memory:"))
    names2 = {tool.name for tool in server2._tool_manager._tools.values()}
    assert "continuum_record_progress" in names2
    assert len(names2) == 12


def test_default_flows_unchanged_when_features_unused(tmp_path: Path) -> None:
    """Without scope or slim or resume.json, flows are identical to today."""
    # Full confirm still works
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r1", goal="do X"))
    storage.append_event(
        "r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT
    )
    storage.append_event(
        "r1", EventType.TASK_UPDATED, {"completed": 1, "failed": 0}, source=Origin.EXTERNAL_AGENT
    )
    storage.append_event(
        "r1", EventType.REVIEW_CONFIRMED, {"components": ["goal", "progress"]}, source=Origin.HUMAN
    )
    dec = RecoveryEngine(storage).assess("r1")
    # No uncertain, both confirmed, should be resume
    assert dec.mode.value == "resume"
    assert dec.safe

    # No resume.json, briefing should be the normal no-active-run path, not silent
    # (but our fast path for hook is silent only when hook and no file; manual briefing
    # with no run should still show message, but we test the engine directly)
    storage.close()

    # Default confirm via CLI without --scope should still be full
    import io
    import os

    orig_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        db = tmp_path / "db2.sqlite"
        s = SQLiteStorage(str(db))
        s.create_run(Run(run_id="r2", goal="g2"))
        s.append_event("r2", EventType.RUN_STARTED, {"goal": "g2"}, source=Origin.EXTERNAL_AGENT)
        s.append_event(
            "r2",
            EventType.TASK_UPDATED,
            {"completed": 1, "failed": 0},
            source=Origin.EXTERNAL_AGENT,
        )
        s.close()
        from continuum.cli.main import main as cli_main

        out = io.StringIO()
        err = io.StringIO()
        code = cli_main(["--db", str(db), "confirm", "r2"], out=out, err=err)
        assert code == 0 or code == 1  # confirm may return mode-based exit, but should not error
        # Check that event was written with both components
        s2 = SQLiteStorage(str(db))
        evs = [e for e in s2.read_events("r2") if e.type == EventType.REVIEW_CONFIRMED]
        assert evs
        assert set(evs[-1].payload.get("components", [])) == {"goal", "progress"}
        s2.close()
    finally:
        os.chdir(orig_cwd)


def _briefing_args(**overrides: object) -> argparse.Namespace:
    """A briefing args namespace as the dispatcher builds one.

    ``cmd_briefing`` is reached through the CLI dispatcher, which answers the
    no-resume.json case itself, so the function's own guard is exercised
    directly here.
    """
    base: dict[str, object] = {
        "run_id": None,
        "hook_event_name": "SessionStart",
        "json": False,
        "raw_summary": False,
        "_palette": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_briefing_guard_is_silent_with_no_resume_file(tmp_path: Path) -> None:
    """cmd_briefing's own guard stays silent when nothing is interrupted.

    The dispatcher's fast path answers this case first, so the guard is
    defence-in-depth for a caller that reaches the function directly: it must
    not open the database just to report nothing. An active run exists in the
    store, so any output at all would mean the guard did not fire.
    """
    from continuum.cli.main import cmd_briefing

    orig_cwd = Path.cwd()
    db = tmp_path / "continuum.db"
    try:
        os.chdir(tmp_path)
        storage = SQLiteStorage(str(db))
        storage.create_run(Run(run_id="r1", goal="do X"))
        storage.append_event(
            "r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT
        )
        out, err = io.StringIO(), io.StringIO()
        code = cmd_briefing(_briefing_args(), storage, out, err)
        assert code == 0
        assert out.getvalue() == "", "nothing is interrupted, so nothing is printed"
        storage.close()
    finally:
        os.chdir(orig_cwd)


def test_briefing_proceeds_without_a_resume_file_for_other_hooks(tmp_path: Path) -> None:
    """The silence is SessionStart-only: every other caller still gets briefed.

    A SessionEnd hook or a manual invocation has nothing to be silent about,
    so the missing file is not special there and the briefing reports the
    active run as usual.
    """
    from continuum.cli.main import cmd_briefing

    orig_cwd = Path.cwd()
    db = tmp_path / "continuum.db"
    try:
        os.chdir(tmp_path)
        storage = SQLiteStorage(str(db))
        storage.create_run(Run(run_id="r1", goal="do X"))
        storage.append_event(
            "r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT
        )
        out, err = io.StringIO(), io.StringIO()
        code = cmd_briefing(_briefing_args(hook_event_name="SessionEnd"), storage, out, err)
        assert code == 0
        assert "CONTINUUM active run: r1" in out.getvalue()
        storage.close()
    finally:
        os.chdir(orig_cwd)


@pytest.mark.parametrize(
    "payload",
    [
        # Unreadable bytes: the file is corrupt, not a signal to fail.
        b"{{not json at all",
        # Valid JSON that is not an object.
        '["not-a-dict"]',
        # An object whose run_id has the wrong shape.
        '{"run_id": 123}',
        # An object whose run_id is present but empty.
        '{"run_id": ""}',
        # An object that names no run at all.
        "{}",
    ],
    ids=["corrupt", "non-object", "non-string-id", "empty-id", "no-id"],
)
def test_briefing_falls_through_an_unusable_resume_file(
    tmp_path: Path, payload: bytes | str
) -> None:
    """A resume.json that cannot name a run does not advertise one (#1063).

    Each payload fails validation for a different reason — unreadable, not an
    object, a run_id of the wrong type, an empty run_id, a missing run_id — but
    all must degrade the same way: the briefing falls back to the live active
    run and prints no resume command, because a command for a run that cannot
    be validated could only fail.
    """
    from continuum.cli.main import main as cli_main

    orig_cwd = Path.cwd()
    db = tmp_path / "continuum.db"
    try:
        os.chdir(tmp_path)
        Path(".continuum").mkdir(parents=True, exist_ok=True)
        data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        Path(".continuum/resume.json").write_bytes(data)
        storage = SQLiteStorage(str(db))
        storage.create_run(Run(run_id="live", goal="the real run"))
        storage.append_event(
            "live", EventType.RUN_STARTED, {"goal": "the real run"}, source=Origin.EXTERNAL_AGENT
        )
        storage.close()

        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["--db", str(db), "briefing"], out=out, err=err)
        assert code == 0, err.getvalue()
        output = out.getvalue()
        assert "CONTINUUM active run: live" in output
        assert "resume pending" not in output
        assert "no longer in the database" not in output
    finally:
        os.chdir(orig_cwd)
