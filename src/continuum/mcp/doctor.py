"""Client-side diagnosis of MCP connection failures (issue #835).

When an MCP host fails to start the CONTINUUM server, the user sees one
opaque string: ``CONNECTION_CLOSED`` or "server never became ready". The
real causes -- an executable the host cannot spawn, or the optional ``mcp``
extra not installed -- produce identical client output, because the useful
stderr never crosses the stdio protocol pipe and the server cannot
self-report a spawn failure at all (a bare-name spawn fails at
``CreateProcess`` before any CONTINUUM code runs).

The diagnosis therefore has to happen client-side, which is what
``continuum mcp doctor`` does. Every probe runs in a freshly spawned
process, never this one, because PATH resolution and importability must be
observed the way a host would observe them -- an already-running Python
process has mutable ``sys.path`` and may have imported the SDK long ago.

This module is pure standard library and must stay that way: the doctor
has to work precisely in the state where the ``mcp`` extra is missing. The
two sibling modules it reads configuration from (``authz`` for the names of
the variables that decide authorization, ``install`` for the per-host
settings paths) are themselves standard library only, so importing them
keeps that guarantee.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from continuum.mcp.authz import POLICY_ENV_VAR, POLICY_ENV_VAR_ALIAS
from continuum.mcp.install import HOST_PROFILES, SERVER_NAME

__all__ = ["run_doctor", "render_doctor"]

#: The console script a real MCP host spawns; the name ``.mcp.json`` declares
#: and resolves through PATH.
CONSOLE_SCRIPT = "continuum-mcp"

#: Protocol version sent in the initialize request. Any SDK in the supported
#: range accepts it and answers with the version it negotiated, which the
#: doctor reports.
PROTOCOL_VERSION = "2024-11-05"

#: Upstream wire-framing bug the CRLF note references: on Windows the MCP SDK
#: terminates stdio frames with CRLF, which Claude Code tolerates and strict
#: NDJSON clients reject.
CRLF_UPSTREAM_ISSUE = "modelcontextprotocol/python-sdk#2433"

#: How long each handshake read waits before the probe gives up, in seconds.
HANDSHAKE_TIMEOUT_SECONDS = 15.0

#: Lines of child stderr kept for the report; enough to carry the one-line
#: cold-start errors the server itself prints (``continuum.mcp.server.main``).
STDERR_TAIL_LINES = 15

#: The remedy for a missing extra, spelled the way ``continuum.mcp.server``
#: spells it in its own cold-start error so the two never disagree.
INSTALL_COMMAND = "pip install continuum-agent[mcp]"

#: The command that writes a host registration. Named by the absent-registration
#: finding, which is a different remedy than a missing SDK and must not reuse
#: ``INSTALL_COMMAND``: one installs the extra, this one writes the file.
INSTALL_SERVER_COMMAND = "continuum mcp install"

#: The client name the probe declares when no registration names one. It is
#: correct for proving the server is alive and wrong as a prediction of what a
#: host sends, which is why the permission check below declares the name read
#: out of the registration instead and reports what came back.
DEFAULT_PROBE_CLIENT_NAME = "continuum-mcp-doctor"

#: The guarded tool the permission probe calls. Mutating (it is decorated with
#: the authorization guard), and it rejects a run id no database holds with
#: ``_DOWNSTREAM_MARKER`` *after* the guard has already let the call through,
#: which is what separates an authorized caller from a refused one.
PERMISSION_PROBE_TOOL = "continuum_record_progress"

#: A run id no database contains. ``goal`` is deliberately not sent, so the tool
#: cannot create the run it is asked about, and the probe writes nothing either
#: way: the verdict is read from a throwaway database.
PERMISSION_PROBE_RUN_ID = "continuum-mcp-doctor-no-such-run"

#: What an authorized call looks like from the outside: the guard admitted the
#: caller and the tool then refused its own arguments.
_DOWNSTREAM_MARKER = "no such run"

#: Markers of an authorization refusal in the server's own answer. These are
#: observations of a message the server sent, not a re-implementation of the
#: policy: the doctor never decides who is allowed, it reports what the
#: server said. A refusal whose wording later changes therefore degrades this
#: check to ``warn``, never silently to ``pass``.
_REFUSAL_MARKERS = (
    "is not permitted to use the mutating tool",
    "did not identify itself",
)

#: Environment variables that decide authorization. Stripped from the child's
#: inherited environment so the verdict reflects the registration under test
#: rather than whatever the operator's shell happened to export.
_POLICY_ENV_VARS = frozenset({POLICY_ENV_VAR, POLICY_ENV_VAR_ALIAS})

#: Probe code for check 1: import the SDK in a fresh interpreter. The version
#: print is best-effort metadata, not a requirement -- a working SDK without
#: install metadata is still a working SDK.
_SDK_PROBE = (
    "import mcp\n"
    "try:\n"
    "    import importlib.metadata\n"
    "    print(importlib.metadata.version('mcp'))\n"
    "except Exception:\n"
    "    print('unknown version')\n"
)

#: Probe code for check 2: resolve the console script and the module fallback
#: the way a freshly spawned process sees them. ``shutil.which`` from the
#: doctor's own process would answer for a process that has already mutated
#: its environment; the host has not.
_RESOLUTION_PROBE = (
    "import importlib.util, json, os, shutil\n"
    "print(json.dumps({\n"
    "    'which': shutil.which('continuum-mcp'),\n"
    "    'module': importlib.util.find_spec('continuum.mcp') is not None,\n"
    "    'path': os.environ.get('PATH', ''),\n"
    "}))\n"
)


# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #


def _run_probe(code: str, *, timeout: float) -> tuple[int, str, str]:
    """Run ``python -c code`` in a fresh process, returning (rc, out, err).

    The environment is inherited verbatim: the doctor must observe the PATH
    and ``PYTHONPATH`` a host-spawned child would inherit, not a sanitized
    one, or the diagnosis would describe a machine the user does not have.
    """
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _last_line(text: str) -> str:
    """The last non-empty line, because tracebacks bury the useful part."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


