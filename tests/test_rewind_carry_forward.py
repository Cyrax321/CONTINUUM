"""Rewind carry-forward passthrough (issue #493)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.cli import main
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_rewind_over_unsettled_authorization_refuses_and_carry_passes(tmp_path: Path) -> None:
    db = str(tmp_path / "carry.db")
    run_id = "run_carry_1"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    cp = CheckpointManager(storage).checkpoint(run_id, trigger="test")
    storage.append_event(
        run_id, EventType.APPROVAL_GRANTED, {"approval_id": "ap-1", "subject": "ship"}
    )
    storage.close()
    code, out, _ = _run_cli("--db", db, "rewind", run_id, "--to", cp.checkpoint_id)
    assert code != 0
    assert "ap-1" in out
    assert "restore refused" in out.lower()
    code2, out2, _ = _run_cli(
        "--db", db, "rewind", run_id, "--to", cp.checkpoint_id, "--carry-forward", "ap-1"
    )
    assert code2 == 0, out2
    assert "ap-1" in out2
    assert "preserved preconditions" in out2.lower()
    assert "carried forward: ap-1" in out2.lower()
    storage2 = SQLiteStorage(db)
    events = storage2.read_events(run_id)
    lineage = [e for e in events if e.type is EventType.RUN_RESTORED]
    assert lineage
    assert "ap-1" in lineage[-1].payload.get("carry_forward", [])
    code3, out3, _ = _run_cli(
        "--db",
        db,
        "--json",
        "rewind",
        run_id,
        "--to",
        cp.checkpoint_id,
        "--carry-forward",
        "ap-1",
    )
    assert code3 == 0
    payload = json.loads(out3)
    assert "ap-1" in payload.get("carry_forward", [])
    assert "preserved_summary" in payload
    storage2.close()


def test_rewind_carry_forward_repeatable(tmp_path: Path) -> None:
    db = str(tmp_path / "repeat.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run2", goal="g"))
    storage.append_event("run2", EventType.RUN_STARTED, {"goal": "g"})
    cp = CheckpointManager(storage).checkpoint("run2")
    storage.append_event("run2", EventType.APPROVAL_GRANTED, {"approval_id": "a", "subject": "s1"})
    storage.append_event("run2", EventType.APPROVAL_GRANTED, {"approval_id": "b", "subject": "s2"})
    storage.close()
    code, out, _ = _run_cli(
        "--db",
        db,
        "rewind",
        "run2",
        "--to",
        cp.checkpoint_id,
        "--carry-forward",
        "a",
        "--carry-forward",
        "b",
    )
    assert code == 0, out
    assert "a" in out and "b" in out
    storage2 = SQLiteStorage(db)
    events = storage2.read_events("run2")
    lineage = [e for e in events if e.type is EventType.RUN_RESTORED]
    assert lineage
    cf = lineage[-1].payload.get("carry_forward", [])
    assert "a" in cf and "b" in cf
    storage2.close()


def test_rewind_over_uncertain_slot_refuses_and_carry_passes(tmp_path: Path) -> None:
    db = str(tmp_path / "uncertain.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run4", goal="g"))
    storage.append_event("run4", EventType.RUN_STARTED, {"goal": "g"})
    cp = CheckpointManager(storage).checkpoint("run4")
    ledger = ActionLedger(storage, "run4")
    outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
    aid = outcome.action.action_id
    storage.close()
    code, out, _ = _run_cli("--db", db, "rewind", "run4", "--to", cp.checkpoint_id)
    assert code != 0
    assert aid in out
    assert "restore refused" in out.lower()
    code2, out2, _ = _run_cli(
        "--db", db, "rewind", "run4", "--to", cp.checkpoint_id, "--carry-forward", aid
    )
    assert code2 == 0, out2
    assert aid in out2
    storage2 = SQLiteStorage(db)
    events = storage2.read_events("run4")
    lineage = [e for e in events if e.type is EventType.RUN_RESTORED]
    assert aid in lineage[-1].payload.get("carry_forward", [])
    storage2.close()


def test_rewind_carry_forward_dry_run_does_not_write_lineage(tmp_path: Path) -> None:
    db = str(tmp_path / "dry.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run3", goal="g"))
    storage.append_event("run3", EventType.RUN_STARTED, {"goal": "g"})
    cp = CheckpointManager(storage).checkpoint("run3")
    storage.append_event(
        "run3", EventType.APPROVAL_GRANTED, {"approval_id": "ap-1", "subject": "s"}
    )
    storage.close()
    code, _, _ = _run_cli(
        "--db",
        db,
        "rewind",
        "run3",
        "--to",
        cp.checkpoint_id,
        "--dry-run",
        "--carry-forward",
        "ap-1",
    )
    assert code == 0
    storage2 = SQLiteStorage(db)
    events = storage2.read_events("run3")
    lineage = [e for e in events if e.type is EventType.RUN_RESTORED]
    assert len(lineage) == 0
    storage2.close()
