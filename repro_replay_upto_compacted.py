"""Repro for issue #1172: replay --upto fails on every compacted run.

Builds a run, replays with --upto successfully, compacts it, then retries.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

from continuum.checkpoint import CheckpointManager
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Run
from continuum.replayguard import protected_call
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def work(db: str, i: int) -> None:
    protected_call(
        SQLiteStorage(db),
        "r1",
        action_type="process_doc",
        key=f"doc:{i}",
        fn=lambda doc=i: {"doc": doc},
    )


def names(db: str, table: str) -> list[str]:
    with SQLiteStorage(db) as store:
        rows = store._connection.execute(
            f"SELECT type FROM {table} WHERE run_id = 'r1' ORDER BY sequence"
        ).fetchall()
    return [r["type"] for r in rows]


def main_repro() -> int:
    tmp = Path(tempfile.mkdtemp())
    db = str(tmp / "repro.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="r1", goal="bisect a compacted run"))
        store.append_event("r1", EventType.RUN_STARTED, {"goal": "bisect a compacted run", "total": 20})
        for i in range(5):
            protected_call(
                store,
                "r1",
                action_type="process_doc",
                key=f"doc:{i}",
                fn=lambda doc=i: {"doc": doc},
            )
        # A mid-run checkpoint, so the archive holds a STATE_CHECKPOINTED too.
        CheckpointManager(store).checkpoint("r1")
    for i in range(5, 9):
        work(db, i)

    print("== before compaction ==")
    code, _, err = run("--db", db, "replay", "r1", "--upto", "11")
    print(f"  replay --upto 11 rc: {code}")
    if code is not ExitCode.OK:
        print(f"  err: {err.strip()}")
        return 1

    print("== compact ==")
    run("--db", db, "compact", "r1", "--force")
    print(f"  archived: {names(db, 'events_archive')}")
    print(f"  live:     {names(db, 'events')}")

    print("== after compaction ==")
    code, _, _ = run("--db", db, "replay", "r1")
    print(f"  replay (no upto) rc: {code}")
    failed = False
    for upto in (11, 12, 13, 999):
        code, _, err = run("--db", db, "replay", "r1", "--upto", str(upto))
        print(f"  replay --upto {upto:>4} rc: {code}")
        if code is not ExitCode.OK:
            failed = True
            print(f"    err: {err.strip()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_repro())
