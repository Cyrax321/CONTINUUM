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
has to work precisely in the state where the ``mcp`` extra is missing.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

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
    operator exactly what to add.
    """
    return Path(sys.executable).resolve().parent


# --------------------------------------------------------------------------- #
# the live handshake probe
# --------------------------------------------------------------------------- #


class _ProbeFailure(Exception):
    """The handshake did not complete; carries the child's stderr tail."""

    def __init__(self, reason: str, stderr_tail: list[str]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stderr_tail = stderr_tail


class _ServerProbe:
    """One spawned server, spoken to over raw stdio with a deadline.

    Reads go through ``os.read`` on the pipe's file descriptor, never
    through a text-mode wrapper, because ``readline`` on a translated pipe
    would quietly rewrite ``\\r\\n`` to ``\\n`` and hide the very framing
    difference the report is supposed to name. stderr is drained on a
    thread so a chatty server cannot deadlock the probe by filling the pipe
    buffer.
    """

    def __init__(self, command: list[str], db_path: str, timeout: float) -> None:
        self.command = command
        self.timeout = timeout
        #: The terminator observed on response frames: "CRLF (\\r\\n)" or
        #: "LF (\\n)", or None before the first frame arrives.
        self.wire_framing: str | None = None
        self._errors: list[str] = []
        self._id = 0
        self.proc = subprocess.Popen(
            command + ["--db", db_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._errors.append(raw.decode("utf-8", errors="replace").rstrip())

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read_line(self) -> str | None:
        """Read one frame, or None on timeout/EOF, bounded by ``timeout``."""
        selector = selectors.DefaultSelector()
        selector.register(self.proc.stdout, selectors.EVENT_READ)  # type: ignore[arg-type]
        try:
            buffer = b""
            deadline = time.monotonic() + self.timeout
            while b"\n" not in buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None  # timeout: "server never became ready"
                if not selector.select(remaining):
                    return None
                # os.read, not a buffered read: a BufferedReader would block
                # until its full count arrives or the pipe closes, and the
                # deadline would never fire mid-frame.
                chunk = os.read(self.proc.stdout.fileno(), 65536)  # type: ignore[union-attr]
                if not chunk:
                    return None  # EOF: the server closed the connection
                buffer += chunk
            line, _, _ = buffer.partition(b"\n")
            self._observe_framing(line)
            return line.decode("utf-8", errors="replace")
        finally:
            selector.close()

    def _observe_framing(self, raw: bytes) -> None:
        ending = "CRLF (\\r\\n)" if raw.endswith(b"\r") else "LF (\\n)"
        if self.wire_framing is None:
            self.wire_framing = ending
        elif ending not in self.wire_framing:
            self.wire_framing = f"mixed, last seen {ending}"

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the parsed response object.

        Frames whose id is not ours (a server-initiated notification, a
        log message) are skipped rather than misread as the answer.
        """
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        while True:
            line = self._read_line()
            if line is None:
                raise _ProbeFailure("no response within the deadline", self._errors_tail())
            try:
                response: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _ProbeFailure(f"unparseable response: {exc}", self._errors_tail()) from exc
            if response.get("id") == self._id:
                break
        if "error" in response:
            message = response["error"].get("message", "unknown error")
            raise _ProbeFailure(f"server returned an error: {message}", self._errors_tail())
        return response

    def notify(self, method: str) -> None:
        """Send a notification, which the server answers with silence."""
        self._send({"jsonrpc": "2.0", "method": method})

    def _errors_tail(self) -> list[str]:
        return self._errors[-STDERR_TAIL_LINES:]

    def close(self) -> None:
        """Shut the server down; the MCP protocol has no shutdown method."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


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
        try:
            response = probe.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "continuum-mcp-doctor", "version": "1.0"},
                },
            )
            result = response.get("result", {})
            server = result.get("serverInfo", {})
            probe.notify("notifications/initialized")
            listed = probe.request("tools/list", {})
            tools = [tool.get("name", "?") for tool in listed.get("result", {}).get("tools", [])]
        except _ProbeFailure as exc:
            exit_code = probe.proc.poll()
            detail = f"the server never completed the initialize handshake ({exc.reason})"
            if exit_code is not None:
                detail += (
                    f"; it exited with code {exit_code} before the handshake"
                    " -- this is what the host reports as CONNECTION_CLOSED"
                )
            if exc.stderr_tail:
                detail += f"; its stderr tail: {' | '.join(exc.stderr_tail)}"
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
        finally:
            probe.close()
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
        if check["status"] == "fail" and check.get("fix"):
            remedies.append(f"{check['check']}: {check['fix']}")
    remedies.append("registration: `continuum mcp install` (#834) or see docs/api/mcp.md")
    return remedies


_STATUS_MARK = {"pass": "[ok]  ", "fail": "[fail]", "warn": "[warn]", "info": "[note]"}


def render_doctor(report: dict[str, Any]) -> str:
    """Render the report as one actionable line per finding."""
    lines = ["MCP doctor: diagnosing why a host cannot connect (issue #835)", ""]
    for check in report["checks"]:
        lines.append(f"{_STATUS_MARK[check['status']]} {check['check']}: {check['detail']}")
        if check["status"] == "fail" and check.get("fix"):
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
