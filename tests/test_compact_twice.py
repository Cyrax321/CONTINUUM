"""A run can be compacted more than once (#648).

The anchor checkpoint folded only the live tail, so after the first
compaction RUN_STARTED lived in the archive and the second compact died
with ValueError: could not be anchored. project_current now folds full
history, matching its own contract.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from continuum.actions import ActionLedger
from continuum.checkpoint.manager import CheckpointManager
from continuum.cli import ExitCode, main
from continuum.models import Run
from continuum.storage import SQLiteStorage


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def make_db(path: Path) -> str:
    db = str(path / "compact2.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_1", goal="g"))
    return db


def do_work(db: str, key: str) -> None:
    with SQLiteStorage(db) as store:
        outcome = ActionLedger(store, "run_1").claim("refund", {}, key=key)
        ActionLedger(store, "run_1").complete(outcome.key, external_id="e1", result={})


def test_second_compact_succeeds_after_more_work(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    do_work(db, "k1")
    with SQLiteStorage(db) as store:
        first = store.compact_run("run_1")
        assert first["archived"] > 0
    do_work(db, "k2")
    with SQLiteStorage(db) as store:
        second = store.compact_run("run_1")
        assert second["archived"] > 0
        assert store.verify_events("run_1").ok


def test_checkpoint_works_after_compaction(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    do_work(db, "k1")
    with SQLiteStorage(db) as store:
        store.compact_run("run_1")
    with SQLiteStorage(db) as store:
        checkpoint = CheckpointManager(store).checkpoint("run_1", reason="post-compact")
    assert checkpoint.run_id == "run_1"
    with SQLiteStorage(db) as store:
        assert store.verify_events("run_1").ok


def test_cli_compact_twice_end_to_end(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    do_work(db, "k1")
    code, _, err = run_cli("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err
    do_work(db, "k2")
    code, _, err = run_cli("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err
    with SQLiteStorage(db) as store:
        assert store.verify_events("run_1").ok


def test_replay_upto_between_the_two_anchors_reports_the_window_not_the_boundary(
    tmp_path: Path,
) -> None:
    """The archived first anchor is the trap (#648 meets #1172).

    ``--upto`` reads full history, so a window can contain that archived anchor
    and still end short of the *current* boundary. Detecting anchoring from any
    anchor in the window restored the current checkpoint and folded an empty
    tail over it, answering the state at the boundary when an earlier window
    was asked for: ``completed`` and ``source_sequence`` both reported 34 for
    ``--upto 25`` on a run whose boundary was 34.
    """
    db = make_db(tmp_path)
    for key in ("k1", "k2", "k3"):
        do_work(db, key)
    run_cli("--db", db, "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        first_boundary = max(e.sequence for e in store.read_events("run_1"))
    do_work(db, "k4")
    run_cli("--db", db, "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        boundary = max(e.sequence for e in store.read_archived_events("run_1"))
        assert boundary > first_boundary, "the second compact must move the boundary"

    # A window that contains the first anchor but ends before the new boundary.
    upto = first_boundary + 1
    assert upto <= boundary
    code, out, err = run_cli("--db", db, "--json", "replay", "run_1", "--upto", str(upto))
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["source_sequence"] == upto, "the window asked for, not the boundary"
    assert payload["events_replayed"] == upto
    assert payload["verified"] is True

    # Past the boundary the anchored branch takes over again.
    code, out, err = run_cli("--db", db, "--json", "replay", "run_1", "--upto", str(boundary + 2))
    assert code == ExitCode.OK, err
    assert json.loads(out)["source_sequence"] == boundary + 2
