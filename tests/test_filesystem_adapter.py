from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import Mock

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import FilesystemSandboxAdapter
from continuum.models import Run
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


def _adapter(tmp_path: pathlib.Path) -> tuple[FilesystemSandboxAdapter, SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="sandbox"))
    return FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox")), storage


def test_init_creates_sandbox_dir_when_absent(tmp_path: pathlib.Path) -> None:
    storage = SQLiteStorage(":memory:")
    sandbox_dir = tmp_path / "deeply" / "nested" / "sandbox"
    assert not sandbox_dir.exists()

    adapter = FilesystemSandboxAdapter(storage, str(sandbox_dir))

    assert sandbox_dir.exists()
    assert sandbox_dir.is_dir()
    assert adapter.sandbox_dir == sandbox_dir


def test_init_accepts_custom_recovery_engine(tmp_path: pathlib.Path) -> None:
    storage = SQLiteStorage(":memory:")
    engine = RecoveryEngine(storage)

    adapter = FilesystemSandboxAdapter(
        storage,
        str(tmp_path / "sandbox"),
        engine=engine,
    )

    assert adapter.engine is engine


def test_run_shell_executes_in_sandbox(tmp_path: pathlib.Path) -> None:
    adapter, _ = _adapter(tmp_path)
    result = adapter.run_shell("run_1", "echo hello")
    assert result.status == "completed"
    assert result.output.strip() == "hello"


def test_run_shell_is_recorded(tmp_path: pathlib.Path) -> None:
    adapter, storage = _adapter(tmp_path)
    adapter.run_shell("run_1", "echo hi")
    recorded = ActionLedger(storage, "run_1").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "shell"


def test_run_shell_passes_dep_scope(tmp_path: pathlib.Path) -> None:
    adapter, storage = _adapter(tmp_path)
    adapter.run_shell("run_1", "echo x", dep_scope="numpy")
    recorded = ActionLedger(storage, "run_1").all()
    assert recorded[0].dep_scope == "numpy"


def test_run_shell_accepts_an_argv_list_without_a_shell(
    tmp_path: pathlib.Path,
) -> None:
    """The argv form is shell-free, so it runs identically on every platform.

    echo only happens to work under both shell families; a command like
    python -c "print(1)" or any redirection is family-specific, which is
    why the list form exists (#842).
    """
    adapter, storage = _adapter(tmp_path)
    result = adapter.run_shell("run_1", [sys.executable, "-c", "print('argv ok')"])
    assert result.status == "completed"
    assert result.output.strip() == "argv ok"
    recorded = ActionLedger(storage, "run_1").all()
    assert recorded[0].action_type == "shell"
    assert recorded[0].arguments["command"] == [
        sys.executable,
        "-c",
        "print('argv ok')",
    ]


def test_run_shell_argv_list_passes_shell_false(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = _adapter(tmp_path)
    command = [sys.executable, "-c", "print('argv check')"]
    run_spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr("continuum.adapters.filesystem.subprocess.run", run_spy)

    result = adapter.run_shell("run_1", command)

    assert result.status == "completed"
    assert run_spy.call_args.kwargs["shell"] is False


def test_run_shell_string_command_passes_shell_true(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = _adapter(tmp_path)
    command = "echo shell_check"
    run_spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr("continuum.adapters.filesystem.subprocess.run", run_spy)

    result = adapter.run_shell("run_1", command)

    assert result.status == "completed"
    assert run_spy.call_args.kwargs["shell"] is True


def test_run_shell_captures_nonzero_exit_code_failure(
    tmp_path: pathlib.Path,
) -> None:
    adapter, storage = _adapter(tmp_path)
    command = [sys.executable, "-c", "import sys; sys.exit(42)"]

    result = adapter.run_shell("run_1", command)

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error
    assert "42" in result.error

    pending = ActionLedger(storage, "run_1").pending()
    assert len(pending) == 1
    assert pending[0].action_type == "shell"


def test_run_shell_executes_with_workdir_cwd(tmp_path: pathlib.Path) -> None:
    adapter, _ = _adapter(tmp_path)
    code = "import pathlib; pathlib.Path('marker.txt').write_text('in_sandbox', encoding='utf-8')"
    result = adapter.run_shell("run_1", [sys.executable, "-c", code])

    assert result.status == "completed"
    marker = tmp_path / "sandbox" / "marker.txt"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "in_sandbox"
