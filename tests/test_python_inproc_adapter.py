from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import Mock

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import PythonInProcAdapter
from continuum.models import ActionStatus, Run
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_py", goal="test python inproc adapter"))
    return storage


def test_init_creates_workdir_if_absent(tmp_path: pathlib.Path) -> None:
    workdir = tmp_path / "deeply" / "nested" / "workdir"
    assert not workdir.exists()

    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(workdir))

    assert workdir.is_dir()
    assert adapter.workdir == workdir


def test_init_with_custom_engine(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    engine = RecoveryEngine(storage)
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"), engine=engine)

    assert adapter.engine is engine


def test_run_python_happy_path(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))
    code = "print('hello from inproc')"

    result = adapter.run_python("run_py", code)

    assert result.status == "completed"
    assert "hello from inproc" in result.output
    assert result.error is None

    recorded = ActionLedger(storage, "run_py").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "python"
    assert recorded[0].status == ActionStatus.COMPLETED
    assert recorded[0].arguments["code"] == code


def test_run_python_executes_in_workdir(tmp_path: pathlib.Path) -> None:
    workdir = tmp_path / "work"
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(workdir))
    marker_file = workdir / "marker.txt"
    marker_file.write_text("inproc_payload", encoding="utf-8")

    code = "with open('marker.txt') as f: print(f.read())"
    result = adapter.run_python("run_py", code)

    assert result.status == "completed"
    assert "inproc_payload" in result.output
    assert result.error is None


def test_run_python_forwards_dep_scope(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))

    result = adapter.run_python("run_py", "pass", dep_scope="inproc.scoped_dep")

    assert result.status == "completed"
    recorded = ActionLedger(storage, "run_py").all()
    assert len(recorded) == 1
    assert recorded[0].dep_scope == "inproc.scoped_dep"


def test_run_python_captures_unhandled_exception(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))
    code = "raise ValueError('script failure')"

    # Unhandled exceptions in script must be captured into AdapterResult rather than raised
    result = adapter.run_python("run_py", code)

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error

    # The action attempt is recorded in the ledger as uncertain/pending
    pending = ActionLedger(storage, "run_py").pending()
    assert len(pending) == 1
    assert pending[0].action_type == "python"
    assert pending[0].arguments["code"] == code


def test_run_python_captures_nonzero_exit_code(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))
    code = "import sys; sys.exit(2)"

    result = adapter.run_python("run_py", code)

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error
    assert "2" in result.error

    pending = ActionLedger(storage, "run_py").pending()
    assert len(pending) == 1
    assert pending[0].action_type == "python"


def test_run_python_captures_mocked_subprocess_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))

    def mock_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=[sys.executable, "-c", "pass"],
            output="",
            stderr="simulated execution failure",
        )

    monkeypatch.setattr("continuum.adapters.python_inproc.subprocess.run", mock_run)

    result = adapter.run_python("run_py", "pass")

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error


def test_run_python_invokes_interpreter_contract(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage()
    workdir = tmp_path / "work"
    adapter = PythonInProcAdapter(storage, str(workdir))
    run_spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr("continuum.adapters.python_inproc.subprocess.run", run_spy)

    result = adapter.run_python("run_py", "print('spy_test')")

    assert result.status == "completed"
    assert run_spy.call_count == 1
    called_cmd = run_spy.call_args.args[0]
    called_kwargs = run_spy.call_args.kwargs
    assert called_cmd == [sys.executable, "-c", "print('spy_test')"]
    assert called_kwargs.get("cwd") == str(workdir)
    assert called_kwargs.get("capture_output") is True
    assert called_kwargs.get("text") is True
    assert called_kwargs.get("check") is True