def _path_entries(path_value: str) -> list[str]:
    """Split a PATH string into its non-empty entries."""
    return [entry for entry in path_value.split(os.pathsep) if entry]


def _scripts_dir() -> Path:
    """The directory holding this interpreter's console scripts.

    The classic failure: ``continuum-mcp`` exists right here but the host's
    PATH does not include this directory, so the bare-name spawn in
    ``.mcp.json`` fails at ``CreateProcess``. Naming the directory tells the
    operator exactly what to add, so it has to be the *right* directory.

    That rules out the obvious ``Path(sys.executable).resolve().parent``: a
    venv's ``python`` is a symlink to the base interpreter, and resolving it
    walks past the venv to that interpreter's ``bin``, which neither holds
    the script nor is the directory anyone should add. Windows has the same
    shape one level over: scripts live in ``Scripts`` beside the executable,
    not beside it. ``sysconfig`` reports the directory the install actually
    uses on both platforms.
    """
    return Path(sysconfig.get_path("scripts"))


# --------------------------------------------------------------------------- #
# the live handshake probe
# --------------------------------------------------------------------------- #


class _ProbeFailure(Exception):
    """The handshake did not complete; ``reason`` is the diagnosis to report.

    It deliberately does not carry the child's stderr: the tail is only worth
    reading once the child has terminated, which happens in ``close()`` after
    this is raised. Snapshotting it here would report a deadline where the
    cause was a moment away.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ServerProbe:
    """One spawned server, spoken to over raw stdio with a deadline.

    Reads go through ``os.read`` on the pipe's file descriptor, never
    through a text-mode wrapper, because ``readline`` on a translated pipe
    would quietly rewrite ``\\r\\n`` to ``\\n`` and hide the very framing
    difference the report is supposed to name. stderr is drained on a
    thread so a chatty server cannot deadlock the probe by filling the pipe
    buffer.
    """

    def __init__(
        self,
        command: list[str],
        db_path: str,
        timeout: float,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.timeout = timeout
        #: The terminator observed on response frames: "CRLF (\\r\\n)" or
        #: "LF (\\n)", or None before the first frame arrives.
        self.wire_framing: str | None = None
        self._errors: list[str] = []
        self._id = 0
        #: Bytes already read past the last returned frame. One ``os.read`` can
        #: deliver several frames at once, so the surplus is kept for the next
        #: ``_read_line`` instead of being dropped.
        self._inbox: bytes = b""
        self.proc = subprocess.Popen(
            command + ["--db", db_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._errors.append(raw.decode("utf-8", errors="replace").rstrip())

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read_chunk(self, deadline: float) -> bytes | None:
        """Read up to 64KiB, or ``None`` at the deadline / on EOF.

        The read is blocking on a worker thread because Windows has no
        ``select()`` for pipes: its ``select`` accepts sockets only, so
        registering a child's stdout raises ``WinError 10038`` and every
        probe would die before the first frame arrives. The thread is
        abandoned on timeout, which is safe because the caller has already
        given up and ``close()`` kills the process, closing the pipe and
        unblocking the read.
        """
        chunk_box: queue.Queue[bytes | None] = queue.Queue()

        def reader() -> None:
            try:
                chunk_box.put(os.read(self.proc.stdout.fileno(), 65536))  # type: ignore[union-attr]
            except OSError:
                # A closed pipe between the put and the read: treat as EOF.
                chunk_box.put(None)

        threading.Thread(target=reader, daemon=True).start()
        try:
            return chunk_box.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return None  # deadline: "server never became ready"

    def _read_line(self) -> str | None:
        """Read one frame, or None on timeout/EOF, bounded by ``timeout``.

        Bytes past the first newline are kept in ``self._inbox`` rather than
        discarded: one ``os.read`` routinely returns more than a frame, because
        the server writes its response and any notification back to back and
        the pipe coalesces whatever was written between reads. Throwing the
        tail away would drop the frame a later request is waiting for and the
        report would name a timeout the server never caused.
        """
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self._inbox:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None  # timeout: "server never became ready"
            # os.read, not a buffered read: a BufferedReader would block
            # until its full count arrives or the pipe closes, and the
            # deadline would never fire mid-frame.
            chunk = self._read_chunk(deadline)
            if not chunk:
                return None  # timeout or EOF: the server closed the connection
            self._inbox += chunk
        line, sep, rest = self._inbox.partition(b"\n")
        self._inbox = rest if sep else b""
        self._observe_framing(line)
        return line.decode("utf-8", errors="replace")

    def _observe_framing(self, raw: bytes) -> None:
        ending = "CRLF (\\r\\n)" if raw.endswith(b"\r") else "LF (\\n)"
        if self.wire_framing is None:
            self.wire_framing = ending
        elif ending not in self.wire_framing:
            self.wire_framing = f"mixed, last seen {ending}"

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the parsed response object.

        Frames whose id is not ours (a server-initiated notification, a
        log message) are skipped rather than misread as the answer. A
        ``result`` carrying ``isError`` is returned like any other result:
        the SDK reports a tool that refused a call as a result, not as a
        JSON-RPC error, and the refusal is exactly what the permission
        probe has to read.
        """
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        while True:
            line = self._read_line()
            if line is None:
                raise _ProbeFailure("no response within the deadline")
            try:
                response: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _ProbeFailure(f"unparseable response: {exc}") from exc
            if response.get("id") == self._id:
                break
        if "error" in response:
            message = response["error"].get("message", "unknown error")
            raise _ProbeFailure(f"server returned an error: {message}")
        return response

    def notify(self, method: str) -> None:
        """Send a notification, which the server answers with silence."""
        self._send({"jsonrpc": "2.0", "method": method})

    def _errors_tail(self) -> list[str]:
        return self._errors[-STDERR_TAIL_LINES:]

    def close(self) -> None:
        """Shut the server down; the MCP protocol has no shutdown method.

        The stderr reader is joined after the child terminates so the tail a
        caller reads next is the child's final output: a server that only
        writes its cause once it gives up would otherwise be killed mid-message
        and the report would name a deadline instead of the cause.
        """
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._stderr_thread.join(timeout=1.0)


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def _check_sdk(timeout: float) -> dict[str, Any]:
    """Check 1: the ``mcp`` extra imports in a fresh interpreter."""
    try:
        returncode, stdout, stderr = _run_probe(_SDK_PROBE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "check": "sdk-import",
            "status": "fail",
            "detail": f"importing 'mcp' in a fresh interpreter timed out after {timeout:.0f}s",
            "fix": INSTALL_COMMAND,
        }
    if returncode == 0:
        return {
            "check": "sdk-import",
            "status": "pass",
            "detail": f"the 'mcp' SDK imports cleanly in a fresh interpreter (mcp {stdout})",
        }
    return {
        "check": "sdk-import",
        "status": "fail",
        "detail": f"the 'mcp' SDK does not import: {_last_line(stderr)}",
        "fix": f"install it with: {INSTALL_COMMAND}",
    }


