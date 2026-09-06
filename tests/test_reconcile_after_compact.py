"""Reconcile and authority settle must see archived history (#647).

Compaction moves the pre-anchor prefix into events_archive, but the
probe-driven settle path folded only the live tail. An archived
uncertain action crashed settle_run with LookupError, and authority
probes ran on an empty payload. Both scans now use full history.
"""

from __future__ import annotations

import io
import json
import shlex
import sys
from pathlib import Path

from continuum.actions import ActionLedger
from continuum.actions.authority import record_authority_consumed
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Run
from continuum.reconcilers import load_reconcilers, settle_authority, settle_run
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def make_db(path: Path) -> str:
    db = str(path / "rec647.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    return db


def compact(db: str) -> None:
    code, _, err = run("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err
    with SQLiteStorage(db) as store:
        assert store.read_archived_events("run_1")


def registry(tmp_path: Path, entries: dict[str, str]) -> Path:
    p = tmp_path / "reconcilers.json"
    p.write_text(
        json.dumps({"probes": {k: {"command": v, "timeout": 10} for k, v in entries.items()}})
    )
    return p


def test_settle_run_settles_archived_action(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("send_invoice", {}, key="invoice:1")
    compact(db)
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=true"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes)
    assert report.settled == 1
    assert report.settled_true
    with SQLiteStorage(db) as store:
        assert store.verify_events("run_1").ok


def test_cli_reconcile_settles_archived_action(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("send_invoice", {}, key="invoice:2")
    compact(db)
    cfg = registry(tmp_path, {"send_invoice": "echo occurred=true"})
    code, out, err = run("--db", db, "--json", "reconcile", "run_1", "--config", str(cfg))
    assert code == ExitCode.OK, err
    assert json.loads(out)["settled_total"] == 1


def test_settle_authority_keeps_consumption_context_after_compact(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "tok-9", via_action_id="action_probe")
    compact(db)
    probe = tmp_path / "authority_probe.py"
    probe.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "assert payload.get('via_action_id') == 'action_probe', payload\n"
        "assert payload.get('consumer_run_id') == 'run_1', payload\n"
        "print('valid=true')\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}"
    probes = {"tok-9": {"command": command, "timeout": 10}}
    with SQLiteStorage(db) as store:
        report = settle_authority(store, "run_1", "tok-9", probes, dry_run=True)
    assert report.valid is True, report.detail
    assert report.settled is False
    with SQLiteStorage(db) as store:
        assert store.verify_events("run_1").ok
