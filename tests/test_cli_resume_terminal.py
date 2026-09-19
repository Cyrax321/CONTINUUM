"""resume on a terminal run must not exit 0 (issue #1197).

``continuum resume "$RUN" && ./start-agent.sh`` is the line the exit-code
contract exists to protect: only a verified safe-to-resume run exits 0. A
terminal run -- completed, crashed, aborted or failed -- has nothing to
resume, but cmd_resume never inspected the run's status, so the folded log of
a cleanly closed run assessed as RESUME and the command exited 0 with
"Next permitted action: continue". The automation then launched an agent onto
closed state, and --repair appended RECOVERY_STARTED to a finished log.

get_active_run already excludes these statuses from the implicit target
("a completed run is terminal: it can never be offered for resume"); the
explicit-id path now refuses the same set.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.cli import main
from continuum.cli.exitcodes import ExitCode
from continuum.events import EventType
from continuum.models import Run, RunStatus
from continuum.storage import SQLiteStorage

TERMINAL_STATUSES = (
    RunStatus.COMPLETED,
    RunStatus.CRASHED,
    RunStatus.ABORTED,
    RunStatus.FAILED,
)


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _seed(db: str, run_id: str, status: RunStatus) -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id=run_id, goal="ship it"))
        store.append_event(run_id, EventType.RUN_STARTED, {"goal": "ship it"})
        row = store.get_run(run_id).model_copy(update={"status": status})
        store.update_run(row)


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "terminal.db")


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_run_does_not_exit_zero(db: str, status: RunStatus) -> None:
    """The one contract: a finished run is never a permission to continue."""
    _seed(db, "run_closed", status)

    code, out, _err = run("--db", db, "resume", "run_closed")

    assert code != ExitCode.OK, f"a {status.value} run exited 0"
    assert "Recovery decision: NOT_RESUMABLE" in out
    assert status.value in out


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_run_json_reports_unsafe(db: str, status: RunStatus) -> None:
    """A machine consumer reads safe/mode, so neither may claim resumable."""
    _seed(db, "run_closed", status)

    code, out, _err = run("--db", db, "--json", "resume", "run_closed")
    payload = json.loads(out)

    assert code != ExitCode.OK
    assert payload["safe"] is False
    assert payload["mode"] != "resume"
    assert payload["status"] == status.value
    assert payload["terminal"] is True


def test_repair_on_a_terminal_run_appends_nothing(db: str) -> None:
    """--repair must not record a repair plan onto a closed event log."""
    _seed(db, "run_closed", RunStatus.COMPLETED)

    code, _out, _err = run("--db", db, "resume", "run_closed", "--repair")

    assert code != ExitCode.OK
    with SQLiteStorage(db) as store:
        types = {e.type for e in store.read_events("run_closed")}
    assert EventType.RECOVERY_STARTED not in types


def test_an_active_run_still_exits_zero(db: str) -> None:
    """The guard must not make a live run look finished."""
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_live", goal="ship it"))
        store.append_event("run_live", EventType.RUN_STARTED, {"goal": "ship it"})

    code, out, _err = run("--db", db, "resume", "run_live")

    assert code == ExitCode.OK, f"an active run did not exit 0: {out}"
