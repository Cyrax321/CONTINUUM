"""continuum forget --tenant enumeration, tombstone and verify after tombstone (issue #567, parent #304)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from continuum.actions.ledger import ActionLedger
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _run_cli(args: list[str], db: str) -> tuple[int, str, str]:
    """Run CLI main with given args, capturing output."""
    import os

    # Ensure we import from the worktree's src, not installed package
    env = os.environ.copy()
    # Use the current worktree's src if available, else fallback
    # The test file lives in the worktree, so its parent is the worktree root
    worktree_root = Path(__file__).resolve().parents[1]
    src_path = str(worktree_root / "src")
    env["PYTHONPATH"] = src_path + (
        ":" + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
    )
    cmd = [sys.executable, "-m", "continuum.cli", "--db", db, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def test_forget_enumerates_and_tombstones(tmp_path: Path) -> None:
    db = str(tmp_path / "forget.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        # Create memory writes for two tenants
        for rec in ("rec-1", "rec-2"):
            rendered = f"mem:pgvector_main:acme:{rec}"
            ledger.claim("mem_write", {}, key=rendered, scoped_to_run=False)
        for rec in ("rec-3",):
            rendered = f"mem:pgvector_main:globex:{rec}"
            ledger.claim("mem_write", {}, key=rendered, scoped_to_run=False)
        # Verify enumeration via direct ledger scan
        events = list(store.read_events("run_1"))
        mem_events = [e for e in events if e.payload.get("rendered_key", "").startswith("mem:")]
        assert len(mem_events) == 3

    # CLI forget --tenant acme --dry-run should list rec-1, rec-2
    code, out, err = _run_cli(
        ["--json", "forget", "--tenant", "acme", "--dry-run", "--run-id", "run_1"], db
    )
    assert code == 0, f"dry-run failed: {out} {err}"
    data = json.loads(out)
    assert data["tenant"] == "acme"
    assert sorted(data["record_keys"]) == ["rec-1", "rec-2"]
    assert data["dry_run"] is True
    # Dry-run should not have written tombstone
    with SQLiteStorage(db) as store:
        events = list(store.read_events("run_1"))
        tombstones = [e for e in events if e.type == EventType.MEMORY_TOMBSTONED]
        assert len(tombstones) == 0
        # verify should pass
        report = store.verify_events("run_1")
        assert report.ok is True

    # Now real forget
    code, out, err = _run_cli(
        ["--json", "forget", "--tenant", "acme", "--run-id", "run_1", "--reason", "gdpr"], db
    )
    assert code == 0, f"forget failed: {out} {err}"
    data = json.loads(out)
    assert data["tenant"] == "acme"
    assert data["tombstone_run"] == "run_1"
    assert "tombstone_sequence" in data
    # Check tombstone event
    with SQLiteStorage(db) as store:
        events = list(store.read_events("run_1"))
        tombstones = [e for e in events if e.type == EventType.MEMORY_TOMBSTONED]
        assert len(tombstones) == 1
        assert tombstones[0].payload["tenant"] == "acme"
        assert sorted(tombstones[0].payload["record_keys"]) == ["rec-1", "rec-2"]
        assert tombstones[0].payload["reason"] == "gdpr"
        assert tombstones[0].payload["hashes_kept"] is True
        # Verify still passes after tombstone
        report = store.verify_events("run_1")
        assert report.ok is True
        # Historical hashes are retained: the chain still has the original mem events
        mem_events = [e for e in events if e.payload.get("rendered_key", "").startswith("mem:")]
        assert len(mem_events) == 3


def test_forget_verify_after_tombstone_across_runs(tmp_path: Path) -> None:
    db = str(tmp_path / "forget2.db")
    with SQLiteStorage(db) as store:
        for rid in ("run_1", "run_2"):
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
        ledger1 = ActionLedger(store, "run_1")
        ledger1.claim("mem_write", {}, key="mem:pgvector_main:acme:rec-42", scoped_to_run=False)
        ledger2 = ActionLedger(store, "run_2")
        ledger2.claim("mem_write", {}, key="mem:pgvector_main:acme:rec-99", scoped_to_run=False)

    # Forget acme without specifying run-id, should enumerate across all runs
    code, out, err = _run_cli(["--json", "forget", "--tenant", "acme", "--dry-run"], db)
    assert code == 0, f"cross-run dry-run failed: {out} {err}"
    data = json.loads(out)
    # Should find both rec-42 and rec-99
    assert sorted(data["record_keys"]) == ["rec-42", "rec-99"]
    assert len(data["hits"]) == 2

    # Real forget to active run (run_2 is active as last updated)
    code, out, err = _run_cli(["--json", "forget", "--tenant", "acme"], db)
    assert code == 0
    data = json.loads(out)
    assert data["tenant"] == "acme"
    # Tombstone should be written to some run (active)
    assert data["tombstone_run"] in ("run_1", "run_2")
    with SQLiteStorage(db) as store:
        # Both runs verify
        for rid in ("run_1", "run_2"):
            report = store.verify_events(rid)
            assert report.ok is True


def test_forget_empty_tenant_no_tombstone(tmp_path: Path) -> None:
    db = str(tmp_path / "forget3.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        ledger.claim("mem_write", {}, key="mem:pgvector_main:acme:rec-1", scoped_to_run=False)

    code, out, err = _run_cli(["--json", "forget", "--tenant", "nobody", "--run-id", "run_1"], db)
    assert code == 0
    data = json.loads(out)
    assert data["record_keys"] == []
    assert data["hits"] == []
    with SQLiteStorage(db) as store:
        events = list(store.read_events("run_1"))
        tombstones = [e for e in events if e.type == EventType.MEMORY_TOMBSTONED]
        # No tombstone when nothing to tombstone? Our impl writes only when sorted_keys non-empty
        assert len(tombstones) == 0
