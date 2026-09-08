"""Register the MCP server with a host so the host can actually spawn it (issue #834).

``.mcp.json`` declares the server by bare command name, and that name resolves only
when the *host's* PATH contains the environment CONTINUUM was installed into. On
Windows this fails by construction: ``CreateProcess`` resolves a bare command name
against the calling process's own PATH, never the environment it passes to the child,
and a venv's ``Scripts`` directory joins PATH only inside a shell that activated it.
The host reports the failed spawn as ``CONNECTION_CLOSED``, which reads like a crash
but describes an executable that was never found (#699). A committed JSON file cannot
name both ``.venv/bin/continuum-mcp`` and ``.venv\\Scripts\\continuum-mcp.exe``, so no
static config can fix this for every machine at once.

The fix is to resolve the command *on the machine that will spawn the server*, which
is what this module does: ``continuum mcp install`` resolves the real entry point
(the ``continuum-mcp`` executable, or ``<python> -m continuum.mcp`` when no executable
is on PATH — the same two-step resolution ``hooks install`` uses for its commands,
``clienthooks.observe_command``), proves it serves the protocol by driving a real
``initialize`` handshake over stdio, and only then writes the registration with the
resolved absolute path baked in.

The handshake probe is what separates this from a PATH lookup: it catches the
missing-``mcp``-extra state (#697, where the entry point exists but its dependency
does not), a stale entry point from a moved virtualenv, and a server that starts but
cannot open storage — all before any configuration is written, so a failed install
leaves the host's config untouched rather than registering a server known not to
work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "MCP_PROFILES",
    "ProbeError",
    "SERVER_KEY",
    "build_registration",
    "install_server",
    "probe_server",
    "remove_server",
    "resolve_server_command",
]

#: The server key every host config uses. MCP servers are registered by name, so
#: unlike hooks (whose entries are anonymous and recognised by command shape,
#: ``clienthooks._is_managed_hook``) the key *is* the identity: whatever wrote the
#: entry under this name registered the CONTINUUM server.
SERVER_KEY = "continuum-mcp"

#: Which client name the authz layer trusts with mutating tools. The committed
#: ``.mcp.json`` grants ``claude-code``; the installer keeps that default and keys
#: it to the profile so a future host profile grants its own client name.
_MUTATING_CLIENTS_ENV = "CONTINUUM_MCP_MUTATING_CLIENTS"

#: Per-host wiring profiles, in the spirit of ``clienthooks.CLIENT_PROFILES``
#: (#209): everything that differs between hosts is data. A host's config file,
#: where its scopes live, and whether its "local" scope is per-project (Claude Code
#: nests local registrations under ``projects[<cwd>].mcpServers`` in the user config)
#: is all a host needs to describe here for install/remove to work.
MCP_PROFILES: dict[str, dict[str, Any]] = {
    "claude-code": {
        # local and user scopes share ~/.claude.json; project scope is the
        # committed .mcp.json at the project root.
        "user_config": "~/.claude.json",
        "project_config": ".mcp.json",
        # Claude Code's local scope is stored per project directory inside the
        # user config (verified against `claude mcp add`: projects[cwd].mcpServers,
        # with the directory spelled in forward slashes even on Windows).
        "local_is_per_project": True,
        "mutating_client": "claude-code",
    },
}


class ProbeError(Exception):
    """The server could not be started or did not complete the MCP handshake."""


def resolve_server_command() -> list[str]:
    """The argv that runs the MCP server on this machine, right now.

    Three steps, most desirable first: an executable on PATH (what a wheel, pipx
    or ``uv tool`` install provides); an executable sitting beside the running
    ``continuum`` (the common venv case where ``continuum mcp install`` was invoked
    from inside the environment but PATH resolution is unavailable to the host);
    and the interpreter-plus-module form, which needs no PATH at all and is what
    makes editable and unactivated-venv installs work on Windows. The forms match
    ``clienthooks.observe_command``'s resolution so the two installers cannot
    disagree about how to reach a CONTINUUM entry point.
    """
    direct = shutil.which(SERVER_KEY)
    if direct:
        return [direct]
    sibling_name = f"{SERVER_KEY}.exe" if os.name == "nt" else SERVER_KEY
    running = shutil.which("continuum")
    if running:
        sibling = Path(running).parent / sibling_name
        if sibling.exists():
            return [str(sibling)]
    return [sys.executable, "-u", "-m", "continuum.mcp"]


def probe_server(command: Sequence[str], *, timeout: float = 20.0) -> dict[str, Any]:
    """Prove ``command`` serves MCP by driving a real ``initialize`` handshake.

    Raises :class:`ProbeError` — never writes anything, spawns nothing else — so
    the caller can refuse to register a server that does not work. The probe's
    database is a throwaway in the system temporary directory: verifying a
    registration must not leave ``continuum.db`` files in the project, whose
    presence the CLI treats as state.

    The environment is inherited rather than minimal: on Windows a child denied
    ``SystemRoot`` dies during interpreter startup before any CONTINUUM code runs
    (issue #211).
    """
    tmp = Path(tempfile.mkdtemp(prefix="continuum-mcp-probe-"))
    env = dict(os.environ)
    env["CONTINUUM_DB"] = str(tmp / "probe.db")
    errors: list[str] = []
    stderr_thread: threading.Thread | None = None
    try:
        try:
            proc = subprocess.Popen(  # noqa: SIM115 - cleaned up in finally
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ProbeError(
                f"cannot start the MCP server ({exc}). The registered command must "
                f"be spawnable by the host; check the installation."
            ) from exc

        # Drain stderr on a thread so a chatty server cannot deadlock the probe by
        # filling the pipe buffer while we block reading stdout.
        def drain_stderr() -> None:
            assert proc.stderr is not None
            errors.extend(line.rstrip() for line in proc.stderr)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        try:
            assert proc.stdin is not None
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "continuum-mcp-install", "version": "0"},
                        },
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            line = _readline_with_timeout(proc, timeout)
            reply: dict[str, Any] = json.loads(line)
            server: dict[str, Any] = reply.get("result", {}).get("serverInfo", {})
            if server.get("name") != SERVER_KEY:
                raise ProbeError(
                    f"the command answered the handshake as a different server "
                    f"({server.get('name')!r})"
                )
            return server
        except json.JSONDecodeError as exc:
            raise ProbeError(
                f"the server's handshake reply is not JSON ({exc}); it printed to "
                f"stdout, which the MCP protocol reserves for protocol frames"
            ) from exc
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except ProbeError as exc:
        # The server's own stderr is often the actual diagnosis (the missing-extra
        # message, a storage error), and the host never shows it, so the probe
        # attaches it rather than summarising it away. The drain thread needs a
        # moment past process exit to consume the pipe's tail; join briefly
        # instead of racing it.
        if stderr_thread is not None:
            stderr_thread.join(1.0)
        tail = "\n".join(errors[-5:]).strip()
        if tail:
            raise ProbeError(f"{exc}\nThe server reported:\n{tail}") from None
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _readline_with_timeout(proc: subprocess.Popen[str], timeout: float) -> str:
    """Read one stdout line, bounded in time.

    ``select`` cannot wait on Windows pipes, so the read runs on a thread and the
    join is the deadline. An empty line means the server exited before answering,
    which is where every cold-start failure (#87, #697) shows up.
    """
    assert proc.stdout is not None
    result: list[str] = []

    def read() -> None:
        result.append(proc.stdout.readline())  # type: ignore[union-attr]

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        proc.kill()
        raise ProbeError(
            f"the MCP server did not complete the initialize handshake within {timeout:.0f}s"
        )
    line = result[0]
    if not line:
        raise ProbeError("the MCP server exited before answering the handshake")
    return line


def build_registration(
    command: Sequence[str], *, db: str | None = None, client: str = "claude-code"
) -> dict[str, Any]:
    """The ``mcpServers`` entry for ``command``.

    ``db`` defaults to the same ``continuum.db`` the committed ``.mcp.json`` uses:
    relative, resolved by the host against the project it spawns the server in.
    That is parity with the shipped configuration, not an oversight — a
    registration's command path is per-machine (resolved here, baked in), but the
    database belongs to the project, and an absolute default would point every
    project at one database. ``--db`` bakes an explicit path for operators who
    want one.
    """
    db_path = db or "continuum.db"
    if len(command) == 1:
        resolved_command: str = command[0]
        args: list[str] = ["--db", db_path]
    else:
        resolved_command = command[0]
        args = [*command[1:], "--db", db_path]
    return {
        "type": "stdio",
        "command": resolved_command,
        "args": args,
        "env": {_MUTATING_CLIENTS_ENV: client},
    }


def _load_config(config_path: Path) -> dict[str, Any]:
    """Read a host config file, refusing to clobber one that is not JSON.

    A file someone edited by hand is a statement of intent; overwriting it to save
    a typo would destroy work (same contract as ``clienthooks._install_hook``).
    """
    if config_path.exists():
        try:
            settings: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{config_path} is not valid JSON ({exc}); refusing to edit it"
            ) from exc
        if not isinstance(settings, dict):
            raise ValueError(f"{config_path} does not contain a JSON object")
        return settings
    return {}


def _mcp_servers_for(
    settings: dict[str, Any], *, scope: str, project_dir: Path, profile: Mapping[str, Any]
) -> dict[str, Any]:
    """Navigate to the ``mcpServers`` map a given scope lives in, creating the path.

    Claude Code keeps its per-project "local" scope inside the user config under
    ``projects[<cwd>].mcpServers`` — with the directory spelled in forward slashes
    even on Windows, which is how ``claude mcp add`` itself writes it, so this
    reproduces the host's own layout rather than inventing a parallel one. The
    "user" scope is the same file's top-level ``mcpServers``; the "project" scope
    is a different file entirely (``.mcp.json``, chosen by the caller), whose
    ``mcpServers`` is also top-level.
    """
    if scope == "local" and profile["local_is_per_project"]:
        projects = settings.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError("'projects' is not an object")
        # as_posix: forward slashes on every platform, matching what the host
        # itself writes as the project key.
        project_key = project_dir.resolve().as_posix()
        project = projects.setdefault(project_key, {})
        if not isinstance(project, dict):
            raise ValueError(f"projects[{project_key}] is not an object")
    elif scope in ("local", "user", "project"):
        project = settings
    else:
        raise ValueError(f"unknown scope {scope!r}")
    servers = project.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("'mcpServers' is not an object")
    return servers


def install_server(
    config_path: Path,
    registration: Mapping[str, Any],
    *,
    scope: str,
    project_dir: Path,
    profile: Mapping[str, Any] | None = None,
) -> str:
    """Write ``registration`` under :data:`SERVER_KEY`; returns the change made.

    ``"installed"`` when the key was absent, ``"updated"`` when an existing entry
    pointed somewhere else (a moved virtualenv — repointed, not duplicated), and
    ``"present"`` when nothing needed to change, so re-running after an upgrade
    converges instead of accumulating entries.
    """
    profile = profile or MCP_PROFILES["claude-code"]
    settings = _load_config(config_path)
    servers = _mcp_servers_for(settings, scope=scope, project_dir=project_dir, profile=profile)
    existing = servers.get(SERVER_KEY)
    if existing == dict(registration):
        return "present"
    servers[SERVER_KEY] = dict(registration)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return "updated" if existing is not None else "installed"


def remove_server(
    config_path: Path, *, scope: str, project_dir: Path, profile: Mapping[str, Any] | None = None
) -> bool:
    """Drop the :data:`SERVER_KEY` registration. True when anything was removed.

    Only our key is touched: every other server, every other key in the file, and
    the project entry itself survive, exactly as ``clienthooks._remove_hooks``
    leaves unrelated hook configuration alone. Empty ``mcpServers`` maps are left
    in place because the host itself does (verified: ``claude mcp remove`` leaves
    ``"mcpServers": {}`` behind), so removing cannot make the file a shape the
    host has never seen.
    """
    profile = profile or MCP_PROFILES["claude-code"]
    settings = _load_config(config_path)
    if not settings:
        return False
    try:
        servers = _mcp_servers_for(settings, scope=scope, project_dir=project_dir, profile=profile)
    except ValueError:
        return False
    if SERVER_KEY not in servers:
        return False
    del servers[SERVER_KEY]
    config_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True
