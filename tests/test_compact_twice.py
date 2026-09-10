"""A run can be compacted more than once (#648).

The anchor checkpoint folded only the live tail, so after the first
compaction RUN_STARTED lived in the archive and the second compact died
with ValueError: could not be anchored. project_current now folds full
history, matching its own contract.
"""

from __future__ import annotations

import io
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
