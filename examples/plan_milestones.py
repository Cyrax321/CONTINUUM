"""Plan milestones example (issue #312)."""

from __future__ import annotations

import tempfile

from continuum.events import EventType
from continuum.models import Run
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage


def main() -> None:
    db = tempfile.mktemp(suffix=".db")
    storage = SQLiteStorage(db)
    run_id = "plan-run-1"
    storage.create_run(Run(run_id=run_id, goal="5 milestones"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "5 milestones"})
    units = [
        {
            "id": f"u{i}",
            "title": f"milestone {i}",
            "status": "pending",
            "depends_on": [f"u{i - 1}"] if i > 1 else [],
        }
        for i in range(1, 6)
    ]
    storage.append_event(run_id, EventType.PLAN_UPSERT, {"plan_id": "p1", "units": units})
    print("initial plan:", [p.step_id for p in project(run_id, storage.read_events(run_id)).plan])
    storage.append_event(
        run_id,
        EventType.PLAN_UPSERT,
        {
            "plan_id": "p1",
            "units": [
                {"id": "u1", "title": "milestone 1", "status": "done", "depends_on": []},
                {"id": "u2", "title": "milestone 2", "status": "done", "depends_on": ["u1"]},
            ],
        },
    )
    state = project(run_id, storage.read_events(run_id))
    print("after 2 done:", [(p.step_id, p.status.value) for p in state.plan])
    storage2 = SQLiteStorage(db)
    state2 = project(run_id, storage2.read_events(run_id))
    remaining = [p for p in state2.plan if p.status.value != "completed"]
    print("resume remaining:", [p.step_id for p in remaining])
    assert [p.step_id for p in remaining] == ["u3", "u4", "u5"]
    print("zero duplicate work for completed units across crash: PASS")


if __name__ == "__main__":
    main()
