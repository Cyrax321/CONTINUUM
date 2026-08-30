"""Attempt lesson example: hard kill mid-action, resume sees lesson.

Run this example directly: it will fork a subprocess that crashes with
os._exit(137) mid-action, then the parent resumes and asserts the new session
receives an AttemptLesson without reading the prior transcript.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.summary import record_attempt_lesson
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage


def _child_crash(db_path: str, run_id: str) -> None:
    """Child process: claim an action then hard-kill before completion."""
    from continuum.actions import ActionLedger

    storage = SQLiteStorage(db_path)
    storage.create_run(Run(run_id=run_id, goal="example goal: process mid-action crash"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "example goal"})
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("example.action", {"resource": "r1"}, key="k1")
    print(f"child: claimed {outcome.action.action_id}, now hard-killing", flush=True)
    os._exit(137)


def _parent_resume(db_path: str, run_id: str) -> None:
    """Parent: assess, repair, and verify lesson."""
    from continuum.recovery.engine import RecoveryEngine

    storage = SQLiteStorage(db_path)
    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    print(f"parent: decision mode={decision.mode}, uncertain={len(decision.uncertain_actions)}")
    lesson = record_attempt_lesson(
        storage, run_id, decision, uncertain_actions=decision.uncertain_actions
    )
    print(f"parent: recorded lesson {lesson.attempt_id}: {lesson.falsified}")
    state = project(run_id, storage.read_events(run_id))
    assert state.attempt_lessons
    assert any(item.attempt_id == lesson.attempt_id for item in state.attempt_lessons)
    print(f"parent: project sees {len(state.attempt_lessons)} lesson(s)")
    from continuum.recovery.summary import render_attempt_lesson

    for line in render_attempt_lesson(state.attempt_lessons[0]):
        print(f"  briefing: {line}")
    print("example: PASS - resumed session sees lesson without transcript")
    storage.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "example.db")
        run_id = "run_lesson_example"
        proc = subprocess.Popen([sys.executable, __file__, "--child", db_path, run_id])
        proc.wait()
        print(f"child exited with {proc.returncode} (expected 137 for hard kill)")
        assert proc.returncode == 137
        _parent_resume(db_path, run_id)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _child_crash(sys.argv[2], sys.argv[3])
    else:
        main()