def _check_resolution(timeout: float) -> dict[str, Any]:
    """Check 2: how a freshly spawned process resolves the server command."""
    try:
        returncode, stdout, stderr = _run_probe(_RESOLUTION_PROBE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "check": "command-resolution",
            "status": "fail",
            "detail": f"resolving '{CONSOLE_SCRIPT}' timed out after {timeout:.0f}s",
            "fix": "check PATH for loops or huge directories",
            "resolved": None,
            "module_fallback": False,
        }
    if returncode != 0:
        return {
            "check": "command-resolution",
            "status": "fail",
            "detail": f"the resolution probe itself failed: {_last_line(stderr)}",
            "fix": "report this; the probe is continuum's own code",
            "resolved": None,
            "module_fallback": False,
        }
    data = json.loads(stdout.splitlines()[-1])
    which: str | None = data["which"]
    module_available: bool = data["module"]
    path_entries = _path_entries(data["path"])
    scripts_dir = _scripts_dir()

    # Check 4 rides along here: PATH visibility is the diagnosis for a
    # missing exe, so it is computed where the resolution result is.
    if which is not None:
        return {
            "check": "command-resolution",
            "status": "pass",
            "detail": (
                f"'{CONSOLE_SCRIPT}' resolves to {which} "
                f"({len(path_entries)} PATH entries visible to the probe process)"
            ),
            "resolved": which,
            "module_fallback": module_available,
        }
    detail = (
        f"'{CONSOLE_SCRIPT}' is not on the PATH a fresh process sees ({len(path_entries)} entries)"
    )
    if module_available:
        detail += "; the python -m continuum.mcp fallback is available"
    if _script_exists_next_to_interpreter():
        detail += f"; the script exists at {scripts_dir} but that directory is not on PATH"
        fix = f"add {scripts_dir} to PATH, or point the host at the module form"
    else:
        detail += "; the script is not installed next to this interpreter either"
        fix = f"reinstall with: {INSTALL_COMMAND}"
    return {
        "check": "command-resolution",
        "status": "fail",
        "detail": detail,
        "fix": fix,
        "resolved": None,
        "module_fallback": module_available,
    }


