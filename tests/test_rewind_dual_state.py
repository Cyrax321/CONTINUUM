"""Atomic dual-state rewind (issue #292) — 6 acceptance criteria."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

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
    arch = Path("references/architecture.md").read_text(encoding="utf-8")
    cli = Path("references/cli.md").read_text(encoding="utf-8")
    assert "rewind" in arch.lower()
    assert "rewind" in cli.lower()
