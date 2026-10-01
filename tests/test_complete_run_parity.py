"""Parity of the three ways a human closes a run (issue #1153).

``continuum complete``, the TUI's complete verb, and the dashboard's HITL
button are the only surfaces that close a run as completed from a human. Only
the CLI used to perform the whole verb: the TUI left
``.continuum/resume.json`` pointing at the run that had just been finished,
and the dashboard skipped both the file and the ``REVIEW_CONFIRMED`` that
clears self-certification. All three now share one tail in
``continuum.runs.close_run``, and these tests pin the invariant: the same two
events in the same order with the same provenance, the same row flip, the
resume pointer cleared when it names the closed run, and every other run's
pointer left untouched.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.cli import main as cli_main
from continuum.dashboard import hitl
from continuum.events import EventType
from continuum.models import Origin, Run, RunStatus
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage
from continuum.tui import TuiApp

RESUME = Path(".continuum/resume.json")


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """resume.json is resolved relative to the cwd, as checkpointing does."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "parity.db")


def seed(db: str, run_id: str = "r1", goal: str = "ship the thing") -> None:
    """An externally-driven run: self-certified until a human confirms it."""
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id=run_id, goal=goal))
        store.append_event(
            run_id, EventType.RUN_STARTED, {"goal": goal}, source=Origin.EXTERNAL_AGENT
        )
        store.append_event(
            run_id,
            EventType.TASK_UPDATED,
            {"completed": 1, "total": 1, "failed": 0},
            source=Origin.EXTERNAL_AGENT,
        )


def checkpoint(db: str, run_id: str = "r1") -> None:
    """Write resume.json the way a real session does: via a checkpoint."""
    with SQLiteStorage(db) as store:
        CheckpointManager(store).checkpoint(run_id, trigger="manual", reason="test")


def write_resume(run_id: str) -> None:
    RESUME.parent.mkdir(parents=True, exist_ok=True)
    RESUME.write_text(json.dumps({"run_id": run_id, "checkpoint_id": "cp_1"}), encoding="utf-8")


def read_resume() -> dict:
    return json.loads(RESUME.read_text(encoding="utf-8"))


def complete_via_cli(db: str, run_id: str) -> int:
    out, err = io.StringIO(), io.StringIO()
    return cli_main(["--db", db, "complete", run_id], out=out, err=err)


def complete_via_tui(db: str, run_id: str) -> str:
    app = TuiApp(SQLiteStorage(db))
    assert app.handle_key(" ") is True  # dismiss the splash
    assert app.rows[app.index].run_id == run_id
    assert app.handle_key("x") is True
    assert "RUN_COMPLETED" in app.pending[0]
    assert app.handle_key("y") is True  # confirm the write
    return app.message


def complete_via_dashboard(db: str, run_id: str, summary: str = "") -> None:
    with SQLiteStorage(db) as store:
        hitl.complete_run(store, run_id, summary=summary)


# The closed event log, asserted once per surface: REVIEW_CONFIRMED before
# RUN_COMPLETED, both human, and the run row flipped to COMPLETED.
def assert_closed_log(db: str, run_id: str, closed_by: str, summary: str = "") -> None:
    with SQLiteStorage(db) as store:
        types = [event.type for event in store.read_events(run_id)]
        assert types[-2:] == [EventType.REVIEW_CONFIRMED, EventType.RUN_COMPLETED]
        events = store.read_events(run_id)
        review = events[-2]
        completed = events[-1]
        assert review.source is Origin.HUMAN
        assert review.payload["components"] == ["goal", "progress"]
        assert completed.source is Origin.HUMAN
        assert completed.payload["closed_by"] == closed_by
        assert ("summary" in completed.payload) == bool(summary)
        assert store.get_run(run_id).status is RunStatus.COMPLETED


SURFACES = [
    pytest.param(complete_via_cli, "cli", id="cli"),
    pytest.param(complete_via_tui, "tui", id="tui"),
    pytest.param(complete_via_dashboard, "dashboard", id="dashboard"),
]


@pytest.mark.parametrize("complete,closed_by", SURFACES)
def test_every_surface_clears_the_resume_pointer_it_named(
    db: str, complete, closed_by: str
) -> None:
    """A completed run is no longer interrupted, so its resume file is gone."""
    seed(db)
    checkpoint(db)  # this is what writes .continuum/resume.json
    assert read_resume()["run_id"] == "r1"

    complete(db, "r1")

    assert not RESUME.exists()
    assert_closed_log(db, "r1", closed_by)


@pytest.mark.parametrize("complete,closed_by", SURFACES)
def test_no_surface_clobbers_another_runs_pointer(db: str, complete, closed_by: str) -> None:
    """Closing r1 must never delete a resume file naming r2.

    The delete is conditional on the run id for exactly this reason: two runs
    share one cwd, and the instant-resume file tracks whichever run was
    checkpointed last.
    """
    seed(db)
    write_resume("r2")  # a different run is the interrupted one

    complete(db, "r1")

    assert read_resume()["run_id"] == "r2"
    assert_closed_log(db, "r1", closed_by)


@pytest.mark.parametrize("complete,closed_by", SURFACES)
def test_every_surface_clears_self_certification(db: str, complete, closed_by: str) -> None:
    """REVIEW_CONFIRMED is the point of closing from a human, not a side effect.

    An externally-driven run reports its own goal and progress, so both stay
    self-certified until a human confirms them. The dashboard used to close
    such a run without ever landing that event, leaving it certified by the
    agent that did the work.
    """
    seed(db)

    complete(db, "r1")

    with SQLiteStorage(db) as store:
        statuses = {
            entry.component.value: entry.status.value
            for entry in RecoveryEngine(store).assess("r1").validation.report.statuses
        }
    assert statuses["goal"] == "valid"
    assert statuses["progress"] == "valid"


def test_the_cli_short_circuits_an_already_completed_run(db: str) -> None:
    """The CLI's idempotency guard survives the refactor: no duplicate events."""
    seed(db)
    assert complete_via_cli(db, "r1") == 0
    assert complete_via_cli(db, "r1") == 0

    with SQLiteStorage(db) as store:
        events = store.read_events("r1")
    assert sum(event.type is EventType.RUN_COMPLETED for event in events) == 1


def test_an_unreadable_resume_file_does_not_block_completing(db: str) -> None:
    """A corrupt resume.json is swallowed, not surfaced: closing still succeeds."""
    seed(db)
    write_resume("r1")
    RESUME.write_text("{not json", encoding="utf-8")

    complete_via_dashboard(db, "r1")

    assert_closed_log(db, "r1", "dashboard")
