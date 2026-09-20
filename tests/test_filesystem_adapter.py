from continuum.actions.ledger import ActionLedger
from continuum.adapters import FilesystemSandboxAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _adapter(tmp_path):
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="sandbox"))
    return FilesystemSandboxAdapter(storage, str(tmp_path / "sandbox")), storage


def test_run_shell_executes_in_sandbox(tmp_path) -> None:
    adapter, _ = _adapter(tmp_path)
    result = adapter.run_shell("run_1", "echo hello")
    assert result.status == "completed"
    assert result.output.strip() == "hello"


def test_run_shell_is_recorded(tmp_path) -> None:
    adapter, storage = _adapter(tmp_path)
    adapter.run_shell("run_1", "echo hi")
    recorded = ActionLedger(storage, "run_1").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "shell"


def test_run_shell_passes_dep_scope(tmp_path) -> None:
    adapter, storage = _adapter(tmp_path)
    adapter.run_shell("run_1", "echo x", dep_scope="numpy")
    recorded = ActionLedger(storage, "run_1").all()
    assert recorded[0].dep_scope == "numpy"


def test_run_shell_accepts_an_argv_list_without_a_shell(tmp_path) -> None:
    """The argv form is shell-free, so it runs identically on every platform.

    ``echo`` only happens to work under both shell families; a command like
    ``python -c "print(1)"`` or any redirection is family-specific, which is
    why the list form exists (#842).
    """
    import sys

    adapter, storage = _adapter(tmp_path)
    result = adapter.run_shell("run_1", [sys.executable, "-c", "print('argv ok')"])
    assert result.status == "completed"
    assert result.output.strip() == "argv ok"
    recorded = ActionLedger(storage, "run_1").all()
    assert recorded[0].action_type == "shell"
    assert recorded[0].arguments["command"] == [sys.executable, "-c", "print('argv ok')"]
