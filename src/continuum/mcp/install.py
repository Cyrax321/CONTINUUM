"""Cross-platform MCP host registration (issue #834).

The committed ``.mcp.json`` registers the server by a path expression that is
POSIX-only (``${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp``); on Windows
the console script lives at ``.venv\\Scripts\\continuum-mcp.exe``, so the
registered path does not exist there at all. And a committed JSON file cannot
express "``bin`` on POSIX, ``Scripts`` on Windows" in the first place: static
config has no platform conditionals, so resolution has to happen on the
machine that will spawn the server.

Worse, a bare command name does not survive either: ``CreateProcess`` (used by
Python's ``subprocess``, Node's ``spawn``, and therefore by every MCP host)
resolves a bare name against the *calling* process's PATH, not the environment
block handed to the child, so a host launched from a non-activated terminal
cannot spawn ``continuum-mcp`` even when the install is perfectly healthy.
That is the mechanism behind ``CONNECTION_CLOSED`` (see ``docs/api/mcp.md``).

``continuum mcp install`` therefore does what a committed file cannot: it
resolves the real command *now*, in this environment, and bakes absolute
values into the host's configuration --

- ``shutil.which("continuum-mcp")`` when the console script is on PATH, else
  ``[sys.executable, "-u", "-m", "continuum.mcp"]`` (the fallback is what
  makes venv and editable installs work on Windows with zero PATH
  assumptions);
- an absolute ``--db``, because every config path in the codebase is
  cwd-relative and the host's spawn cwd is neither documented nor guaranteed
  to be the project root (``.continuum/mcp-policy.json`` also resolves
  against the cwd, so a relative db would silently change the security
  posture too).

Before any of that, the ``mcp`` extra is verified by *spawning* a probe
subprocess rather than importing in-process: a running interpreter has a
mutable ``sys.path`` (pytest inserts ``src/``, shells export ``PYTHONPATH``),
so an in-process check can pass in exactly the states where the baked command
would be dead for the host.

This module is pure standard library, like the CLI it serves: it must work in
every state it is asked to fix.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "HOST_PROFILES",
    "INSTALL_COMMAND",
    "SERVER_NAME",
    "install_server",
    "remove_server",
    "resolve_command",
    "verify_sdk",
]

#: The server name every host registration uses. Also the console script name.
SERVER_NAME = "continuum-mcp"

#: The interpreter-plus-module fallback, baked when no executable is on PATH.
#: ``-u`` keeps stdio unbuffered, which is what a line-delimited protocol wants.
MODULE_ARGS = ("-u", "-m", "continuum.mcp")

#: The remedy for a missing extra, spelled the way ``docs/api/mcp.md`` spells
#: it (quoted: the bracket is a glob in zsh, issue #836).
INSTALL_COMMAND = 'pip install "continuum-agent[mcp]"'

#: How long the SDK probe may run before the install gives up on it.
PROBE_TIMEOUT_SECONDS = 30.0

#: Per-host wiring profiles, structured the way ``CLIENT_PROFILES`` is
#: (``src/continuum/clienthooks.py``): everything that differs between hosts
#: is data, not code. ``project_settings`` is the committed, per-project file
#: (``--scope project``); ``local_settings`` is the per-user file that holds
#: local-scope registrations under ``projects/<absolute project path>``
#: (``--scope local``, the default). ``mutating_clients`` is the client name
#: the registration allows to call mutating tools. Adding cursor or windsurf
#: later is one dict entry, not a redesign.
HOST_PROFILES: dict[str, dict[str, str]] = {
    "claude-code": {
        "project_settings": ".mcp.json",
        "local_settings": "~/.claude.json",
        "local_projects_key": "projects",
        "mutating_clients": "claude-code",
    },
}


def _sibling_script() -> Path | None:
    """The console script installed beside this interpreter, if there is one.

    Not ``.resolve()``-ed: on macOS a venv's python is a symlink to the
    framework build, and resolving it would point this lookup at the framework
    interpreter's bin directory instead of the venv's.
    """
    name = "continuum-mcp.exe" if os.name == "nt" else SERVER_NAME
    sibling = Path(sys.executable).parent / name
    return sibling if sibling.exists() else None


def resolve_command() -> tuple[list[str], str]:
    """The command to bake, resolved now on the machine that will spawn it.

    Returns ``(argv, form)`` where ``form`` is ``"script"`` (the console
    script, resolved to an absolute path) or ``"module"`` (the
    interpreter-plus-module fallback, used when no executable can be found).
    Both forms are absolute, which is the whole point: the host's PATH and
    spawn cwd then stop mattering.

    The console script is looked up next to *this* interpreter before PATH,
    because PATH can hold a ``continuum-mcp`` from a different environment
    than the one this command runs in (observed live: a venv interpreter with
    the framework install's script first on PATH), and the registration should
    point at the environment the operator chose, which is also the one
    :func:`verify_sdk` probes.
    """
    sibling = _sibling_script()
    if sibling is not None:
        return [str(sibling)], "script"
    executable = shutil.which(SERVER_NAME)
    if executable:
        return [executable], "script"
    return [sys.executable, *MODULE_ARGS], "module"


def verify_sdk(form: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Spawn a fresh interpreter that imports what the baked command needs.

    Returns ``(ok, detail)``; ``detail`` is the last line of the probe's
    stderr, because tracebacks bury the useful part. The probe is a real
    subprocess on purpose: the current process has had its ``sys.path``
    mutated by whoever launched it (pytest, an IDE, a shell's PYTHONPATH), and
    the host will spawn the baked command with none of that. What the probe
    imports follows the form being baked -- the module fallback runs
    ``continuum.mcp`` under this interpreter, so both halves have to import
    fresh, while the script form only has to prove the SDK exists.
    """
    code = "import mcp" if form == "script" else "import mcp, continuum.mcp"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    if proc.returncode == 0:
        return True, ""
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    return False, lines[-1] if lines else "(no output)"


def install_server(
    settings_path: Path,
    *,
    scope: str,
    project_root: Path,
    command: list[str],
    db: Path,
    host: str = "claude-code",
) -> str:
    """Upsert the registration for this project. Returns the status word.

    ``"installed"`` when the entry was added, ``"updated"`` when an entry this
    module wrote pointed somewhere else (a moved virtualenv, say) and was
    repointed, ``"present"`` when nothing needed to change. A file that exists
    but is not a JSON object raises rather than being overwritten, and an
    entry under :data:`SERVER_NAME` that this module did not write raises
    too: a hand-registered server is a statement of intent, and replacing it
    to save a name collision would delete configuration the operator wrote.
    """
    data = _load_object(settings_path)
    servers = _mcp_servers(data, scope=scope, project_root=project_root)
    entry = _server_entry(command, db, host=host)

    existing = servers.get(SERVER_NAME)
    if existing is None:
        servers[SERVER_NAME] = entry
        status = "installed"
    elif _is_managed_server(existing):
        if existing == entry:
            status = "present"
        else:
            servers[SERVER_NAME] = entry
            status = "updated"
    else:
        raise ValueError(
            f"{settings_path} already registers {SERVER_NAME!r} with an entry "
            "continuum mcp install did not write; remove it by hand first, or "
            "register under a different --scope"
        )

    _save(settings_path, data)
    return status


def remove_server(
    settings_path: Path,
    *,
    scope: str,
    project_root: Path,
) -> bool:
    """Remove the registration this command wrote. True when anything went.

    Only an entry :func:`_is_managed_server` recognises is touched: the
    committed ``.mcp.json`` entry (path expression, relative db) and any
    hand-registered server survive untouched, as does every other key in the
    file. Empty containers the entry occupied are pruned so a remove leaves
    the file as if the install had never happened.
    """
    if not settings_path.exists():
        return False
    data = _load_object(settings_path)
    if scope == "project":
        removed = _remove_project_entry(data)
    elif scope == "local":
        removed = _remove_local_entry(data, project_root)
    else:
        raise ValueError(f"unknown scope {scope!r} (expected 'local' or 'project')")
    if not removed:
        return False
    _save(settings_path, data)
    return True


def _remove_project_entry(data: dict[str, Any]) -> bool:
    """Take our entry out of a project-scope file's ``mcpServers``."""
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    if not _drop_managed(servers):
        return False
    if not servers:
        del data["mcpServers"]
    return True


def _remove_local_entry(data: dict[str, Any], project_root: Path) -> bool:
    """Take our entry out of this project's local-scope registration."""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    project_entry = projects.get(str(project_root))
    if not isinstance(project_entry, dict):
        return False
    servers = project_entry.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    if not _drop_managed(servers):
        return False
    if not servers:
        del project_entry["mcpServers"]
        if not project_entry:
            del projects[str(project_root)]
        if not projects:
            del data["projects"]
    return True


def _drop_managed(servers: dict[str, Any]) -> bool:
    """Delete our entry from one ``mcpServers`` dict. True when it went."""
    existing = servers.get(SERVER_NAME)
    if existing is None or not _is_managed_server(existing):
        return False
    del servers[SERVER_NAME]
    return True


# --------------------------------------------------------------------------- #
# the entry and its recognition
# --------------------------------------------------------------------------- #


def _server_entry(command: list[str], db: Path, *, host: str) -> dict[str, Any]:
    """The registration to bake: absolute command, absolute db, env.

    The env names the host's own client in the mutating-tools allowlist:
    without it the server connects but exposes only the read-only tools
    (``docs/api/mcp.md``), which is a working registration that looks broken
    the first time the agent tries to record anything.
    """
    argv = [*command, "--db", str(db)]
    return {
        "type": "stdio",
        "command": argv[0],
        "args": argv[1:],
        "env": {"CONTINUUM_MCP_MUTATING_CLIENTS": HOST_PROFILES[host]["mutating_clients"]},
    }


def _is_managed_server(entry: Any) -> bool:
    """True when an ``mcpServers`` entry is one this module wrote.

    Narrow on purpose, the same discipline as ``clienthooks._is_managed_hook``:
    ``mcp remove`` must never delete a registration this command did not
    write. Two shapes are recognised, matching :func:`_server_entry` exactly:
    a resolved console script (absolute path whose stem is ``continuum-mcp``)
    and the interpreter fallback (args beginning ``-u -m continuum.mcp``).
    Both must also carry the absolute ``--db`` this module bakes, which is
    what separates our entry from the committed ``.mcp.json`` one (path
    expression, cwd-relative db) and from a hand-registered absolute path
    (``docs/api/mcp.md`` remedy 2 bakes a relative db).
    """
    if not isinstance(entry, dict):
        return False
    command, args = entry.get("command"), entry.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    if not all(isinstance(arg, str) for arg in args):
        return False
    if not _bakes_absolute_db(args):
        return False
    path = Path(command)
    if not path.is_absolute():
        return False
    if path.stem == SERVER_NAME:
        return True
    return args[: len(MODULE_ARGS)] == list(MODULE_ARGS)


def _bakes_absolute_db(args: list[str]) -> bool:
    """True when ``args`` carries ``--db`` with an absolute path."""
    for index, arg in enumerate(args):
        if arg == "--db" and index + 1 < len(args):
            return Path(args[index + 1]).is_absolute()
    return False


# --------------------------------------------------------------------------- #
# settings-file plumbing
# --------------------------------------------------------------------------- #


def _mcp_servers(
    data: dict[str, Any],
    *,
    scope: str,
    project_root: Path,
) -> dict[str, Any]:
    """The ``mcpServers`` dict a registration is written into, for one scope.

    Project scope is the file's top level. Local scope nests under
    ``projects/<absolute project path>``, which is how the host's own
    ``--scope local`` writes (``docs/api/mcp.md``): local beats project, so a
    registration there wins over the committed ``.mcp.json`` without touching
    it. Missing containers are created; a container that exists but holds
    something else raises rather than being replaced, because whatever is in
    there was written on purpose.
    """
    if scope == "project":
        servers = data.get("mcpServers")
        if servers is None:
            servers = data["mcpServers"] = {}
        if not isinstance(servers, dict):
            raise ValueError("the settings file's 'mcpServers' is not an object")
        return servers

    if scope != "local":
        raise ValueError(f"unknown scope {scope!r} (expected 'local' or 'project')")

    projects = data.get("projects")
    if projects is None:
        projects = data["projects"] = {}
    if not isinstance(projects, dict):
        raise ValueError("the settings file's 'projects' is not an object")
    key = str(project_root)
    project_entry = projects.get(key)
    if project_entry is None:
        project_entry = projects[key] = {}
    if not isinstance(project_entry, dict):
        raise ValueError(f"the settings file's projects[{key!r}] is not an object")
    servers = project_entry.get("mcpServers")
    if servers is None:
        servers = project_entry["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise ValueError(f"projects[{key!r}].mcpServers is not an object")
    return servers


def _load_object(path: Path) -> dict[str, Any]:
    """Read a settings file as a JSON object, or ``{}`` when it is absent.

    A file that exists but does not parse raises rather than being replaced:
    a hand-edited file is a statement of intent, and silently recreating it
    would destroy work to save a typo (the same contract as
    ``clienthooks._install_hook``).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); refusing to edit it") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    """Write the settings back, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def display_command(command: list[str]) -> str:
    """Quote an argv for display, the way the host's shell would need it.

    POSIX shells read shlex's single quotes; ``cmd.exe`` wants the Windows C
    runtime's convention, which is what ``list2cmdline`` emits. Paths with
    spaces are routine on Windows, so the choice is not cosmetic.
    """
    if sys.platform == "win32":
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(part) for part in command)
