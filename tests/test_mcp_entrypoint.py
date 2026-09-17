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
   no executable is on PATH) completes the same handshake;
4. the raw wire framing is observed, not assumed: every response frame ends
   ``\r\n`` on Windows (the upstream SDK defect, modelcontextprotocol/
   python-sdk#2433) and ``\n`` elsewhere, and the frame parses under either
   terminator (issue #839).
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
#: Thirteen tools: three read-only, ten mutating (``docs/api/mcp.md``).
TOOL_COUNT = 13


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


# --------------------------------------------------------------------------- #
# raw-wire framing (issue #839)
# --------------------------------------------------------------------------- #


def _parse_raw_frame(line: bytes) -> tuple[dict[str, Any], bytes]:
    """Parse one raw response frame, returning it with its exact terminator.

    The raw-bytes half of the handshake harness: a text-mode pipe would apply
    universal newlines and rewrite ``\\r\\n`` to ``\\n``, hiding the framing
    difference this exists to observe. Raises on a frame that ends any other
    way (no terminator at all, or a bare ``\\r``), because those are exactly
    the corruptions a strict NDJSON client chokes on.
    """
    assert line, "server closed the connection before answering"
    assert line.endswith(b"\n"), f"frame does not end with a line feed: {line[-8:]!r}"
    terminator = b"\r\n" if line.endswith(b"\r\n") else b"\n"
    return json.loads(line[: -len(terminator)]), terminator


def _spawn_raw(cmd: list[str], db: Path) -> subprocess.Popen[bytes]:
    """Start the server with binary pipes, the way a strict NDJSON client would.

    The environment is a copy of the parent's (Windows children denied
    ``SystemRoot`` die during interpreter startup, issue #211) with the
    database steered through ``CONTINUUM_DB``, exactly as ``_spawn`` does;
    only the pipe decoding differs.
    """
    env = dict(os.environ)
    env["CONTINUUM_DB"] = str(db)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def test_response_framing_is_pinned_on_the_raw_wire(tmp_path: Any) -> None:
    """Every response frame ends ``\\r\\n`` on Windows, ``\\n`` elsewhere (#839).

    On Windows the MCP SDK terminates stdio frames with CRLF
    (modelcontextprotocol/python-sdk#2433): Claude Code absorbs it, strict
    NDJSON clients reject every frame, and the failure only shows on that
    platform. This pin is what turns that from a user report into a CI
    failure: if upstream fixes the defect, the assert flips and the docs
    follow; if our own side regresses, it says so here rather than in the
    field. The module form is spawned because the framing belongs to the
    SDK's writer, not the entry point that reached it.
    """
    proc = _spawn_raw([sys.executable, "-u", "-m", "continuum.mcp"], tmp_path / "framing.db")
    try:
        assert proc.stdin is not None and proc.stdout is not None
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "framing-test", "version": "0"},
            },
        }
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        proc.stdin.flush()
        reply, first_terminator = _parse_raw_frame(proc.stdout.readline())
        assert reply["result"]["serverInfo"]["name"] == "continuum-mcp", reply

        proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        proc.stdin.flush()
        proc.stdin.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
        proc.stdin.flush()
        listed, second_terminator = _parse_raw_frame(proc.stdout.readline())
        assert len(listed["result"]["tools"]) == TOOL_COUNT

        assert first_terminator == second_terminator, "framing changed mid-session"
        expected = b"\r\n" if sys.platform == "win32" else b"\n"
        assert first_terminator == expected, (
            f"frames end {first_terminator!r} but the documented contract on "
            f"{sys.platform} is {expected!r}; if the SDK fixed #2433 on Windows, "
            "update this pin and docs/api/mcp.md together"
        )
    finally:
        _stop(proc)


@pytest.mark.parametrize("terminator", [b"\n", b"\r\n"])
def test_frame_parsing_tolerates_both_terminators(terminator: bytes) -> None:
    """Our client-side reader must accept the frame under either terminator.

    Tolerating both is the whole contract (#839): the server emits one or the
    other depending on platform, and the reader a host or test drives has to
    parse both rather than assume the platform it was written on.
    """
    line = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}' + terminator
    parsed, observed = _parse_raw_frame(line)
    assert parsed == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert observed == terminator


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
