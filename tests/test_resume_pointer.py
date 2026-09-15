"""Every run-completion path clears the instant-resume pointer (issue #394).

``.continuum/resume.json`` is written on every checkpoint so a SessionStart
hook can banner the interrupted run without opening the database. A run closed
as completed is no longer interrupted, so closing it must not leave that
pointer naming it - otherwise the next session starts by surfacing a run the
operator already finished. The CLI cleared it; the TUI and the dashboard HITL
button claimed to mirror the CLI and did not, so the cleanup now lives in one
helper every completion path routes through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager, clear_resume_pointer
from continuum.dashboard import hitl
from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.storage import SQLiteStorage
from continuum.tui import model as tui_model

_POINTER = Path(".continuum/resume.json")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _checkpointed(storage: SQLiteStorage, run_id: str, goal: str = "g") -> None:
    storage.create_run(Run(run_id=run_id, goal=goal))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": goal}, source=Origin.HUMAN)
    CheckpointManager(storage).checkpoint(run_id, trigger="manual", reason="test")


def test_no_pointer_is_not_an_error(project: Path) -> None:
    assert not _POINTER.exists()
    assert clear_resume_pointer("r1") is False


def test_a_pointer_naming_another_run_is_left_alone(project: Path) -> None:
    with SQLiteStorage(str(project / "db")) as storage:
        _checkpointed(storage, "r1")
    assert clear_resume_pointer("r2") is False
    assert json.loads(_POINTER.read_text(encoding="utf-8"))["run_id"] == "r1"


def test_a_pointer_naming_the_run_is_removed(project: Path) -> None:
    with SQLiteStorage(str(project / "db")) as storage:
        _checkpointed(storage, "r1")
    assert clear_resume_pointer("r1") is True
    assert not _POINTER.exists()


def test_an_unreadable_pointer_is_tolerated(project: Path) -> None:
    # A truncated or non-JSON file must not make a completion fail: the pointer
    # is a cache, and its absence is the correct fallback.
    _POINTER.parent.mkdir(parents=True, exist_ok=True)
    _POINTER.write_text("{not json", encoding="utf-8")
    assert clear_resume_pointer("r1") is False
    assert _POINTER.exists()  # not our file to delete; just not understood


@pytest.mark.parametrize("contents", ["5", '"r1"', '["r1"]', "null", "true"])
def test_valid_non_object_json_is_tolerated(project: Path, contents: str) -> None:
    # Valid JSON that is not an object has no ``run_id`` to compare against, and
    # ``.get`` on it would raise AttributeError out of a completion path. The
    # pointer is still just a cache, so the failure is swallowed the same way.
    _POINTER.parent.mkdir(parents=True, exist_ok=True)
    _POINTER.write_text(contents, encoding="utf-8")
    assert clear_resume_pointer("r1") is False
    assert _POINTER.exists()


@pytest.mark.parametrize(
    ("closer", "label"),
    [
        (lambda storage, run_id: _cli_complete(storage, run_id), "cli"),
        (lambda storage, run_id: tui_model.complete_run(storage, run_id), "tui"),
        (lambda storage, run_id: hitl.complete_run(storage, run_id), "dashboard"),
    ],
)
def test_every_completion_path_clears_the_pointer(project: Path, closer, label: str) -> None:
    with SQLiteStorage(str(project / "db")) as storage:
        _checkpointed(storage, "r1")
        assert _POINTER.exists()
        closer(storage, "r1")
        assert storage.get_run("r1").status.value == "completed"
    assert not _POINTER.exists(), f"{label} completion left a stale resume pointer"


def _cli_complete(storage: SQLiteStorage, run_id: str) -> None:
    """Drive the real `continuum complete` command, not a reimplementation."""
    import io

    from continuum.cli.main import main as cli_main

    rc = cli_main(["--db", storage.path, "complete", run_id], out=io.StringIO(), err=io.StringIO())
    assert rc == 0, f"continuum complete failed with {rc}"
