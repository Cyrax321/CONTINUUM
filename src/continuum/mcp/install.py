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

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from continuum.mcp import configfmt

__all__ = [
    "HOST_PROFILES",
    "INSTALL_COMMAND",
    "SERVER_NAME",
    "host_format",
    "host_shape",
    "install_server",
    "remove_server",
    "resolve_command",
    "server_spec",
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
#: the registration allows to call mutating tools.
#:
#: The rest is how the host *spells* a registration, and it falls into two
#: independent groups. ``format`` is the only one that can need a third-party
#: parser. ``container``, ``servers_key``, ``nested_by_project``, ``argv_style``
#: and ``env_key`` say where the entry sits in the document and what its keys
#: are called, and they are plain structure that :mod:`continuum.mcp.configfmt`
#: handles with no dependency at all.
#:
#: Adding a host that is JSON and dict-keyed and spells the command as
#: ``command`` plus ``args`` really is one row. Adding one that is TOML or
#: YAML, list-shaped, or argv-array-shaped is also one row, but it costs a
#: config parser at install time (see ``configfmt``), and no amount of data
#: removes that cost. That caveat used to be hidden behind a docstring
#: claiming otherwise; it is stated here instead.
#:
#: ``local_projects_key`` predates the shape keys and is not read by anything.
#: It is kept so its meaning, which is none, does not change under a caller
#: that reads it. Per-project nesting is ``nested_by_project``.
HOST_PROFILES: dict[str, dict[str, str]] = {
    "claude-code": {
        "project_settings": ".mcp.json",
        "local_settings": "~/.claude.json",
        "user_settings": "~/.claude.json",
        "local_projects_key": "projects",
        "mutating_clients": "claude-code",
        "format": "json",
        "container": "dict",
        "servers_key": "mcpServers",
        "nested_by_project": "projects",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "type",
        "type_value": "stdio",
    },
    "gemini": {
        "project_settings": ".mcp.json",
        "local_settings": "~/.gemini/settings.json",
        "user_settings": "~/.gemini/settings.json",
        "local_projects_key": "mcpServers",
        "mutating_clients": "gemini-cli",
        "format": "json",
        "container": "dict",
        "servers_key": "mcpServers",
        "nested_by_project": "",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "type",
        "type_value": "stdio",
    },
    "cursor": {
        "project_settings": ".cursor/mcp.json",
        "local_settings": "~/.cursor/mcp.json",
        "user_settings": "~/.cursor/mcp.json",
        "local_projects_key": "mcpServers",
        "mutating_clients": "cursor",
        "format": "json",
        "container": "dict",
        "servers_key": "mcpServers",
        "nested_by_project": "",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "type",
        "type_value": "stdio",
    },
    "vscode": {
        "project_settings": ".vscode/mcp.json",
        "local_settings": "~/Library/Application Support/Code/User/settings.json",
        "user_settings": "~/Library/Application Support/Code/User/settings.json",
        "local_projects_key": "mcpServers",
        "mutating_clients": "vscode",
        "format": "json",
        "container": "dict",
        "servers_key": "mcpServers",
        "nested_by_project": "",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "type",
        "type_value": "stdio",
    },
    # --- verified against vendor documentation, sources cited per host below ---
    #
    # Codex CLI: TOML, so writing one needs a config parser at install time.
    # Shape and path from the configuration reference:
    # https://developers.openai.com/codex/config-reference
    # ("User-level configuration lives in ~/.codex/config.toml . You can also
    # add project-scoped overrides in .codex/config.toml files."), and the
    # mcp_servers.<id>.command / .args / .env entries on the same page.
    #
    # MUTATING_CLIENTS IS UNVERIFIED. The reference documents no clientInfo
    # name and the handshake site could not be read, so "codex-cli" is this
    # project's best guess and not a citation. If it is wrong the server
    # still connects and serves only the three read-only tools. That is the
    # failure `continuum mcp doctor` reports, which is why the guess is
    # recorded here rather than buried.
    "codex": {
        "project_settings": ".codex/config.toml",
        "local_settings": "~/.codex/config.toml",
        "user_settings": "~/.codex/config.toml",
        "local_projects_key": "",
        "mutating_clients": "codex-cli",
        "format": "toml",
        "container": "dict",
        "servers_key": "mcp_servers",
        "nested_by_project": "",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "",
        "type_value": "",
    },
    # opencode: one argv array under "command", and "environment" rather than
    # "env". Shape and paths from https://opencode.ai/docs/mcp-servers/ ("You
    # can define MCP servers in your OpenCode Config under mcp", the local
    # example showing "command": ["bun", "x", "my-mcp-command"] and
    # "environment"), and the schema at
    # https://github.com/sst/opencode/blob/dev/packages/core/src/config/mcp.ts
    # (ConfigV2.MCP.Local: type "local", command: String.pipe(Schema.Array),
    # environment). The user's file is
    # ~/.config/opencode/opencode.json, the project's is opencode.json in the
    # workspace root.
    #
    # MUTATING_CLIENTS IS VERIFIED. https://github.com/sst/opencode/blob/dev/
    # packages/opencode/src/mcp/index.ts constructs
    # `new Client({ name: "opencode", version: InstallationVersion })`, which
    # is the clientInfo.name the server reads.
    "opencode": {
        "project_settings": "opencode.json",
        "local_settings": "~/.config/opencode/opencode.json",
        "user_settings": "~/.config/opencode/opencode.json",
        "local_projects_key": "",
        "mutating_clients": "opencode",
        "format": "json",
        "container": "dict",
        "servers_key": "mcp",
        "nested_by_project": "",
        "argv_style": "array",
        "env_key": "environment",
        "type_key": "type",
        "type_value": "local",
    },
    # Continue: a list, not a dict, and each record carries its own name.
    # Shape and paths from https://github.com/continuedev/continue/blob/main/
    # docs/customize/deep-dives/mcp.mdx, which shows `mcpServers:` followed by
    # `- name: SQLite MCP / command: npx / args:` in config.yaml, and
    # documents the `name`, `type`, `command`, `args` and `env` properties
    # (with type "stdio" for a local server). The user's file is
    # ~/.continue/config.yaml and the project's is .continue/config.yaml.
    #
    # MUTATING_CLIENTS IS UNVERIFIED. Continue is a VS Code extension whose
    # MCP client name is not documented; "continue" is this project's best
    # guess and not a citation. See the codex note above for what a wrong
    # value costs.
    "continue": {
        "project_settings": ".continue/config.yaml",
        "local_settings": "~/.continue/config.yaml",
        "user_settings": "~/.continue/config.yaml",
        "local_projects_key": "",
        "mutating_clients": "continue",
        "format": "yaml",
        "container": "list",
        "servers_key": "mcpServers",
        "nested_by_project": "",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "type",
        "type_value": "stdio",
        "name_field": "name",
    },
    # Zed calls them context servers, not MCP servers, and puts them in its
    # own settings file rather than in anything MCP-shaped. Shape from
    # https://zed.dev/docs/ai/mcp, which shows the settings file as
    # `{"context_servers": {"local-mcp-server": {"command": "...",
    # "args": [...], "env": {}}}}`; the key is at the top level because
    # SettingsContent flattens ProjectSettingsContent
    # (https://github.com/zed-industries/zed/blob/main/crates/settings_content
    # /src/settings_content.rs) and ProjectSettingsContent declares
    # `context_servers: HashMap<...>` (crates/settings_content/src/project.rs).
    # The user's file is ~/.config/zed/settings.json and the project's is
    # .zed/settings.json.
    #
    # MUTATING_CLIENTS IS VERIFIED, and not in the way it looks:
    # https://github.com/zed-industries/zed/blob/main/crates/context_server/
    # src/context_server.rs sends `name: "Zed".to_string()`, capital Z. This is
    # precisely the mismatch that silently costs a host its mutating tools,
    # and why it is worth citing rather than inferring from the product name.
    "zed": {
        "project_settings": ".zed/settings.json",
        "local_settings": "~/.config/zed/settings.json",
        "user_settings": "~/.config/zed/settings.json",
        "local_projects_key": "",
        "mutating_clients": "Zed",
        "format": "json",
        "container": "dict",
        "servers_key": "context_servers",
        "nested_by_project": "",
        "argv_style": "split",
        "env_key": "env",
        "type_key": "",
        "type_value": "",
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


def host_format(host: str) -> str:
    """The config format ``host``'s settings file is written in.

    One of :data:`configfmt.FORMATS`. JSON covers every host that shipped
    before this module did; TOML and YAML are the two formats that cannot be
    reached from the standard library alone, and both refuse loudly rather
    than guessing when their parser is absent.
    """
    return HOST_PROFILES[host]["format"]


def host_shape(host: str) -> configfmt.Shape:
    """The structure ``host``'s settings file files its servers in.

    Built from the profile's flat string keys rather than stored as a nested
    object, so a profile stays a flat ``dict[str, dict[str, str]]`` and
    nothing a caller reads off it changes type.
    """
    profile = HOST_PROFILES[host]
    return configfmt.Shape(
        servers_key=profile["servers_key"],
        container=profile["container"],
        nesting_key=profile["nested_by_project"],
        name_field=profile.get("name_field", "name"),
        argv_style=profile["argv_style"],
        env_key=profile["env_key"],
        type_key=profile.get("type_key", ""),
        type_value=profile.get("type_value", ""),
    )


def server_spec(command: list[str], db: Path, host: str) -> configfmt.ServerSpec:
    """The registration to write, stated format-free.

    ``CONTINUUM_MCP_MUTATING_CLIENTS`` names the client the server will read
    out of the ``initialize`` handshake. Without it the server connects and
    exposes only the three read-only tools, which is a working registration
    that looks broken the first time the agent tries to record anything.
    """
    return configfmt.ServerSpec(
        name=SERVER_NAME,
        argv=(*command, "--db", str(db)),
        env={"CONTINUUM_MCP_MUTATING_CLIENTS": HOST_PROFILES[host]["mutating_clients"]},
    )


def _server_entry(command: list[str], db: Path, *, host: str) -> dict[str, Any]:
    """The registration to bake, spelled the way ``host`` spells one.

    Absolute command, absolute db, and the host's own env key. The spelling
    is the host's, not ours: the same three facts are a ``command``/``args``
    pair for most hosts and one argv array for opencode.
    """
    return host_shape(host).entry(server_spec(command, db, host))


def _is_managed_argv(argv: list[str]) -> bool:
    """Whether ``argv`` is one of the two shapes install bakes.

    Two shapes are recognised, and both must carry the absolute ``--db``
    this module bakes: a resolved console script (absolute path whose stem is
    ``continuum-mcp``) and the interpreter fallback (argv beginning
    ``-u -m continuum.mcp``). The absolute db is what separates ours from the
    committed ``.mcp.json`` entry (path expression, cwd-relative db) and from
    a hand-registered absolute path (``docs/api/mcp.md`` remedy 2 bakes a
    relative db).
    """
    if not argv:
        return False
    if not _bakes_absolute_db(argv):
        return False
    launcher = Path(argv[0])
    if launcher.is_absolute() and launcher.stem == SERVER_NAME:
        return True
    return argv[1 : 1 + len(MODULE_ARGS)] == list(MODULE_ARGS)


def _bakes_absolute_db(argv: list[str]) -> bool:
    """True when ``argv`` carries ``--db`` with an absolute path."""
    for index, arg in enumerate(argv):
        if arg == "--db" and index + 1 < len(argv):
            return Path(argv[index + 1]).is_absolute()
    return False


def _is_managed_server(entry: Any, host: str = "claude-code") -> bool:
    """True when ``entry`` is one this module wrote, read in ``host``'s shape.

    Narrow on purpose, the same discipline as
    ``clienthooks._is_managed_hook``: ``mcp remove`` must never delete a
    registration this command did not write. The entry is first normalised to
    argv by the host's shape, so an entry spelled in some other host's shape
    is not even a candidate.
    """
    argv = host_shape(host).argv_of(entry)
    return argv is not None and _is_managed_argv(argv)


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
    repointed, ``"present"`` when nothing needed to change. A file that
    exists but is not a document this module can read raises, as does an
    entry under :data:`SERVER_NAME` that this module did not write: a
    hand-registered server is a statement of intent, and replacing it to save
    a name collision would delete configuration the operator wrote.
    """
    fmt = host_format(host)
    shape = host_shape(host)
    data = configfmt.read_document(fmt, settings_path)
    status, previous = shape.put(data, scope, project_root, server_spec(command, db, host))
    if status != "installed" and previous is not None and not _is_managed_server(previous, host):
        raise ValueError(
            f"{settings_path} already registers {SERVER_NAME!r} with an entry "
            "continuum mcp install did not write; remove it by hand first, or "
            "register under a different --scope"
        )
    configfmt.write_document(fmt, settings_path, data)
    return status


def remove_server(
    settings_path: Path,
    *,
    scope: str,
    project_root: Path,
    host: str | None = None,
) -> bool:
    """Remove the registration this command wrote. True when anything went.

    Only an entry :func:`_is_managed_server` recognises is touched: the
    committed ``.mcp.json`` entry (path expression, relative db) and any
    hand-registered server survive untouched, as does every other key in the
    file. Empty containers the entry occupied are pruned so a remove leaves
    the file as if the install had never happened.

    ``host`` is optional because the command that calls this resolves the
    settings path from a profile but does not pass the host along. It is
    recovered from the path itself, which is exact for every invocation that
    uses the host's own file. A ``--settings`` override pointing somewhere no
    profile names falls back to the file's extension for the format and to
    the canonical shape for the container, and therefore to a no-op rather
    than to a guess about which structure to delete from.
    """
    if not settings_path.exists():
        return False
    resolved = host or _infer_host(settings_path, scope)
    fmt = host_format(resolved)
    shape = host_shape(resolved)
    data = configfmt.read_document(fmt, settings_path)
    entry = shape.find(data, scope, project_root, SERVER_NAME)
    if entry is None or not _is_managed_server(entry, resolved):
        return False
    if not shape.drop(data, scope, project_root, SERVER_NAME):
        return False
    configfmt.write_document(fmt, settings_path, data)
    return True


#: The profile key that decides which file a scope writes to.
_SCOPE_SETTINGS = {
    "project": "project_settings",
    "local": "local_settings",
    "user": "user_settings",
}


def _infer_host(settings_path: Path, scope: str) -> str:
    """Which host's shape a settings path should be read in.

    The path is compared against every profile's file for the requested
    scope. A relative profile path is resolved against the working directory
    the way the command that built this path resolved it, so ``--scope
    project`` lands on the same file either way.
    """
    target = settings_path.expanduser()
    wanted = _SCOPE_SETTINGS.get(scope)
    for name, profile in HOST_PROFILES.items():
        for key in _SCOPE_SETTINGS.values():
            if key == wanted:
                candidate = Path(profile[key]).expanduser()
                if candidate == target or candidate.resolve() == target.resolve():
                    return name
    # A --settings override points at a file no profile names. The extension
    # still says how to parse it; what it cannot say is which container holds
    # the servers, and a wrong answer there would delete the wrong thing.
    return "claude-code"


def display_command(command: list[str]) -> str:
    """Quote an argv for display, the way the host's shell would need it.

    POSIX shells read shlex's single quotes; ``cmd.exe`` wants the Windows C
    runtime's convention, which is what ``list2cmdline`` emits. Paths with
    spaces are routine on Windows, so the choice is not cosmetic.
    """
    if sys.platform == "win32":
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(part) for part in command)
