from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import Mock

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import FilesystemSandboxAdapter
from continuum.models import ActionStatus, Run
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_fs", goal="test filesystem sandbox adapter"))
    return storage


def test_init_creates_sandbox_dir_if_absent(tmp_path: pathlib.Path) -> None:
    sandbox_dir = tmp_path / "deeply" / "nested" / "sandbox"
    assert not sandbox_dir.exists()

    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(sandbox_dir))

    assert sandbox_dir.is_dir()
    assert adapter.sandbox_dir == sandbox_dir
    assert adapter.storage is storage


def test_init_with_custom_engine(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    engine = Mock(spec=RecoveryEngine)
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"), engine=engine)

    assert adapter.engine is engine


def test_run_shell_with_argv_list(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"))
    command = [sys.executable, "-c", "print('hello from argv')"]
    run_spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr("continuum.adapters.filesystem.subprocess.run", run_spy)

    result = adapter.run_shell("run_fs", command)

    assert run_spy.call_args.kwargs.get("shell") is False
    assert result.status == "completed"
    assert "hello from argv" in result.output
    assert result.error is None

    recorded = ActionLedger(storage, "run_fs").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "shell"
    assert recorded[0].status == ActionStatus.COMPLETED
    assert recorded[0].arguments["command"] == command


def test_run_shell_with_string_command(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"))
    # Use sys.executable in a portable string command
    command = f'"{sys.executable}" -c "print(\'hello from string\')"'
    run_spy = Mock(wraps=subprocess.run)
    monkeypatch.setattr("continuum.adapters.filesystem.subprocess.run", run_spy)

    result = adapter.run_shell("run_fs", command)

    assert run_spy.call_args.kwargs.get("shell") is True
    assert result.status == "completed"
    assert "hello from string" in result.output
    assert result.error is None

    recorded = ActionLedger(storage, "run_fs").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "shell"
    assert recorded[0].status == ActionStatus.COMPLETED
    assert recorded[0].arguments["command"] == command


def test_run_shell_executes_in_sandbox_dir(tmp_path: pathlib.Path) -> None:
    sandbox_dir = tmp_path / "sandbox"
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(sandbox_dir))
    marker_file = sandbox_dir / "marker.txt"
    marker_file.write_text("sandbox_content", encoding="utf-8")

    command = [
        sys.executable,
        "-c",
        "import pathlib; print(pathlib.Path('marker.txt').read_text(encoding='utf-8'))",
    ]
    result = adapter.run_shell("run_fs", command)

    assert result.status == "completed"
    assert "sandbox_content" in result.output


def test_run_shell_forwards_dep_scope(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"))
    command = [sys.executable, "-c", "print('scoped execution')"]

    result = adapter.run_shell("run_fs", command, dep_scope="fs.scoped_dep")

    assert result.status == "completed"
    recorded = ActionLedger(storage, "run_fs").all()
    assert len(recorded) == 1
    assert recorded[0].dep_scope == "fs.scoped_dep"


def test_run_shell_captures_called_process_error(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"))
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('fatal command error'); sys.exit(42)",
    ]

    # Command failure must be captured into AdapterResult rather than raised
    result = adapter.run_shell("run_fs", command)

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error
    assert "42" in result.error

    # The attempt is still recorded in the ledger as pending/uncertain
    pending = ActionLedger(storage, "run_fs").pending()
    assert len(pending) == 1
    assert pending[0].action_type == "shell"
    assert pending[0].arguments["command"] == command


def test_run_shell_captures_mocked_subprocess_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"))

    def mock_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=127,
            cmd=["fake_binary"],
            output="",
            stderr="command not found",
        )

    monkeypatch.setattr("continuum.adapters.filesystem.subprocess.run", mock_run)

    result = adapter.run_shell("run_fs", ["fake_binary"])

    assert result.status == "failed"
    assert result.error is not None
    assert "CalledProcessError" in result.error
    assert "127" in result.error
    assert len(ActionLedger(storage, "run_fs").pending()) == 1


def test_run_shell_surfaces_the_failed_command_stderr(tmp_path: pathlib.Path) -> None:
    """The message the process printed is the message the operator needs (#1760).

    The transport already pays for a full pipe to drain stderr; the error used
    to carry only the exit status, so a failed action's only diagnostic was
    "exited 42" and the agent loop re-planned blind against the same error.
    """
    storage = _storage()
    adapter = FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox"))
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('which import failed, says the compiler'); sys.exit(42)",
    ]

    result = adapter.run_shell("run_fs", command)

    assert result.status == "failed"
    assert "which import failed, says the compiler" in (result.error or "")

    # The durable record carries the same tail, not just the in-memory result.
    [entry] = ActionLedger(storage, "run_fs").pending()
    assert "which import failed, says the compiler" in (entry.last_error or "")
