"""The installed ``continuum-mcp`` entry point, spoken to the way a client speaks to it.

Every other MCP test drives the server in-process (``tests/mcp_helpers.py``) or through
``sys.executable -m continuum.mcp[.server]`` (``tests/test_reasoning_summary.py``,
``tests/test_recovery_guidance.py``). Neither exercises what an MCP host actually spawns:
the ``continuum-mcp`` console script that ``[project.scripts]`` installs unconditionally.
That gap is why #697 shipped to PyPI (the entry point existed where its dependency did
not) and why #699 reached users before CI (PATH resolution of the entry point was never
under test). The failures live between the entry point and the protocol, a stretch of
road the suite had never driven.

Three behaviours are pinned here:

1. the console script, spawned by absolute path, completes the ``initialize``
   handshake and serves ``tools/list`` over real stdio (issue #834's baseline);
2. on Windows, a bare ``continuum-mcp`` cannot be spawned even when its directory is on
   the *child's* PATH, since ``CreateProcess`` resolves the calling process's PATH, not the
   environment it passes (the mechanism behind ``CONNECTION_CLOSED``, and the reason
   registration must bake resolved paths rather than rely on the host's PATH);
3. ``python -m continuum.mcp`` (the form ``continuum mcp install`` falls back to when
   no executable is on PATH) completes the same handshake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROTOCOL_VERSION = "2024-11-05"
#: Twelve tools: three read-only, nine mutating (``docs/api/mcp.md``).
TOOL_COUNT = 12


def _entrypoint() -> str | None:
    """The installed ``continuum-mcp`` executable, or ``None`` when absent.

    ``shutil.which`` first, so the test uses whatever the environment would find;
    the sibling-of-interpreter fallback covers environments where the Scripts
    directory is not on PATH (GitHub Actions runners, unactivated venvs), the
    same two-step resolution ``continuum mcp install`` performs.
    """
    found = shutil.which("continuum-mcp")
    if found:
        return found
    name = "continuum-mcp.exe" if os.name == "nt" else "continuum-mcp"
    sibling = Path(sys.executable).parent / name
    return str(sibling) if sibling.exists() else None


def _spawn(cmd: list[str], db: Path) -> subprocess.Popen[str]:
    """Start the server exactly as a host would: pipes, no shell, inherited env.

    The environment is a copy of the parent's rather than a minimal block: on
    Windows a child denied ``SystemRoot`` dies during interpreter startup on
    ``import _overlapped`` before any CONTINUUM code runs (issue #211).
    """
    env = dict(os.environ)
    env["CONTINUUM_DB"] = str(db)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        bufsize=1,
    )


def _request(proc: subprocess.Popen[str], payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON-RPC request and return its response."""
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, "server closed the connection before answering"
    return json.loads(line)


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.stdin:
        proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _handshake(cmd: list[str], db: Path) -> list[dict[str, Any]]:
    """Drive initialize + tools/list over real stdio; return the listed tools."""
    proc = _spawn(cmd, db)
    try:
        reply = _request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "entrypoint-test", "version": "0"},
                },
            },
        )
        server = reply.get("result", {}).get("serverInfo", {})
        assert server.get("name") == "continuum-mcp", server
        proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        proc.stdin.flush()
        listed = _request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = listed.get("result", {}).get("tools", [])
        assert len(tools) == TOOL_COUNT, [t.get("name") for t in tools]
        return tools
    finally:
        _stop(proc)


def test_console_script_handshake_over_stdio(tmp_path: Any) -> None:
    """The installed entry point serves the protocol a host consumes (issues #697, #699)."""
    script = _entrypoint()
    if script is None:
        pytest.skip(
            "no installed continuum-mcp console script (pip install -e '.[dev]' provides one)"
        )
    _handshake([script], tmp_path / "entrypoint.db")


def test_module_fallback_handshake_over_stdio(tmp_path: Any) -> None:
    """``python -m continuum.mcp`` (the no-executable fallback ``mcp install`` bakes)."""
    _handshake([sys.executable, "-u", "-m", "continuum.mcp"], tmp_path / "module.db")


@pytest.mark.skipif(sys.platform != "win32", reason="documents Windows CreateProcess resolution")
def test_bare_name_ignores_child_path_on_windows(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare command name resolves against the *parent's* PATH, never the child's.

    This is the mechanism behind ``CONNECTION_CLOSED`` (#699): an MCP host passes
    the server a constructed environment whose PATH may contain the Scripts
    directory, but ``CreateProcess`` ignores it and searches the calling process's
    own PATH. A venv that is not activated in the shell that launched the host
    therefore cannot be reached by bare name, which is why registration must
    bake an absolute path (issue #834) rather than trust the host's environment.
    """
    script = _entrypoint()
    if script is None:
        pytest.skip(
            "no installed continuum-mcp console script (pip install -e '.[dev]' provides one)"
        )
    script_dir = Path(script).parent.resolve()

    # Parent PATH: the script's own directory surgically removed, so resolution
    # has nothing to find. Any *other* continuum-mcp still reachable would make
    # the constraint untestable here, so that case skips rather than lies.
    kept = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != script_dir
    ]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))
    if shutil.which("continuum-mcp") is not None:
        pytest.skip(
            "another continuum-mcp remains reachable on PATH; cannot isolate the constraint"
        )

    # Child PATH: the script's directory explicitly present. If resolution
    # consulted the child's environment this spawn would succeed; on Windows it
    # does not, and the FileNotFoundError is what a host turns into
    # CONNECTION_CLOSED.
    child_env = dict(os.environ)
    child_env["PATH"] = os.pathsep.join([str(script_dir), *kept])
    child_env["CONTINUUM_DB"] = str(tmp_path / "bare.db")
    with pytest.raises(FileNotFoundError):
        proc = subprocess.Popen(
            ["continuum-mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        proc.kill()
