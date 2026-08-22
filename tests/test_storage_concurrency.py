"""Concurrency guarantees, exercised with real threads and real processes.

The claim under test is narrow and important: **two writers racing to append to
the same run never receive the same sequence number, and never silently
overwrite each other.** One wins, the other is told it lost.

These tests are the reason the engine takes an IMMEDIATE lock and keeps a
UNIQUE constraint on ``(run_id, sequence)``. Without both, a race produces a
forked chain that verifies clean — the worst possible failure, because it looks
correct.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Progress, Run, SemanticState
from continuum.state.semantic import project
from continuum.storage import ConcurrentWriteError, SQLiteStorage


def test_threads_racing_on_one_connection_get_unique_sequences(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))

        def append(index: int) -> int:
            return store.append_event("run_1", EventType.WORK_COMPLETED, {"i": index}).sequence

        with ThreadPoolExecutor(max_workers=8) as pool:
            sequences = sorted(pool.map(append, range(50)))

        assert sequences == list(range(1, 51))
        assert store.verify_events("run_1").ok


def test_threads_on_separate_connections_do_not_fork_the_chain(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as setup:
        setup.create_run(Run(run_id="run_1", goal="g"))

    with SQLiteStorage(db) as setup:
        setup.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 30})

    def worker(index: int) -> int:
        with SQLiteStorage(db) as store:
            return store.append_event("run_1", EventType.WORK_COMPLETED, {"i": index}).sequence

    with ThreadPoolExecutor(max_workers=6) as pool:
        sequences = sorted(pool.map(worker, range(30)))

    assert sequences == list(range(2, 32))
    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert report.ok
        assert report.trusted_through["run_1"] == 31
        # every concurrent append is counted exactly once, none lost or doubled
        assert project("run_1", store.read_events("run_1")).progress.completed == 30


_CHILD = """
import sys
from continuum.events import EventType
from continuum.storage import SQLiteStorage

db, label, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
written = 0
with SQLiteStorage(db) as store:
    for i in range(count):
        store.append_event("run_1", EventType.WORK_COMPLETED, {"by": label, "i": i})
        written += 1
print(written)
"""


def test_separate_processes_append_to_one_chain_without_corruption(tmp_path: Path) -> None:
    """The real crash-recovery scenario: a restarted agent writes to the same file."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))

    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(_CHILD))

    env_path = str(Path(__file__).resolve().parents[1] / "src")
    children = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), label, "15"],
            # Inherit the parent environment; only PYTHONPATH is added, so the
            # child imports continuum from src/. A bare env= drops SystemRoot on
            # Windows and the child dies on `import _overlapped` during startup.
            env={**os.environ, "PYTHONPATH": env_path},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for label in ("alpha", "beta")
    ]
    results = [child.communicate() for child in children]

    for (out, err), child in zip(results, children, strict=True):
        assert child.returncode == 0, f"child failed: {err}"
        assert out.strip() == "15"

    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert report.ok, report.violations
        assert store.last_sequence("run_1") == 30
        events = store.read_events("run_1")
        assert [e.sequence for e in events] == list(range(1, 31))
        # both writers' work is present; neither overwrote the other
        authors = {e.payload["by"] for e in events}
        assert authors == {"alpha", "beta"}


def test_optimistic_concurrency_detects_a_lost_update(tmp_path: Path) -> None:
    """Two readers plan from the same state; the slower one must be refused."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

        observed = store.last_sequence("run_1")  # both writers read 1

        store.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=observed)
        with pytest.raises(ConcurrentWriteError):
            store.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=observed)

        assert store.last_sequence("run_1") == 2


def test_a_failed_write_leaves_no_partial_row(tmp_path: Path) -> None:
    """Rollback must be complete: a rejected append leaves the chain untouched."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        before = store.read_events("run_1")

        with pytest.raises(ConcurrentWriteError):
            store.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=99)

        assert store.read_events("run_1") == before
        assert store.last_sequence("run_1") == 1
        assert store.verify_events("run_1").ok


def test_concurrent_version_commits_do_not_duplicate_a_version(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        from continuum.models import Goal

        def commit(index: int) -> int:
            state = SemanticState(
                run_id="run_1",
                goal=Goal(description="g"),
                progress=Progress(completed=index),
            )
            return store.put_version(state, reason=f"worker {index}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            versions = sorted(pool.map(commit, range(1, 13)))

        assert versions == list(range(0, 12))
        assert list(store.list_versions("run_1")) == list(range(0, 12))


@pytest.mark.skipif(
    mp.get_start_method(allow_none=True) == "fork", reason="fork start method not required"
)
def test_reads_are_not_blocked_by_an_open_write(tmp_path: Path) -> None:
    """WAL mode: an inspector can read a run while the agent is mid-transaction."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as writer:
        writer.create_run(Run(run_id="run_1", goal="g"))
        writer.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

        with SQLiteStorage(db) as reader:
            assert reader.last_sequence("run_1") == 1
            writer.append_event("run_1", EventType.WORK_COMPLETED, {})
            assert reader.last_sequence("run_1") == 2
