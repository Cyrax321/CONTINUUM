"""Turn ``CONNECTION_CLOSED`` into a diagnosis (issue #835).

When the MCP server fails to connect, the host shows exactly one opaque string.
The real causes — an executable the host cannot spawn, or a missing optional
dependency — produce identical client output, and the server's useful stderr
never reaches the user: a bare-name spawn fails at ``CreateProcess`` before any
CONTINUUM code runs, so the server cannot self-report. Diagnosis therefore has
to happen client-side, which is what ``continuum mcp doctor`` does: it checks
each failure class in order, prints one actionable line per finding, and names
the fix.

The checks are ordered cheapest-first and every one runs against a *fresh
subprocess*, never this process: the SDK check must not be satisfied by an
in-process import that a host's spawn would not share, and the PATH check must
not see a ``sys.path`` this interpreter was handed (pytest's ``pythonpath``
injection, ``PYTHONPATH``, an activated venv). The handshake probe reuses the
resolution from :mod:`continuum.mcp.install` so doctor and the installer can
never disagree about which command they are talking about.

Exit status is the health signal: zero only when no check failed — in
particular the resolved command must have completed a real ``initialize``
handshake, the one check that covers every cold-start failure class at once,
and the entry point must be reachable by a fresh process, because a
bare-name host config with an unfindable executable is a ``CONNECTION_CLOSED``
waiting to happen even when the server itself is healthy. Warnings (a missing
executable with the interpreter fallback available, the SDK's CRLF framing on
Windows) do not fail the run; they are conditions an operator should know
about, not states where the server does not work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from continuum.mcp.install import (
    SERVER_KEY,
    ProbeError,
    resolve_server_command,
)

__all__ = ["run_doctor"]

#: Tool-carrying requests need the initialized notification first, per the MCP
#: handshake: tools/list before notifications/initialized is a protocol error.
_TOOLS_REQUEST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _sdk_check() -> dict[str, Any]:
    """Is the ``mcp`` SDK importable by a fresh interpreter? (#697's failure class.)

    In a subprocess because an in-process import proves nothing: this process
    may have the SDK on ``sys.path`` through a path a spawned server would not
    share. The check prints the version on success, so the report can name the
    exact SDK the server would run.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mcp; from importlib.metadata import version; print(version('mcp'))",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return _check(
            "mcp sdk",
            "ok",
            f"importable by a fresh interpreter (mcp {probe.stdout.strip()})",
        )
    return _check(
        "mcp sdk",
        "fail",
        "not importable — the entry point exists but its dependency does not (#697)",
        "pip install 'continuum-agent[mcp]' (from a source checkout: pip install '.[mcp]')",
    )


