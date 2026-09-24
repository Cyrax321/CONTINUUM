"""Tests for ``continuum mcp install`` / ``mcp remove`` (issue #834).

The command exists for the failure states the issue reproduced: on Windows a
committed ``.mcp.json`` points at a POSIX path that does not exist, and a bare
command name is resolved by ``CreateProcess`` against the *host's* PATH, so a
healthy install surfaces as ``CONNECTION_CLOSED``. The fix is resolution at
install time, baked absolute, so every test here reads back what was actually
baked and, in the end-to-end case, spawns it exactly as a host would.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from continuum.cli import ExitCode, main
from continuum.mcp import install as mcp_install

#: The venv's script directory, the place the console script lives next to the
#: interpreter running the tests.
SCRIPTS_DIR = Path(sys.executable).parent

PROTOCOL_VERSION = "2024-11-05"
#: Twelve tools: three read-only, nine mutating (``docs/api/mcp.md``).
TOOL_COUNT = 12


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _fake_missing_mcp(tmp_path: Path) -> Path:
    """A directory whose ``mcp`` package raises on import, like a missing extra.

    PYTHONPATH entries precede site-packages, so the fresh subprocess the
    install command probes sees this shadow instead of the real SDK -- the
    state the extra being absent leaves the machine in.
    """
    package = tmp_path / "shadow" / "mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'raise ModuleNotFoundError("No module named \'mcp\'", name="mcp")\n',
        encoding="utf-8",
    )
    return package.parent


def _install(settings: Path, *extra: str) -> tuple[int, str, str]:
    return run("--json", "mcp", "install", "--settings", str(settings), *extra)


def _local_entry(settings: Path) -> dict[str, Any]:
    """The registration ``--scope local`` (the default) writes, from the file."""
    data = json.loads(settings.read_text(encoding="utf-8"))
    return data["projects"][str(Path.cwd())]["mcpServers"]["continuum-mcp"]


def test_install_bakes_the_resolved_command_and_an_absolute_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What lands in the file is absolute end to end: command and db both.

    The exact form depends on the environment (script when a console script is
    installed, module fallback otherwise), so the test pins the invariants
    that make the registration host-independent rather than one machine's
    resolution: an absolute command, an absolute ``--db``, and a file that
    matches the command the CLI reported baking.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "claude.json"

    code, out, err = _install(settings, "--db", "continuum.db")
    assert code == ExitCode.OK, err

    payload = json.loads(out)
    entry = _local_entry(settings)
    assert entry["command"] == payload["command"][0]
    assert entry["args"] == [*payload["command"][1:], "--db", payload["db"]]
    assert Path(entry["command"]).is_absolute()
    assert Path(payload["db"]).is_absolute()
    assert entry["env"] == {"CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code"}
    assert payload["status"] == "installed"
    # Editing host config must not open a run's storage as a side effect.
    assert not list(tmp_path.glob("*.db"))


def test_module_fallback_when_no_console_script_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No executable anywhere means the interpreter-plus-module form, absolute.

    This is the form that makes Windows work with zero PATH assumptions: the
    host spawns an absolute interpreter with ``-m continuum.mcp``, so neither
    ``CreateProcess``'s PATH resolution nor the spawn cwd can break it.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_install, "_sibling_script", lambda: None)
    monkeypatch.setattr(mcp_install.shutil, "which", lambda name: None)
    settings = tmp_path / "claude.json"

    code, out, err = _install(settings)
    assert code == ExitCode.OK, err

    payload = json.loads(out)
    assert payload["form"] == "module"
    entry = _local_entry(settings)
    assert entry["command"] == sys.executable
    assert entry["args"][:3] == ["-u", "-m", "continuum.mcp"]


def test_a_script_on_path_but_not_beside_the_interpreter_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no sibling script, PATH's answer is what gets baked.

    The sibling lookup misses whenever the interpreter has no console script
    beside it (a base interpreter with the package's scripts elsewhere, or a
    checkout driven through ``python -m``), and that is the case where PATH
    resolution is the thing doing the work. What it resolves is still an
    absolute command, so it is still safe to bake.
    """
    monkeypatch.chdir(tmp_path)
    on_path = tmp_path / "elsewhere" / ("continuum-mcp.exe" if os.name == "nt" else "continuum-mcp")
    on_path.parent.mkdir()
    on_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(mcp_install, "_sibling_script", lambda: None)
    monkeypatch.setattr(mcp_install.shutil, "which", lambda name: str(on_path))
    settings = tmp_path / "claude.json"

    code, out, err = _install(settings)
    assert code == ExitCode.OK, err

    payload = json.loads(out)
    assert payload["form"] == "script"
    assert payload["command"] == [str(on_path)]
    assert _local_entry(settings)["command"] == str(on_path)


