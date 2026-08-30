"""Plan upsert (issue #312) - 7 tests plus property."""

from __future__ import annotations

import json
from pathlib import Path

from continuum.events import EventType
from continuum.interchange import export_semantic_state, import_semantic_state
from continuum.models import Origin, Run, StateStatus
from continuum.state.semantic import project
from continuum.state.validator import StateValidator
from continuum.storage import SQLiteStorage


def _run(tmp_path: Path, run_id: str = "run_1") -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return storage


def test_latest_write_wins_for_same_plan_id_unit_id(tmp_path: Path) -> None:
    storage = _run(tmp_path)
    storage.append_event(
        "run_1",
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [{"id": "u1", "title": "first", "status": "pending", "depends_on": []}],
        },
    )
    storage.append_event(
        "run_1",
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [{"id": "u1", "title": "updated", "status": "done", "depends_on": []}],
        },
    )
    state = project("run_1", storage.read_events("run_1"))
    assert len(state.plan) == 1
    assert state.plan[0].step_id == "u1"
    assert state.plan[0].description == "updated"
    assert state.plan[0].status.value == "completed"
    storage.close()


def test_hash_chain_covers_plan_events(tmp_path: Path) -> None:
    storage = _run(tmp_path)
    storage.append_event(
        "run_1",
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [{"id": "u1", "title": "t", "status": "pending", "depends_on": []}],
        },
    )
    report = storage.verify_events("run_1")
    assert report.ok
    events = list(storage.read_events("run_1"))
    plan_event = next(e for e in events if e.type == EventType.PLAN_UPSERT)
    tampered = plan_event.model_copy(
        update={
            "payload": {
                "plan_id": "p1",
                "units": [{"id": "u1", "title": "tampered", "status": "done", "depends_on": []}],
            }
        }
    )
    assert tampered.hash != tampered.digest()
    storage.close()


def test_project_with_no_plan_yields_empty(tmp_path: Path) -> None:
    storage = _run(tmp_path)
    state = project("run_1", storage.read_events("run_1"))
    assert state.plan == []
    storage.close()


def test_validator_invalidates_stale_and_downstream_but_not_unrelated(tmp_path: Path) -> None:
    storage2 = _run(tmp_path, "run_2")
    storage2.append_event(
        "run_2",
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [
                {"id": "u1", "title": "a", "status": "pending", "depends_on": ["missing"]},
                {"id": "u2", "title": "b", "status": "pending", "depends_on": ["u1"]},
                {"id": "u3", "title": "c", "status": "pending", "depends_on": ["u2"]},
                {"id": "u4", "title": "d", "status": "pending", "depends_on": []},
            ],
        },
    )
    state2 = project("run_2", storage2.read_events("run_2"))
    validator = StateValidator()
    outcome = validator.validate(state2)
    assert any(
        e.component.value == "plan"
        and e.component_id == "u1"
        and e.status == StateStatus.CONFLICTED
        for e in outcome.report.statuses
    )
    assert any(
        e.component.value == "plan" and e.component_id == "u2" and e.status == StateStatus.STALE
        for e in outcome.report.statuses
    )
    assert any(
        e.component.value == "plan" and e.component_id == "u3" and e.status == StateStatus.STALE
        for e in outcome.report.statuses
    )
    assert not any(
        e.component.value == "plan"
        and e.component_id == "u4"
        and e.status in (StateStatus.STALE, StateStatus.CONFLICTED)
        for e in outcome.report.statuses
    )
    storage2.close()


def test_mcp_sets_external_agent_and_requires_review(tmp_path: Path) -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "run_1",
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [{"id": "u1", "title": "t", "status": "pending", "depends_on": []}],
        },
        source=Origin.EXTERNAL_AGENT,
    )
    state = project("run_1", storage.read_events("run_1"))
    assert state.plan[0].provenance.origin == Origin.EXTERNAL_AGENT
    validator = StateValidator()
    outcome = validator.validate(state)
    assert any(
        e.component.value == "plan" and e.status == StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )
    storage.close()


def test_interchange_round_trip_preserves_plan(tmp_path: Path) -> None:
    storage = _run(tmp_path)
    storage.append_event(
        "run_1",
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [
                {"id": "u1", "title": "t", "status": "done", "depends_on": []},
                {"id": "u2", "title": "t2", "status": "pending", "depends_on": ["u1"]},
            ],
        },
    )
    state = project("run_1", storage.read_events("run_1"))
    payload = export_semantic_state(state)
    restored = import_semantic_state(payload)
    assert len(restored.plan) == 2
    assert restored.plan[0].step_id == "u1"
    assert restored.plan[1].step_id == "u2"
    assert restored.plan[0].status.value == "completed"
    storage.close()


def test_cli_round_trip(tmp_path: Path) -> None:
    import io

    from continuum.cli import main
    from continuum.cli.exitcodes import ExitCode

    db = str(tmp_path / "cli.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    storage.close()
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            [
                {"id": "u1", "title": "first", "status": "pending", "depends_on": []},
                {"id": "u2", "title": "second", "status": "working", "depends_on": ["u1"]},
            ]
        ),
        encoding="utf-8",
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--db", db, "record-plan", "run_1", "--plan-id", "p1", "--file", str(plan_file)],
        out=out,
        err=err,
    )
    assert code == ExitCode.OK, err.getvalue()
    out2, err2 = io.StringIO(), io.StringIO()
    code2 = main(["--db", db, "--json", "inspect", "run_1"], out=out2, err=err2)
    assert code2 == ExitCode.OK
    data = json.loads(out2.getvalue())
    assert "plan" in data
    assert len(data["plan"]) == 2


def test_property_random_ordering_deterministic(tmp_path: Path) -> None:
    storage = _run(tmp_path, "run_a")
    storage2 = _run(tmp_path, "run_b")
    units_a = [
        {"id": "u2", "title": "b", "status": "pending", "depends_on": []},
        {"id": "u1", "title": "a", "status": "pending", "depends_on": []},
    ]
    units_b = [
        {"id": "u1", "title": "a", "status": "pending", "depends_on": []},
        {"id": "u2", "title": "b", "status": "pending", "depends_on": []},
    ]
    storage.append_event("run_a", EventType.PLAN_UPSERT, {"plan_id": "p1", "units": units_a})
    storage2.append_event("run_b", EventType.PLAN_UPSERT, {"plan_id": "p1", "units": units_b})
    state = project("run_a", storage.read_events("run_a"))
    state2 = project("run_b", storage2.read_events("run_b"))
    assert [p.step_id for p in state.plan] == ["u1", "u2"]
    assert [p.step_id for p in state2.plan] == ["u1", "u2"]
    storage.close()
    storage2.close()
