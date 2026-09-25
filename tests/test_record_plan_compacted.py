"""record-plan works on a compacted run (issue #1438).

Compaction archives the pre-anchor prefix of the log, and RUN_STARTED goes
with it. cmd_record_plan read its preflight projection (and the post-write
emission) from the live tail alone, so on a compacted run the tail carried no
goal and the command refused a plan the run could absorb:

    error: plan would leave run unprojectable and was not recorded:
    run '...' has no goal: the log never recorded RUN_STARTED

Both reads now fold read_all_events, matching cmd_replay (#1172) and
cmd_provenance (#554). An archived RUN_STARTED is still a recorded RUN_STARTED.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def make_compacted_db(path: Path, run_id: str = "run_compacted") -> str:
    """A run with RUN_STARTED archived by compaction, leaving a live anchor tail."""
    db = str(path / "record_plan_compacted.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id=run_id, goal="long running task"))
    storage.append_event(
        run_id, EventType.RUN_STARTED, {"goal": "long running task"}, source=Origin.HUMAN
    )
    storage.append_event(
        run_id,
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "phase 1"},
        source=Origin.HUMAN,
    )
    storage.compact_run(run_id)
    storage.close()

    # The precondition these tests exist for: RUN_STARTED is archived, not live.
    with SQLiteStorage(db) as store:
        live = list(store.read_events(run_id))
        archived = list(store.read_archived_events(run_id))
    assert any(e.type is EventType.RUN_STARTED for e in archived)
    assert not any(e.type is EventType.RUN_STARTED for e in live)
    return db


def test_record_plan_succeeds_on_a_compacted_run(tmp_path: Path) -> None:
    """The reported failure: a plan upsert on a run whose goal is archived."""
    db = make_compacted_db(tmp_path)

    code, out, err = run_cli(
        "--db",
        db,
        "record-plan",
        "run_compacted",
        "--plan-id",
        "plan_1",
        "--units",
        json.dumps([{"id": "u1", "title": "step 1", "status": "pending", "depends_on": []}]),
    )

    assert code is ExitCode.OK, err
    assert "upserted 1 unit(s)" in out

    # The plan landed and the whole run still projects from full history.
    with SQLiteStorage(db) as store:
        state = project("run_compacted", store.read_all_events("run_compacted"))
    assert len(state.plan) == 1
    assert state.plan[0].step_id == "u1"
    assert state.goal.description == "long running task"


def test_record_plan_json_reports_the_compacted_run_state(tmp_path: Path) -> None:
    """The emitted projection reflects the archived goal, not an empty live tail."""
    db = make_compacted_db(tmp_path)

    code, out, err = run_cli(
        "--db",
        db,
        "--json",
        "record-plan",
        "run_compacted",
        "--plan-id",
        "plan_1",
        "--units",
        json.dumps(
            [
                {"id": "u1", "title": "step 1", "status": "pending", "depends_on": []},
                {"id": "u2", "title": "step 2", "status": "pending", "depends_on": ["u1"]},
            ]
        ),
    )

    assert code is ExitCode.OK, err
    payload = json.loads(out)
    assert payload["plan_id"] == "plan_1"
    assert payload["units"] == 2
    assert len(payload["plan"]) == 2


def test_record_plan_still_rejects_a_plan_the_run_cannot_absorb(tmp_path: Path) -> None:
    """The preflight gate is not weakened: a run with no goal still refuses."""
    db = str(tmp_path / "record_plan_no_goal.db")
    storage = SQLiteStorage(db)
    # A run row exists, but RUN_STARTED was never recorded, so the full history
    # -- archived or not -- carries no goal for the candidate to project onto.
    storage.create_run(Run(run_id="run_no_goal", goal="never started"))
    storage.close()

    code, _out, err = run_cli(
        "--db",
        db,
        "record-plan",
        "run_no_goal",
        "--plan-id",
        "plan_1",
        "--units",
        json.dumps([{"id": "u1", "title": "step 1", "status": "pending", "depends_on": []}]),
    )

    assert code is ExitCode.ERROR
    assert "plan would leave run unprojectable and was not recorded" in err
    # Nothing was written: the fail-closed half of the preflight contract.
    with SQLiteStorage(db) as store:
        events = [
            e for e in store.read_all_events("run_no_goal") if e.type is EventType.PLAN_UPSERT
        ]
    assert events == []


def test_record_plan_works_on_an_uncompacted_run(tmp_path: Path) -> None:
    """The non-compacted path is unchanged behaviour, not a regression."""
    db = str(tmp_path / "record_plan_live.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_live", goal="g"))

    code, out, err = run_cli(
        "--db",
        db,
        "record-plan",
        "run_live",
        "--plan-id",
        "plan_1",
        "--units",
        json.dumps([{"id": "u1", "title": "step 1", "status": "pending", "depends_on": []}]),
    )

    assert code is ExitCode.OK, err
    assert "upserted 1 unit(s)" in out