def _which_in_fresh_process() -> str | None:
    """``shutil.which('continuum-mcp')`` as a newly spawned process sees it.

    The doctor process may have been started from a shell that activated a venv
    after launch, or under a test harness that rewired the environment; a
    freshly spawned interpreter inherits only the environment, which is the
    thing a host's spawn actually consults.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import shutil; print(shutil.which('continuum-mcp') or '')"],
        capture_output=True,
        text=True,
    )
    return probe.stdout.strip() or None


def _entry_point_checks(resolved: Sequence[str]) -> list[dict[str, Any]]:
    """Where the executable is, and what ``mcp install`` would register.

    The interesting state is the gap: the executable exists beside the running
    ``continuum`` (the sibling resolution found it) but no fresh process can
    find it on PATH. That is precisely why a host reports ``CONNECTION_CLOSED``
    while every diagnostic the operator runs by hand works — their shell
    activated the venv, the host did not (#699).
    """
    checks: list[dict[str, Any]] = []
    on_path = _which_in_fresh_process()
    if on_path:
        checks.append(_check("entry point", "ok", f"{on_path} (resolvable by a fresh process)"))
    elif len(resolved) == 1:
        checks.append(
            _check(
                "entry point",
                "fail",
                f"{resolved[0]} exists but is not on PATH a fresh process sees",
                "continuum mcp install bakes the absolute path into the host's config (#834)",
            )
        )
    else:
        checks.append(
            _check(
                "entry point",
                "warn",
                "no continuum-mcp executable on PATH; the interpreter form will be used",
                "install with pip/pipx/uv for a standalone executable, or run "
                "`continuum mcp install` to register the interpreter form",
            )
        )
    checks.append(
        _check(
            "resolution",
            "info",
            "command `mcp install` would register: " + " ".join(resolved),
        )
    )
    return checks


def _readline_with_timeout(proc: subprocess.Popen[bytes], timeout: float) -> bytes:
    """Read one raw stdout line, bounded in time.

    The bytes twin of ``install._readline_with_timeout``: ``select`` cannot wait
    on Windows pipes, so the read runs on a thread and the join is the deadline.
    Binary here because the framing check has to see the bytes as they travel.
    """
    assert proc.stdout is not None
    result: list[bytes] = []

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


def _handshake(command: Sequence[str], *, timeout: float = 20.0) -> dict[str, Any]:
    """Drive ``initialize`` + ``tools/list`` against ``command`` and report all of it.

    Unlike :func:`continuum.mcp.install.probe_server` — a gate that answers yes
    or no — this is a diagnostic: it also lists the tools and records the raw
    frame line endings. The pipe is deliberately binary: in text mode Python
    translates ``\\r\\n`` to ``\\n`` on read, and the CRLF framing defect
    (modelcontextprotocol/python-sdk#2433, mcp 2.2.0 on Windows) would become
    invisible to exactly the tool that exists to see it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="continuum-mcp-doctor-"))
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
            )
        except OSError as exc:
            raise ProbeError(f"cannot start the server ({exc})") from exc

        def drain_stderr() -> None:
            assert proc.stderr is not None
            errors.extend(line.decode("utf-8", "replace").rstrip() for line in proc.stderr)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "continuum-mcp-doctor", "version": "0"},
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            proc.stdin.flush()
            raw = _readline_with_timeout(proc, timeout)
            reply: dict[str, Any] = json.loads(raw.decode("utf-8"))
            server: dict[str, Any] = reply.get("result", {}).get("serverInfo", {})
            if server.get("name") != SERVER_KEY:
                raise ProbeError(
                    f"the command answered the handshake as a different server "
                    f"({server.get('name')!r})"
                )
            crlf = raw.endswith(b"\r\n")
            proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.write((json.dumps(_TOOLS_REQUEST) + "\n").encode("utf-8"))
            proc.stdin.flush()
            tools_raw = _readline_with_timeout(proc, timeout)
            tools_reply: dict[str, Any] = json.loads(tools_raw.decode("utf-8"))
            tools = [t.get("name", "?") for t in tools_reply.get("result", {}).get("tools", [])]
            return {"server": server, "tools": tools, "crlf": crlf}
        except json.JSONDecodeError as exc:
            raise ProbeError(
                f"the server's reply is not JSON ({exc}); stdout is reserved for "
                f"protocol frames, so anything else there breaks the protocol"
            ) from exc
        except UnicodeDecodeError as exc:
            raise ProbeError(f"the server's reply is not UTF-8 ({exc})") from exc
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except ProbeError as exc:
        # Surface the server's own stderr — the missing-extra remediation, a
        # storage error — because the host never shows it (issue #835's point).
        if stderr_thread is not None:
            stderr_thread.join(1.0)
        tail = "\n".join(errors[-5:]).strip()
        if tail:
            raise ProbeError(f"{exc}\nThe server reported:\n{tail}") from None
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_doctor(*, command: Sequence[str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Run every diagnostic and return the report; ``ok`` is the health signal.

    ``command`` overrides resolution for tests; ``None`` resolves exactly the
    way ``continuum mcp install`` does, so the doctor diagnoses the command the
    installer would register — not a hypothetical one.
    """
    resolved = list(command) if command is not None else resolve_server_command()
    checks: list[dict[str, Any]] = [_sdk_check()]
    checks.extend(_entry_point_checks(resolved))

    handshake: dict[str, Any] | None = None
    try:
        handshake = _handshake(resolved, timeout=timeout)
        checks.append(
            _check(
                "handshake",
                "ok",
                f"initialize answered by {handshake['server'].get('name')}; "
                f"{len(handshake['tools'])} tools listed",
            )
        )
        if handshake["crlf"]:
            checks.append(
                _check(
                    "framing",
                    "warn",
                    "stdio frames end with CRLF (mcp SDK on Windows, "
                    "modelcontextprotocol/python-sdk#2433); tolerated by Claude Code, "
                    "rejected by strict NDJSON clients",
                )
            )
    except ProbeError as exc:
        checks.append(
            _check(
                "handshake",
                "fail",
                str(exc),
                "run `continuum mcp install` to register a verified command (#834); "
                "see docs/api/mcp.md",
            )
        )

    ok = all(c["status"] != "fail" for c in checks)
    return {"ok": ok, "command": resolved, "checks": checks, "handshake": handshake}
