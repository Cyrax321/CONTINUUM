"""Pagination for provenance listings (issue #597, step 1).

--limit/--offset truncate display only. The in-memory graph behind staleness
and validation is always whole, mirroring the tree --limit contract (#321).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def seed_db(path: Path) -> str:
    """A run with a 5-node chain: 2 evidence, 1 decision, 2 actions."""
    db = str(path / "paging.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_1", goal="g"))
        ev1 = store.append_event("run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
        ev2 = store.append_event("run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev2"})
        dec = store.append_event(
            "run_1",
            EventType.DECISION_CREATED,
            {"decision": "d", "decision_id": "dec1", "caused_by": [ev1.event_id]},
        )
        store.append_event(
            "run_1",
            EventType.ACTION_RECORDED,
            {
                "key": "k1",
                "action_id": "a1",
                "action_type": "send",
                "status": "completed",
                "action": {
                    "action_id": "a1",
                    "action_type": "send",
                    "status": "completed",
                    "arguments": {},
                },
                "caused_by": [dec.event_id],
            },
        )
        store.append_event(
            "run_1",
            EventType.ACTION_RECORDED,
            {
                "key": "k2",
                "action_id": "a2",
                "action_type": "send",
                "status": "completed",
                "action": {
                    "action_id": "a2",
                    "action_type": "send",
                    "status": "completed",
                    "arguments": {},
                },
                "caused_by": [ev2.event_id],
            },
        )
    return db


def node_lines(out: str) -> list[str]:
    return [
        line for line in out.splitlines() if line.startswith("  ") and not line.startswith("  ...")
    ]


def test_provenance_limit_truncates_display_only(tmp_path: Path) -> None:
    db = seed_db(tmp_path)
    code, out, err = run_cli("--db", db, "provenance", "run_1", "--limit", "2")
    assert code == ExitCode.OK, err
    assert len(node_lines(out)) == 2
    assert "hidden by paging" in out


def test_provenance_offset_skips_from_the_front(tmp_path: Path) -> None:
    db = seed_db(tmp_path)
    _, full, _ = run_cli("--db", db, "provenance", "run_1")
    code, out, err = run_cli("--db", db, "provenance", "run_1", "--offset", "2")
    assert code == ExitCode.OK, err
    assert len(node_lines(out)) == len(node_lines(full)) - 2


def test_provenance_json_carries_totals(tmp_path: Path) -> None:
    db = seed_db(tmp_path)
    code, out, err = run_cli("--db", db, "--json", "provenance", "run_1", "--limit", "3")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["nodes_total"] == len(payload["nodes"]) + payload["nodes_hidden"]
    assert len(payload["nodes"]) == 3
    assert payload["nodes_hidden"] == payload["nodes_total"] - 3


def test_provenance_limit_zero_is_refused(tmp_path: Path) -> None:
    db = seed_db(tmp_path)
    code, _, err = run_cli("--db", db, "provenance", "run_1", "--limit", "0")
    assert code == ExitCode.ERROR
    assert "--limit must be 1 or more" in err


def test_impact_limit_and_json_totals(tmp_path: Path) -> None:
    db = seed_db(tmp_path)
    code, out, err = run_cli("--db", db, "impact", "run_1", "--evidence", "ev1", "--limit", "1")
    assert code == ExitCode.OK, err
    assert "hidden by paging" in out
    code, out, err = run_cli(
        "--db", db, "--json", "impact", "run_1", "--evidence", "ev1", "--limit", "1"
    )
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert len(payload["downstream"]) == 1
    assert payload["downstream_total"] == 1 + payload["downstream_hidden"]


def test_no_flags_behaves_as_before(tmp_path: Path) -> None:
    db = seed_db(tmp_path)
    code, out, err = run_cli("--db", db, "provenance", "run_1")
    assert code == ExitCode.OK, err
    assert "hidden by paging" not in out
    code, out, err = run_cli("--db", db, "--json", "provenance", "run_1")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["nodes_hidden"] == 0
    assert payload["nodes_total"] == len(payload["nodes"])
