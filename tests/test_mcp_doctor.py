"""``continuum mcp doctor`` — every ``CONNECTION_CLOSED`` cause, named (issue #835).

The host shows one opaque string when the MCP server cannot connect, and the
real causes produce identical client output while the server's useful stderr
never reaches the user. The doctor's contract is that each failure class is
reproduced and *named*, with the fix attached, and that its exit code is safe
to script: zero only when nothing failed.

What is pinned here:

1. a healthy install completes a real handshake against the real command and
   reports every tool;
2. the #697 state (entry point without the ``mcp`` extra) is reproduced in a
   fresh subprocess via a ``sitecustomize`` import blocker — the same
   technique as ``tests/test_mcp_server.py``'s missing-extra test — and the
   diagnosis carries the server's own stderr remediation, the line the host
   never shows;
3. the #699 state (executable exists but a fresh process cannot find it on
   PATH) fails with the ``mcp install`` remedy;
4. the interpreter fallback is a warning, not a failure — the server works,
   the operator should just know how it is being reached;
5. on Windows, the SDK's CRLF framing (modelcontextprotocol/python-sdk#2433)
   is detected on the wire, because text-mode pipes hide it.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from continuum.cli import ExitCode, main
from continuum.mcp.doctor import run_doctor

#: Twelve tools: three read-only, nine mutating (``docs/api/mcp.md``).
TOOL_COUNT = 12

# Installable via PYTHONPATH: a sitecustomize that makes ``import mcp`` fail in
# any freshly spawned interpreter, reproducing #697 (entry point shipped, extra
# not installed) without uninstalling anything. Same blocker as
# tests/test_mcp_server.py's _WITHOUT_MCP_SDK, applied at interpreter startup
# so the doctor's *subprocess* checks see it — which is the point: the checks
# must diagnose the state a host's spawn would actually hit.
_SITECUSTOMIZE_BLOCKER = """
import sys


class _BlockMCP:
    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None


sys.meta_path.insert(0, _BlockMCP())
"""


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _checks(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in payload["checks"]}


def test_healthy_install_reports_the_full_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working install: every check runs, the handshake lists every tool,
    nothing is created in the project, and the exit code says healthy."""
    monkeypatch.chdir(tmp_path)
    code, out, err = run("--json", "mcp", "doctor")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["ok"] is True
    names = set(_checks(payload))
    assert {"mcp sdk", "entry point", "resolution", "handshake"} <= names
    tools = payload["handshake"]["tools"]
    assert len(tools) == TOOL_COUNT, tools
    assert payload["handshake"]["server"]["name"] == "continuum-mcp"
    # Read-only: a diagnostic must not create the very database whose
    # presence the CLI treats as state.
    assert not (tmp_path / "continuum.db").exists()
    if sys.platform == "win32":
        # Detected on the wire, not assumed from the platform: the frames the
        # server actually sent ended with \r\n (python-sdk#2433).
        assert payload["handshake"]["crlf"] is True
        assert _checks(payload)["framing"]["status"] == "warn"


def test_missing_extra_is_named_with_the_fix_and_the_servers_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #697 state, end to end: the SDK check fails in a fresh interpreter,
    the handshake probe fails against the server that cannot start, and — the
    part the host never shows — the server's own remediation line is carried
    into the diagnosis."""
    blocker_dir = tmp_path / "no-mcp-extra"
    blocker_dir.mkdir()
    (blocker_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE_BLOCKER, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH")
    combined = f"{blocker_dir}{os.pathsep}{existing}" if existing else str(blocker_dir)
    monkeypatch.setenv("PYTHONPATH", combined)
    monkeypatch.chdir(tmp_path)

    code, out, _ = run("--json", "mcp", "doctor")
    assert code == ExitCode.ERROR
    payload = json.loads(out)
    assert payload["ok"] is False
    checks = _checks(payload)
    assert checks["mcp sdk"]["status"] == "fail"
    assert "continuum-agent[mcp]" in checks["mcp sdk"]["fix"]
    assert checks["handshake"]["status"] == "fail"
    # The server's own stderr, which a host swallows into CONNECTION_CLOSED.
    assert "continuum-agent[mcp]" in checks["handshake"]["detail"]


def test_exe_off_path_is_a_failure_with_the_install_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #699 state: the executable exists but no fresh process can find it
    on PATH, so a bare-name host config is a CONNECTION_CLOSED waiting to
    happen — even though spawning the absolute path would work. The doctor
    must not call this healthy."""
    fake_exe = tmp_path / ("continuum-mcp.exe" if sys.platform == "win32" else "continuum-mcp")
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr("continuum.mcp.doctor._which_in_fresh_process", lambda: None)
    monkeypatch.setattr("continuum.mcp.doctor.resolve_server_command", lambda: [str(fake_exe)])

    report = run_doctor()
    assert report["ok"] is False
    checks = _checks(report)
    assert checks["entry point"]["status"] == "fail"
    assert "not on PATH" in checks["entry point"]["detail"]
    assert "mcp install" in checks["entry point"]["fix"]


def test_interpreter_fallback_is_a_warning_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No executable anywhere (a source checkout, an unactivated venv): the
    interpreter form carries the server, the handshake proves it, and the exit
    code stays zero — the operator is told *how* the server is reachable, not
    that it is broken."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("continuum.mcp.doctor._which_in_fresh_process", lambda: None)
    monkeypatch.setattr(
        "continuum.mcp.doctor.resolve_server_command",
        lambda: [sys.executable, "-u", "-m", "continuum.mcp"],
    )

    code, out, err = run("--json", "mcp", "doctor")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["ok"] is True
    checks = _checks(payload)
    assert checks["entry point"]["status"] == "warn"
    assert "interpreter form" in checks["entry point"]["detail"]


def test_handshake_failure_surfaces_the_child_stderr() -> None:
    """A server that dies pre-handshake with a message on stderr: the message
    is the diagnosis, and it reaches the report."""
    dying = [sys.executable, "-c", "import sys; sys.stderr.write('kaboom'); sys.exit(1)"]
    report = run_doctor(command=dying)
    assert report["ok"] is False
    assert "kaboom" in _checks(report)["handshake"]["detail"]


def test_report_names_the_command_it_diagnosed() -> None:
    """The resolution is part of the report, so the operator can compare what
    the doctor probed against what their host config says."""
    command = [sys.executable, "-u", "-m", "continuum.mcp"]
    report = run_doctor(command=command)
    assert report["command"] == command
