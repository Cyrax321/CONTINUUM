from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import Mock

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import PythonInProcAdapter
from continuum.models import Run
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_py", goal="test python inproc adapter"))
    return storage


def test_init_creates_workdir_when_absent(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    workdir = tmp_path / "deeply" / "nested" / "workdir"
    assert not workdir.exists()

    adapter = PythonInProcAdapter(storage, str(workdir))

    assert workdir.exists()
    assert workdir.is_dir()
    assert adapter.workdir == workdir


def test_init_accepts_custom_recovery_engine(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    engine = RecoveryEngine(storage)

    adapter = PythonInProcAdapter(
        storage,
        str(tmp_path / "work"),
        engine=engine,
    )

    assert adapter.engine is engine


def test_run_python_success_returns_stdout_and_records(
    tmp_path: pathlib.Path,
) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))

    result = adapter.run_python("run_py", "print('hello from python')")

    assert result.status == "completed"
    assert result.output.strip() == "hello from python"

    recorded = ActionLedger(storage, "run_py").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "python"
    assert recorded[0].arguments == {"code": "print('hello from python')"}


def test_run_python_passes_dep_scope(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))

    adapter.run_python("run_py", "pass", dep_scope="torch")

    recorded = ActionLedger(storage, "run_py").all()
    assert len(recorded) == 1
    assert recorded[0].dep_scope == "torch"


def test_run_python_captures_unhandled_exception_failure(
    tmp_path: pathlib.Path,
) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))

    result = adapter.run_python("run_py", "raise ValueError('boom')")

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error
    assert result.output is None

    pending = ActionLedger(storage, "run_py").pending()
    assert len(pending) == 1
    assert pending[0].action_type == "python"


def test_run_python_captures_nonzero_exit_code_failure(
    tmp_path: pathlib.Path,
) -> None:
    storage = _storage()
    adapter = PythonInProcAdapter(storage, str(tmp_path / "work"))

    result = adapter.run_python("run_py", "import sys; sys.exit(2)")

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error
    assert "2" in result.error

    pending = ActionLedger(storage, "run_py").pending()
    assert len(pending) == 1


def test_run_python_working_directory_isolation(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    workdir = tmp_path / "work"
    adapter = PythonInProcAdapter(storage, str(workdir))

    code = (
        "import pathlib; "
        "pathlib.Path('output.txt').write_text('isolated_execution', encoding='utf-8')"
    )
    result = adapter.run_python("run_py", code)

    assert result.status == "completed"
    output_file = workdir / "output.txt"
    assert output_file.exists()
    assert output_file.read_text(encoding="utf-8") == "isolated_execution"


def test_run_python_invokes_subprocess_with_expected_arguments(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage()
    workdir = tmp_path / "work"
    adapter = PythonInProcAdapter(storage, str(workdir))

    run_spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr("continuum.adapters.python_inproc.subprocess.run", run_spy)

    result = adapter.run_python("run_py", "print('args test')")

    assert result.status == "completed"
    assert run_spy.call_count == 1
    call_args, call_kwargs = run_spy.call_args
    assert call_args[0] == [sys.executable, "-c", "print('args test')"]
    assert call_kwargs["cwd"] == str(workdir)
    assert call_kwargs["capture_output"] is True
    assert call_kwargs["text"] is True
    assert call_kwargs["check"] is True