def test_missing_extra_refuses_to_write_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the ``mcp`` extra absent, install names the fix and writes nothing.

    The probe is a real subprocess, so the PYTHONPATH shadow reaches it: a
    registration whose server cannot start is worse than no registration,
    because it turns a clear install problem into an opaque CONNECTION_CLOSED.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(_fake_missing_mcp(tmp_path)), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )
    settings = tmp_path / "claude.json"

    code, out, err = _install(settings)

    assert code == ExitCode.ERROR
    assert 'pip install "continuum-agent[mcp]"' in err
    assert not settings.exists(), "a refused install must not touch the config file"


def test_reinstall_is_idempotent_and_repoints_a_moved_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running install never duplicates; a moved venv is repointed.

    Idempotency is what makes ``mcp install`` something a setup script can
    run unconditionally, and repointing is what keeps a moved virtualenv from
    leaving every host spawning a path that no longer exists.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "claude.json"

    code, out, _ = _install(settings)
    assert code == ExitCode.OK
    code, out, _ = _install(settings)
    assert code == ExitCode.OK
    assert json.loads(out)["status"] == "present"
    assert list(_local_entry(settings)) is not None
    assert len(json.loads(settings.read_text())["projects"][str(Path.cwd())]["mcpServers"]) == 1

    moved = tmp_path / "venv2" / ("continuum-mcp.exe" if os.name == "nt" else "continuum-mcp")
    monkeypatch.setattr(mcp_install, "_sibling_script", lambda: moved)
    code, out, err = _install(settings)
    assert code == ExitCode.OK, err
    assert json.loads(out)["status"] == "updated"
    assert _local_entry(settings)["command"] == str(moved)


