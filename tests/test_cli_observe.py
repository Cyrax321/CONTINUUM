"""Host-side observation hooks: ``continuum observe`` and ``continuum hooks``.

These exist because of issue #207: a Claude Code session wrote its artifact and
was killed before any voluntary recording call, so recovery reported progress
0/1 with zero checkpoints. The observe path makes the recording happen in a
PostToolUse hook, outside the model's control, so work that landed on disk is
never invisible to the next session.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from continuum.cli import ExitCode, main
from continuum.clienthooks import (
    DEFAULT_MATCHER,
    _is_continuum_hook,
    _split_command,
    install_claude_code_hook,
    observe_command,
    remove_claude_code_hook,
)
from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def payload_for(path: Path) -> dict[str, object]:
    return {"tool_name": "Write", "tool_input": {"file_path": str(path)}}


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "demo.db")
    with SQLiteStorage(path) as store:
        store.create_run_started(Run(run_id="run_1", goal="Write the thing"))
    yield path


@pytest.fixture
def written_file(tmp_path: Path) -> Path:
    target = tmp_path / "artifact.txt"
    target.write_text("hello")
    return target


# --- observe ----------------------------------------------------------------- #


def test_an_observation_is_recorded_as_tool_completed_with_file_facts(
    db: str, tmp_path: Path, written_file: Path
) -> None:
    payload_file = tmp_path / "hook.json"
    payload_file.write_text(json.dumps(payload_for(written_file)))

    code, out, err = run("--db", db, "observe", "--payload-file", str(payload_file))
    assert code == ExitCode.OK, err

    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    assert [e.type for e in events] == [EventType.RUN_STARTED, EventType.TOOL_COMPLETED]
    recorded = events[-1]
    assert recorded.source is Origin.EXTERNAL_AGENT
    assert recorded.payload["tool"] == "Write"
    assert recorded.payload["path"] == str(written_file)
    assert recorded.payload["bytes"] == 5  # len(b"hello")
    assert isinstance(recorded.payload["sha256"], str)
    assert len(recorded.payload["sha256"]) == 64


def test_observe_reads_stdin_when_no_payload_file_is_given(
    db: str,
    tmp_path: Path,
    written_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload_for(written_file))))
    code, _, err = run("--db", db, "observe")
    assert code == ExitCode.OK, err

    with SQLiteStorage(db) as store:
        assert store.last_sequence("run_1") == 2


def test_observe_targets_the_requested_run_not_the_active_one(
    tmp_path: Path, written_file: Path
) -> None:
    """The explicit flag must win over active-run fallback selection.

    A second, more recently started run stays in the same database so the
    fallback would pick *it* if the flag were ignored; asserting it received
    nothing is what makes this test discriminate.
    """
    other = str(tmp_path / "other.db")
    with SQLiteStorage(other) as store:
        store.create_run_started(Run(run_id="run_2", goal="Another thing"))
        store.create_run_started(Run(run_id="run_z", goal="Active run"))

    payload_file = tmp_path / "hook.json"
    payload_file.write_text(json.dumps(payload_for(written_file)))
    code, _, err = run(
        "--db", other, "observe", "--run-id", "run_2", "--payload-file", str(payload_file)
    )
    assert code == ExitCode.OK, err

    with SQLiteStorage(other) as store:
        events = store.read_events("run_2")
        assert events[-1].type is EventType.TOOL_COMPLETED
        # The fallback target must have been left untouched.
        assert store.last_sequence("run_z") == 1


def test_observe_with_no_active_run_drops_the_observation_without_failing(
    db: str, tmp_path: Path, written_file: Path
) -> None:
    """Hooks fire for every session in the directory, including ones with no
    CONTINUUM run. Failing them would pressure the user into uninstalling the
    instrumentation, so the drop exits 0 with a visible stderr note."""
    empty = str(tmp_path / "empty.db")

    payload_file = tmp_path / "hook.json"
    payload_file.write_text(json.dumps(payload_for(written_file)))
    code, out, err = run("--db", empty, "observe", "--payload-file", str(payload_file))
    assert code == ExitCode.OK
    assert "No active CONTINUUM run" in err
    assert out == ""

    with SQLiteStorage(empty) as store:
        assert store.list_runs() == []


def test_observe_rejects_an_unknown_run_id(db: str, tmp_path: Path, written_file: Path) -> None:
    payload_file = tmp_path / "hook.json"
    payload_file.write_text(json.dumps(payload_for(written_file)))
    code, _, err = run(
        "--db", db, "observe", "--run-id", "typo", "--payload-file", str(payload_file)
    )
    assert code == ExitCode.NOT_FOUND
    assert "typo" in err


def test_observe_rejects_a_payload_that_is_not_json(db: str, tmp_path: Path) -> None:
    payload_file = tmp_path / "hook.json"
    payload_file.write_text("{not json")
    code, _, err = run("--db", db, "observe", "--payload-file", str(payload_file))
    assert code == ExitCode.ERROR
    assert "not valid JSON" in err

    with SQLiteStorage(db) as store:
        assert store.last_sequence("run_1") == 1


# --- hooks install / remove -------------------------------------------------- #


def test_install_writes_a_single_post_tool_use_entry(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    command = "/bin/continuum observe"

    status = install_claude_code_hook(settings, command)
    assert status == "installed"

    data = json.loads(settings.read_text())
    entry = data["hooks"]["PostToolUse"]
    assert entry == [
        {
            "matcher": DEFAULT_MATCHER,
            "hooks": [{"type": "command", "command": command}],
        }
    ]

    assert install_claude_code_hook(settings, command) == "present"


def test_install_preserves_unrelated_settings_and_entries(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash"}]}})
    )

    install_claude_code_hook(settings, "/bin/continuum observe")
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"] == [{"matcher": "Bash"}]
    assert len(data["hooks"]["PostToolUse"]) == 1


def test_install_repoints_a_stale_command(tmp_path: Path) -> None:
    """A moved virtualenv must self-heal rather than leave a dead binary wired."""
    settings = tmp_path / "settings.json"
    install_claude_code_hook(settings, "/old/venv/bin/continuum observe")

    status = install_claude_code_hook(settings, "/new/venv/bin/continuum observe")
    assert status == "updated"
    data = json.loads(settings.read_text())
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == (
        "/new/venv/bin/continuum observe"
    )


def test_install_refuses_to_overwrite_hand_edited_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{broken")
    with pytest.raises(ValueError, match="not valid JSON"):
        install_claude_code_hook(settings, "/bin/continuum observe")
    # The file survives untouched: a file someone edited by hand is a
    # statement of intent.
    assert settings.read_text() == "{broken"


def test_remove_deletes_only_the_observe_entry(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install_claude_code_hook(settings, "/bin/continuum observe")

    assert remove_claude_code_hook(settings) is True
    data = json.loads(settings.read_text())
    assert "hooks" not in data or "PostToolUse" not in data.get("hooks", {})
    assert remove_claude_code_hook(settings) is False


def test_cli_hooks_commands_round_trip(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"

    code, out, err = run("--json", "hooks", "install", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    installed = json.loads(out)
    assert installed["hooks"][0]["status"] == "installed"
    assert json.loads(settings.read_text())["hooks"]["PostToolUse"]

    code, out, _ = run("--json", "hooks", "install", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK
    assert json.loads(out)["hooks"][0]["status"] == "present"

    code, out, _ = run("--json", "hooks", "remove", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK
    assert json.loads(out)["removed"] is True


def test_installed_command_actually_records_through_the_real_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The string baked into settings.json must work as-is: this drives the
    real `continuum` executable (or interpreter fallback) through a shell,
    exactly as Claude Code would."""
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    monkeypatch.chdir(project)

    # Prepend the worktree's src so every subprocess below (including the
    # shell-run baked command, whose interpreter fallback resolves `continuum`
    # through PYTHONPATH) imports the tree under test, not an installed copy
    # (issue #837).
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [str(Path(__file__).resolve().parents[1] / "src"), os.environ.get("PYTHONPATH")],
            )
        ),
    }
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "init"], check=True, capture_output=True, env=env
    )
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "start", "demo", "--goal", "write things"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "hooks", "install", "claude-code"],
        check=True,
        capture_output=True,
        env=env,
    )
    artifact = project / "out.txt"
    artifact.write_text("done")

    # The command baked into the settings must work as-is through a shell,
    # exactly as Claude Code would run it.
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    hook_input = json.dumps(payload_for(artifact))
    result = subprocess.run(
        command, input=hook_input, capture_output=True, text=True, shell=True, env=env
    )
    assert result.returncode == ExitCode.OK, result.stderr

    with SQLiteStorage(str(project / "continuum.db")) as store:
        events = store.read_events("demo")
    assert events[-1].type is EventType.TOOL_COMPLETED
    assert events[-1].payload["path"] == str(artifact)
    assert shlex.split(command)[-1] == "observe"


