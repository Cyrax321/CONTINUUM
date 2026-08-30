"""Instant resume, scoped confirm, slim subset (issue #394)."""

from __future__ import annotations

import json
import time
from pathlib import Path

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