def _script_exists_next_to_interpreter() -> bool:
    """Whether ``continuum-mcp`` sits in this interpreter's scripts dir.

    Distinguishes "installed but not on PATH" (fix: PATH) from "not
    installed at all" (fix: install). ``.exe`` is the Windows console
    script; POSIX scripts carry no suffix.
    """
    scripts = _scripts_dir()
    return (scripts / CONSOLE_SCRIPT).exists() or (scripts / f"{CONSOLE_SCRIPT}.exe").exists()


def _handshake_command(resolution: dict[str, Any]) -> list[str] | None:
    """The argv to probe: the resolved script, else the module fallback."""
    resolved = resolution.get("resolved")
    if resolved:
        return [str(resolved)]
    if resolution.get("module_fallback"):
        return [sys.executable, "-u", "-m", "continuum.mcp"]
    return None


def _check_handshake(
    command: list[str] | None, timeout: float
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check 3: a live spawn plus a real ``initialize`` handshake.

    Returns the finding and the extra notes (wire framing). This is the
    single check that catches every cold-start failure class at once: a
    missing extra, an unspawnable executable and a wedged server all
    surface here as "no response" plus the child's own stderr tail.
    """
    notes: list[dict[str, Any]] = []
    if command is None:
        return (
            {
                "check": "handshake",
                "status": "fail",
                "detail": "no spawnable command to probe (no script on PATH, no module fallback)",
                "fix": INSTALL_COMMAND,
            },
            notes,
        )
    with tempfile.TemporaryDirectory(prefix="continuum-mcp-doctor-") as tmp:
        db_path = str(Path(tmp) / "probe.db")
        try:
            probe = _ServerProbe(command, db_path, timeout)
        except OSError as exc:
            return (
                {
                    "check": "handshake",
                    "status": "fail",
                    "detail": f"spawning {' '.join(command)} failed: {exc}",
                    "fix": "the host cannot spawn it either; fix the executable or PATH",
                },
                notes,
            )
        failure_reason: str | None = None
        try:
            response = probe.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": DEFAULT_PROBE_CLIENT_NAME, "version": "1.0"},
                },
            )
            result = response.get("result", {})
            server = result.get("serverInfo", {})
            probe.notify("notifications/initialized")
            listed = probe.request("tools/list", {})
            tools = [tool.get("name", "?") for tool in listed.get("result", {}).get("tools", [])]
        except _ProbeFailure as exc:
            # The child is typically still alive when the deadline fires, and
            # it may not have written its cause yet. The tail is read *after*
            # close() below, once the child has terminated and the stderr
            # reader has drained the pipe: a snapshot taken here would name a
            # deadline where the child's own message was a moment away.
            failure_reason = exc.reason
        finally:
            probe.close()
        if failure_reason is not None:
            exit_code = probe.proc.poll()
            detail = f"the server never completed the initialize handshake ({failure_reason})"
            if exit_code is not None:
                detail += (
                    f"; it exited with code {exit_code} before the handshake"
                    " -- this is what the host reports as CONNECTION_CLOSED"
                )
            stderr_tail = probe._errors_tail()
            if stderr_tail:
                detail += f"; its stderr tail: {' | '.join(stderr_tail)}"
            return (
                {
                    "check": "handshake",
                    "status": "fail",
                    "detail": detail,
                    "fix": "the stderr above usually names the cause; if not: " + INSTALL_COMMAND,
                    "command": command,
                },
                notes,
            )
        server_version = server.get("version") or "unknown version"
        if probe.wire_framing:
            notes.append(
                {
                    "check": "wire-framing",
                    "status": "info",
                    "detail": f"response frames end with {probe.wire_framing}",
                }
            )
            if "CRLF" in probe.wire_framing:
                notes.append(
                    {
                        "check": "wire-framing",
                        "status": "warn",
                        "detail": (
                            "the SDK terminates stdio frames with CRLF "
                            f"({CRLF_UPSTREAM_ISSUE}): tolerated by Claude Code, "
                            "rejected by strict NDJSON clients"
                        ),
                    }
                )
        return (
            {
                "check": "handshake",
                "status": "pass",
                "detail": (
                    f"{command[0]} answered initialize: server {server.get('name', '?')} "
                    f"v{server_version}, protocol "
                    f"{result.get('protocolVersion', '?')}, {len(tools)} tools"
                ),
                "command": command,
                "server": server.get("name"),
                "protocol_version": result.get("protocolVersion"),
                "tools": tools,
            },
            notes,
        )


# --------------------------------------------------------------------------- #
# the registration, and the client name it bakes
# --------------------------------------------------------------------------- #


def _baked_client(entry: Any) -> str | None:
    """The client name baked into a registration's environment, if any.

    ``install`` writes ``CONTINUUM_MCP_MUTATING_CLIENTS`` as a per-host
    literal, so its presence is what makes an entry a registration worth
    probing. The primary spelling is accepted too, because a hand-written
    registration may use it; which of the two a given server honours is the
    server's business, not this function's.
    """
    if not isinstance(entry, dict):
        return None
    env = entry.get("env")
    if not isinstance(env, dict):
        return None
    for var in (POLICY_ENV_VAR_ALIAS, POLICY_ENV_VAR):
        baked = env.get(var)
        if isinstance(baked, str) and baked.strip():
            return baked.strip()
    return None


def _servers_at(path: Path, container_key: str | None, container_id: str | None) -> dict[str, Any]:
    """The ``mcpServers`` dict of one settings file, empty when unreadable.

    A file that is absent, unreadable or not JSON yields an empty dict rather
    than raising: the doctor is asked to run on a machine that is already
    broken, and a settings file it cannot parse is a finding about the
    registration, not a crash of the diagnosis.
    """
    if not path.is_file():
        return {}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if container_key is not None:
        scoped = data.get(container_key)
        if not isinstance(scoped, dict):
            return {}
        section = scoped.get(container_id or "")
        if not isinstance(section, dict):
            return {}
        data = section
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _registration_sites(
    cwd: Path,
) -> Iterator[tuple[str, Path, str | None, str | None, dict[str, Any]]]:
    """Every place a registration for this project could live, most specific first.

    Project scope leads because it is cwd-relative: its answer depends on the
    checkout rather than on whose account the process runs as. Local and user
    scope follow, in that order, because ``mcp install`` defaults to local and
    local beats project in the host's own precedence. The container key and id
    travel with each site so an entry found in a nested per-project section
    can be read back out of the file later.
    """
    for host, profile in HOST_PROFILES.items():
        project = Path(profile["project_settings"])
        path = project if project.is_absolute() else cwd / project
        yield host, path, None, None, _servers_at(path, None, None)
    for host, profile in HOST_PROFILES.items():
        path = Path(profile["local_settings"]).expanduser()
        key = profile["local_projects_key"]
        yield host, path, key, str(cwd), _servers_at(path, key, str(cwd))
    for host, profile in HOST_PROFILES.items():
        path = Path(profile["user_settings"]).expanduser()
        yield host, path, None, None, _servers_at(path, None, None)


def find_registration(cwd: Path) -> dict[str, Any] | None:
    """The registration written for this project, or ``None`` when there is none.

    Returns the host it was written for, the file and section it lives in, and
    the client name its environment bakes, which is the value the permission
    check declares and then verifies.
    """
    for host, settings_path, container_key, container_id, servers in _registration_sites(cwd):
        baked = _baked_client(servers.get(SERVER_NAME))
        if baked is None:
            continue
        return {
            "host": host,
            "settings_path": settings_path,
            "container_key": container_key,
            "container_id": container_id,
            "baked_client": baked,
        }
    return None


def _probe_environment(registration: dict[str, Any]) -> dict[str, str]:
    """The child's environment: this process's, minus the policy, plus the entry's.

    Authorization is decided by the environment the server is *spawned* with,
    so the probe must not carry the operator's shell into the verdict. A
    ``CONTINUUM_MCP_MUTATING_CLIENTS`` exported in the terminal running the
    doctor would otherwise authorize a registration that grants nothing, which
    is the silent failure this check exists to end. ``PATH`` and
    ``PYTHONPATH`` are untouched: the child still has to be spawnable and
    importable for the call to say anything at all.
    """
    env = {key: value for key, value in os.environ.items() if key not in _POLICY_ENV_VARS}
    env.update(_registration_env(registration))
    return env


def _registration_env(registration: dict[str, Any]) -> dict[str, str]:
    """The environment block a registration hands the server, by variable.

    ``find_registration`` reports only what the finding needs (the file and
    the baked name), so the entry is re-read from the file it names rather
    than carried around. That keeps the env block sourced from the file on
    disk, which is the thing being verified: reading it out of a summary
    would verify the summary instead.
    """
    entry = _servers_at(
        registration["settings_path"],
        registration["container_key"],
        registration["container_id"],
    ).get(SERVER_NAME)
    env = entry.get("env") if isinstance(entry, dict) else None
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items()}


def _refusal_permitted(text: str) -> str | None:
    """The callers a refusal says are permitted, or ``None`` when it names none.

    Read out of the server's message so the finding can name both strings the
    operator has to reconcile: what the registration baked, and what the server
    actually grants.
    """
    marker = "Permitted callers: "
    index = text.find(marker)
    if index == -1:
        return None
    tail = text[index + len(marker) :]
    stop = tail.find(".")
    return (tail if stop == -1 else tail[:stop]).strip() or None


def _tool_text(result: dict[str, Any]) -> str:
    """The text of a ``tools/call`` result, joined for reading and matching."""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict)).strip()


def _check_mutation_access(
    command: list[str], registration: dict[str, Any] | None, timeout: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Checks 5 and 6: the caller name the server uses, and whether it may mutate.

    ``install`` writes a client name into a registration and never learns what
    the host actually sends, so a wrong guess leaves a registration that
    installs cleanly, passes the handshake and silently keeps the caller on the
    read-only tools. Nothing else in the doctor looks at that name, so this is
    where it is observed rather than assumed: the probe declares the name read
    out of the registration, then calls a guarded tool and reports the verdict
    the server returns.

    The verdict is read, never predicted. An authorized call and a refused one
    are both reported by the SDK as ``isError`` results whose text differs, and
    that difference is the entire value of the check: a call that got past
    authorization and then failed on its own arguments (``no such run``) is
    proof the caller may mutate, while a refusal is proof of degradation.
    Collapsing the two would leave the original bug invisible, because a
    healthy server and a silently read-only one both answer with an error.
    """
    if registration is None:
        absent = {
            "check": "client-name",
            "status": "warn",
            "detail": (
                f"no {SERVER_NAME} registration was found for this project, so the "
                "name the server keys authorization on is unknown; without it the "
                "caller is read-only and the agent silently loses its mutating tools"
            ),
            "fix": f"run: {INSTALL_SERVER_COMMAND}",
            "baked_client": None,
            "observed_client": None,
        }
        return absent, {
            "check": "mutation-permitted",
            "status": "info",
            "detail": (
                f"not probed: no registration names a client to declare, so "
                f"{PERMISSION_PROBE_TOOL} was left uncalled"
            ),
            "authorized": None,
        }

    baked = str(registration["baked_client"])
    host = str(registration["host"])
    settings_path = registration["settings_path"]
    with tempfile.TemporaryDirectory(prefix="continuum-mcp-doctor-authz-") as tmp:
        db_path = str(Path(tmp) / "authz-probe.db")
        try:
            probe = _ServerProbe(command, db_path, timeout, env=_probe_environment(registration))
        except OSError as exc:
            reason = f"spawning {' '.join(command)} for the authorization probe failed: {exc}"
            return (
                _client_name_check(
                    "warn",
                    f"the baked client name could not be verified ({reason})",
                    registration,
                    None,
                    None,
                ),
                _permission_check(
                    "warn", f"the mutating tool was not called: {reason}", None, None
                ),
            )
        failure_reason: str | None = None
        try:
            probe.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": baked, "version": "1.0"},
                },
            )
            probe.notify("notifications/initialized")
            called = probe.request(
                "tools/call",
                {
                    "name": PERMISSION_PROBE_TOOL,
                    "arguments": {"run_id": PERMISSION_PROBE_RUN_ID, "completed": 1},
                },
            )
        except _ProbeFailure as exc:
            failure_reason = exc.reason
        finally:
            probe.close()

    if failure_reason is not None:
        reason = f"the server never answered the {PERMISSION_PROBE_TOOL} call ({failure_reason})"
        return (
            _client_name_check(
                "warn",
                f"the baked client name could not be verified: {reason}",
                registration,
                None,
                None,
            ),
            _permission_check("warn", reason, None, None),
        )

    result = called.get("result", {})
    text = _tool_text(result)
    refused = any(marker in text for marker in _REFUSAL_MARKERS)

    if refused:
        permitted = _refusal_permitted(text)
        fix = (
            f"set {POLICY_ENV_VAR_ALIAS} in {settings_path} to the name your host sends in "
            f"clientInfo.name, then re-run: {INSTALL_SERVER_COMMAND} --host {host}"
        )
        permitted_text = permitted if permitted else "no caller at all"
        return (
            _client_name_check(
                "fail",
                f"the registration bakes client name {baked!r} but the server refuses it as a "
                f"mutating caller, permitting {permitted_text}: {text}",
                registration,
                baked,
                permitted,
                fix=fix,
            ),
            _permission_check(
                "fail",
                f"the server refused {PERMISSION_PROBE_TOOL} for the caller it observes as "
                f"{baked!r}, which means the agent keeps only the read-only tools: {text}",
                False,
                text,
                fix=fix,
            ),
        )

    if _DOWNSTREAM_MARKER in text or not result.get("isError"):
        return (
            _client_name_check(
                "pass",
                f"the registration bakes client name {baked!r} and the server authorizes it: "
                f"{PERMISSION_PROBE_TOOL} was reached as a mutating caller. Authorization "
                f"matched {baked!r} exactly, so this is only as good as the host sending "
                "that name byte for byte",
                registration,
                baked,
                baked,
            ),
            _permission_check(
                "pass",
                f"{PERMISSION_PROBE_TOOL} was authorized and then refused its own arguments "
                f"({_DOWNSTREAM_MARKER}), which is what an authorized caller looks like",
                True,
                text,
            ),
        )

    return (
        _client_name_check(
            "warn",
            f"the server answered the {PERMISSION_PROBE_TOOL} call with something that is "
            f"neither an authorization nor a downstream refusal, so the baked name {baked!r} "
            f"could not be confirmed either way: {text}",
            registration,
            baked,
            None,
        ),
        _permission_check(
            "warn",
            f"{PERMISSION_PROBE_TOOL} neither authorized nor refused recognisably: {text}",
            None,
            text,
        ),
    )