def test_the_baked_command_survives_a_round_trip_through_the_host_quoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quoting must match the shell that will run the hook, on either platform.

    The path is forced to contain a space because that is what exposed the
    original bug: ``shlex.quote`` wraps such a path in single quotes, which
    ``cmd.exe`` has no syntax for, so every hook installed on Windows failed
    with "The filename, directory name, or volume label syntax is incorrect"
    and the durability the hook exists to provide was silently absent.
    Asserting the round-trip rather than a literal string keeps the test
    meaningful on POSIX, where the correct quoting is the other one.
    """
    spaced = str(Path("D:/pROJ OPEN/CONTINUUM/.venv/Scripts/continuum.exe"))
    monkeypatch.setattr("continuum.clienthooks.shutil.which", lambda _: spaced)

    command = observe_command(db=None)
    assert _split_command(command) == [spaced, "observe"]
    assert _is_continuum_hook({"command": command}, "observe")

    with_db = observe_command(db="C:/some dir/continuum.db")
    assert _split_command(with_db) == [spaced, "--db", "C:/some dir/continuum.db", "observe"]


def test_a_hook_written_by_an_older_version_is_still_recognised() -> None:
    """``hooks remove`` has to clean up after an upgrade.

    Before the quoting fix every command was POSIX-quoted regardless of
    platform, so a settings file written by an older CONTINUUM still contains
    that form. Failing to recognise it would leave the stale hook behind and
    let ``hooks install`` add a duplicate alongside it.
    """
    legacy = r"'D:\pROJ OPEN\CONTINUUM\.venv\Scripts\python.exe' -m continuum.cli observe"
    assert _is_continuum_hook({"command": legacy}, "observe")
    assert not _is_continuum_hook({"command": "some-other-tool observe"}, "observe")