def test_remove_deletes_only_what_install_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove takes out our entry and nothing else in the file.

    The per-user settings file holds configuration for other projects and
    other servers; an uninstall that pruned those would be a one-way door.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "claude.json"
    settings.write_text(
        json.dumps(
            {
                "numStartups": 41,
                "projects": {
                    "/elsewhere": {"allowedTools": ["Bash"]},
                    str(Path.cwd()): {
                        "mcpServers": {"weather": {"command": "/usr/local/bin/weather"}}
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    code, _, err = _install(settings)
    assert code == ExitCode.OK, err

    code, out, err = run("--json", "mcp", "remove", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is True

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["numStartups"] == 41
    assert data["projects"]["/elsewhere"] == {"allowedTools": ["Bash"]}
    assert data["projects"][str(Path.cwd())]["mcpServers"] == {
        "weather": {"command": "/usr/local/bin/weather"}
    }

    # Removing twice is a quiet no-op, and the empty container is gone.
    code, out, err = run("--json", "mcp", "remove", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is False


def test_remove_prunes_the_containers_it_emptied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remove leaves the file as if the install had never happened.

    A local-scope registration sits three containers deep (``projects`` ->
    this project -> ``mcpServers``), a project-scope one two (``mcpServers``).
    When our entry is the only thing in all of them, all of them go; the
    alternative is a per-user settings file left permanently holding an empty
    project keyed by an absolute path, which is exactly the kind of residue a
    user notices and files a bug about.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "claude.json"

    code, _, err = _install(settings)
    assert code == ExitCode.OK, err
    assert "projects" in json.loads(settings.read_text(encoding="utf-8"))

    code, out, err = run("--json", "mcp", "remove", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is True
    assert settings.read_text(encoding="utf-8") == "{}\n"

    # Project scope prunes its own container the same way.
    committed = tmp_path / ".mcp.json"
    code, _, err = run("--json", "mcp", "install", "--scope", "project")
    assert code == ExitCode.OK, err
    assert "mcpServers" in json.loads(committed.read_text(encoding="utf-8"))

    code, out, err = run("--json", "mcp", "remove", "--scope", "project")
    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is True
    assert committed.read_text(encoding="utf-8") == "{}\n"


def test_remove_is_a_quiet_noop_when_the_settings_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing from a file that was never written reports nothing to fix.

    The command is documented as safe to run unconditionally from a setup
    script, so the nothing-registered state has to be a clean no-op rather
    than a "file not found" the caller has to guard for.
    """
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "claude.json"

    code, out, err = run("--json", "mcp", "remove", "--settings", str(missing))

    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is False
    assert not missing.exists()


def test_a_settings_file_the_command_cannot_read_is_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed file is reported, not recreated.

    The settings files are hand-edited and hold unrelated configuration, so
    every shape that is not the one expected raises instead of being
    replaced: silently recreating a file a typo broke would trade a one-line
    fix for a whole settings file gone. This is the same contract as the
    foreign-entry case above, one layer down -- the file itself, not an entry
    in it.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "claude.json"

    # Not JSON at all.
    settings.write_text("{not json", encoding="utf-8")
    code, _, err = _install(settings)
    assert code == ExitCode.ERROR
    assert "not valid JSON" in err
    assert settings.read_text(encoding="utf-8") == "{not json"

    # Valid JSON, but not an object.
    settings.write_text("[]\n", encoding="utf-8")
    code, _, err = _install(settings)
    assert code == ExitCode.ERROR
    assert "does not contain a JSON object" in err
    assert settings.read_text(encoding="utf-8") == "[]\n"

    # An object, but a container the registration needs is taken. Each shape is
    # built as a value and serialised, never interpolated into a JSON string:
    # a project key is an absolute path, and on Windows its backslashes would
    # make the hand-rolled string invalid JSON.
    cwd = str(Path.cwd())
    for scope, contents in (
        ("project", {"mcpServers": ["not a dict"]}),
        ("local", {"projects": "a string"}),
        ("local", {"projects": {cwd: "not a dict"}}),
        ("local", {"projects": {cwd: {"mcpServers": ["not a dict"]}}}),
    ):
        settings.write_text(json.dumps(contents), encoding="utf-8")
        code, _, err = _install(settings, "--scope", scope)
        assert code == ExitCode.ERROR, (scope, contents)
        assert "is not an object" in err, (scope, contents)


def test_remove_leaves_the_committed_registration_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed ``.mcp.json`` entry was not written by this command.

    Its path expression and cwd-relative db are not the shapes install bakes,
    so ``mcp remove`` must leave it byte-identical: unregistering it would
    unplug the server for everyone who clones the repository.
    """
    monkeypatch.chdir(tmp_path)
    committed = tmp_path / ".mcp.json"
    committed.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "continuum-mcp": {
                        "command": "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp",
                        "args": ["--db", "continuum.db"],
                        "env": {"CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code"},
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = committed.read_text(encoding="utf-8")

    code, out, err = run("--json", "mcp", "remove", "--scope", "project")

    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is False
    assert committed.read_text(encoding="utf-8") == before


def test_a_foreign_entry_under_our_name_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-registered server under ``continuum-mcp`` is intent, not clutter.

    The docs' remedy 2 tells operators to register an absolute path with a
    cwd-relative db by hand; that shape is close to ours but not ours, and
    replacing it to save a name collision would delete configuration the
    operator wrote.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / ".mcp.json"
    hand_written = {
        "type": "stdio",
        "command": "/opt/manual/continuum-mcp",
        "args": ["--db", "continuum.db"],
        "env": {"CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code"},
    }
    settings.write_text(
        json.dumps({"mcpServers": {"continuum-mcp": hand_written}}, indent=2) + "\n",
        encoding="utf-8",
    )

    code, out, err = run("mcp", "install", "--scope", "project", "--settings", str(settings))

    assert code == ExitCode.ERROR
    assert "did not write" in err
    assert json.loads(settings.read_text(encoding="utf-8"))["mcpServers"] == {
        "continuum-mcp": hand_written
    }


def test_project_scope_writes_the_shared_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--scope project`` lands in ``.mcp.json`` in the project root."""
    monkeypatch.chdir(tmp_path)

    code, out, err = run("--json", "mcp", "install", "--scope", "project")
    assert code == ExitCode.OK, err

    entry = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "continuum-mcp"
    ]
    assert Path(entry["command"]).is_absolute()
    assert entry["args"][-1] == str((tmp_path / "continuum.db").resolve())


@pytest.mark.parametrize(
    ("entry", "ours"),
    [
        # What install bakes: both forms, absolute db. The POSIX spellings are
        # only absolute on POSIX -- a drive-less path is not absolute on
        # Windows, so the predicate rightly rejects them there and the
        # expectation follows the platform the suite runs on.
        (
            {"command": "/venv/bin/continuum-mcp", "args": ["--db", "/proj/continuum.db"]},
            sys.platform != "win32",
        ),
        (
            {
                "command": "/venv/bin/python",
                "args": ["-u", "-m", "continuum.mcp", "--db", "/proj/continuum.db"],
            },
            sys.platform != "win32",
        ),
        # Windows spellings of the same two shapes; the drive-letter paths are
        # only absolute where they are real paths, so the expectation follows
        # the platform the suite runs on.
        (
            {
                "command": "C:\\venv\\Scripts\\continuum-mcp.exe",
                "args": ["--db", "C:\\proj\\continuum.db"],
            },
            sys.platform == "win32",
        ),
        (
            {
                "command": "C:\\venv\\Scripts\\python.exe",
                "args": ["-u", "-m", "continuum.mcp", "--db", "C:\\proj\\continuum.db"],
            },
            sys.platform == "win32",
        ),
        # The committed .mcp.json: path expression, cwd-relative db.
        (
            {
                "command": "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp",
                "args": ["--db", "continuum.db"],
            },
            False,
        ),
        # Remedy 2 in the docs: hand-registered absolute path, relative db.
        (
            {
                "command": "/opt/manual/continuum-mcp",
                "args": ["--db", "continuum.db"],
            },
            False,
        ),
        # Bare name (what CONNECTION_CLOSED usually means) and garbage.
        ({"command": "continuum-mcp", "args": ["--db", "/proj/continuum.db"]}, False),
        ({"command": "/venv/bin/continuum-mcp"}, False),
        # Absolute command and db, but the args are not all strings; a
        # hand-edit that drops a value into args must not become deletable.
        ({"command": "/venv/bin/continuum-mcp", "args": ["--db", None]}, False),
        # No --db at all: ours always bakes one, so this is not ours.
        ({"command": "/venv/bin/continuum-mcp", "args": []}, False),
        ("not even a dict", False),
    ],
)
def test_recognition_is_narrow(entry: Any, ours: bool) -> None:
    """Only the two shapes install bakes count as ours, on every platform.

    This predicate is what ``mcp remove`` deletes on, so its narrowness is a
    safety property, not a style choice; the parametrisation pins each edge a
    reviewer would otherwise have to take on faith.
    """
    assert mcp_install._is_managed_server(entry) is ours


def test_the_baked_registration_connects_from_a_foreign_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance case (issue #834): the entry a host reads actually serves.

    The server is spawned exactly as a host would spawn it -- the command and
    args read back out of the settings file, from a working directory that is
    not the project root, with the install environment's script directory
    stripped from PATH -- and must still complete ``initialize`` and
    ``tools/list``. That is the property registration exists to provide.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "claude.json"
    code, _, err = _install(settings, "--db", str(tmp_path / "baked.db"))
    assert code == ExitCode.OK, err
    entry = _local_entry(settings)
    command = [entry["command"], *entry["args"]]

    # Not the project root, and no install scripts reachable through PATH.
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep) if p and Path(p) != SCRIPTS_DIR
    )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=foreign,
        env=env,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None

        def request(payload: dict[str, Any]) -> dict[str, Any]:
            proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
            line = proc.stdout.readline()  # type: ignore[union-attr]
            assert line, "server closed the connection before answering"
            return json.loads(line)

        reply = request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "install-test", "version": "0"},
                },
            }
        )
        assert reply["result"]["serverInfo"]["name"] == "continuum-mcp"
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        proc.stdin.flush()
        listed = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [tool["name"] for tool in listed["result"]["tools"]]
        assert len(tools) == TOOL_COUNT, tools
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