def _client_name_check(
    status: str,
    detail: str,
    registration: dict[str, Any],
    observed: str | None,
    permitted: str | None,
    *,
    fix: str | None = None,
) -> dict[str, Any]:
    """Build the ``client-name`` finding.

    Carries the observed name beside the baked one so a consumer reading the
    payload can compare the strings itself rather than parsing prose.
    """
    check: dict[str, Any] = {
        "check": "client-name",
        "status": status,
        "detail": detail,
        "baked_client": registration["baked_client"],
        "observed_client": observed,
        "permitted_clients": permitted,
    }
    if fix:
        check["fix"] = fix
    return check


def _permission_check(
    status: str,
    detail: str,
    authorized: bool | None,
    observed: str | None,
    *,
    fix: str | None = None,
) -> dict[str, Any]:
    """Build the ``mutation-permitted`` finding.

    ``authorized`` is the distinction the whole check exists to keep: ``True``
    when the guarded tool was reached, ``False`` when the server refused the
    caller, ``None`` when no verdict was obtained.
    """
    check: dict[str, Any] = {
        "check": "mutation-permitted",
        "status": status,
        "detail": detail,
        "tool": PERMISSION_PROBE_TOOL,
        "authorized": authorized,
    }
    if observed:
        check["server_said"] = observed
    if fix:
        check["fix"] = fix
    return check


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def run_doctor(*, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run every check in order and return the report payload.

    The verdict is the handshake: only an install whose server completes a
    real ``initialize`` round-trip is healthy, because that is the exact
    exchange a host performs and reports as ``CONNECTION_CLOSED`` when it
    fails. Wire-framing notes never flip the verdict -- CRLF is tolerated
    by the hosts that matter today; it is surfaced, not punished.
    """
    checks = [_check_sdk(timeout), _check_resolution(timeout)]
    command = _handshake_command(checks[-1])
    handshake, notes = _check_handshake(command, timeout)
    checks.append(handshake)
    checks.extend(notes)
    if handshake["status"] == "pass" and command is not None:
        # Only worth probing a server that answered: the permission question
        # is unanswerable without one, and reporting it alongside a failed
        # handshake would bury the cause of the failure under its symptom.
        checks.extend(_check_mutation_access(command, find_registration(Path.cwd()), timeout))
    healthy = all(check["status"] != "fail" for check in checks)
    return {
        "command": "mcp doctor",
        "healthy": healthy,
        "checks": checks,
        "remedies": _remedies(checks),
    }


def _remedies(checks: list[dict[str, Any]]) -> list[str]:
    """Pointers to the fixes for whatever failed, in the order asked for."""
    remedies: list[str] = []
    for check in checks:
        # A warn carries its next step too: an absent registration is not a
        # failure, but the command that resolves it is the whole point of
        # reporting it, and it is only printed when something went wrong.
        if check["status"] in ("fail", "warn") and check.get("fix"):
            remedies.append(f"{check['check']}: {check['fix']}")
    remedies.append("registration: `continuum mcp install` (#834) or see docs/api/mcp.md")
    return remedies


_STATUS_MARK = {"pass": "[ok]  ", "fail": "[fail]", "warn": "[warn]", "info": "[note]"}


def render_doctor(report: dict[str, Any]) -> str:
    """Render the report as one actionable line per finding."""
    lines = ["MCP doctor: diagnosing why a host cannot connect (issue #835)", ""]
    for check in report["checks"]:
        lines.append(f"{_STATUS_MARK[check['status']]} {check['check']}: {check['detail']}")
        # A warn prints its next step too. An absent registration is not a
        # broken install, so it never flips the verdict and the remedies block
        # is never rendered for it, which would leave the one command that
        # resolves it invisible to the operator reading the report.
        if check["status"] in ("fail", "warn") and check.get("fix"):
            lines.append(f"       fix: {check['fix']}")
    lines.append("")
    if report["healthy"]:
        lines.append("healthy: the server starts and completes the initialize handshake.")
    else:
        lines.append("not healthy: see the [fail] lines above; each names its cause and fix.")
        lines.append("remedies:")
        for remedy in report["remedies"]:
            lines.append(f"  - {remedy}")
    return "\n".join(lines)
