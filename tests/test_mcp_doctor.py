"""Tests for ``continuum mcp doctor`` (issue #835).

The doctor exists for the three reproduced failure states named in the
issue: the ``mcp`` extra missing, the console script not on the host's
PATH, and a healthy install. Each state is reproduced for real -- a fake
``mcp`` package shadowing the real one on ``PYTHONPATH`` for the first, a
stripped PATH for the second -- never by mocking the checks, because the
doctor's whole value is that it observes spawned processes the way a host
does.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from continuum.cli import ExitCode, main
from continuum.mcp.doctor import render_doctor, run_doctor

#: The venv's script directory, prepended to PATH by tests that need the
#: console script resolvable so the healthy-path assertions hold even when
#: pytest runs without the environment's bin dir on PATH.
SCRIPTS_DIR = Path(sys.executable).resolve().parent


def _with_scripts_dir_on_path() -> str:
    """A PATH that definitely contains this interpreter's console scripts."""
    return os.pathsep.join([str(SCRIPTS_DIR), os.environ.get("PATH", "")])


def _fake_missing_mcp(tmp_path: Path) -> Path:
    """A directory whose ``mcp`` package raises on import, like a missing extra.

    PYTHONPATH entries precede site-packages, so every freshly spawned
    process (the doctor's probes, and the server child itself) sees this
    shadow instead of the real SDK. That is the same goal as the
    ``sys.meta_path`` blocker in ``tests/test_mcp_server.py``, but it works
    across process boundaries, which is what the doctor's fresh-subprocess
    probes require.
    """
    package = tmp_path / "shadow" / "mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'raise ModuleNotFoundError("No module named \'mcp\'", name="mcp")\n',
        encoding="utf-8",
    )
    return package.parent


def _by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the report's checks by name."""
    return {check["check"]: check for check in report["checks"]}


def test_healthy_install_completes_a_real_handshake(monkeypatch: Any) -> None:
    """The acceptance case: doctor exits 0 and reports all tools on a good install.

    PATH is pinned to include the interpreter's script dir so the assertion
    holds wherever pytest runs; the handshake is still real -- a spawned
    server answering ``initialize`` and ``tools/list`` over stdio.
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    report = run_doctor()

    assert report["healthy"] is True
    checks = _by_name(report)
    assert checks["sdk-import"]["status"] == "pass"
    assert checks["command-resolution"]["status"] == "pass"
    handshake = checks["handshake"]
    assert handshake["status"] == "pass"
    assert handshake["server"] == "continuum-mcp"
    # "reports all tools": the count matches what the docs table guards.
    assert len(handshake["tools"]) >= 12
    assert "continuum_record_progress" in handshake["tools"]


def test_missing_extra_names_the_cause_and_the_fix(monkeypatch: Any, tmp_path: Path) -> None:
    """A missing SDK must be diagnosed, not reported as a bare CONNECTION_CLOSED.

    The handshake failure must carry the server's own stderr tail, because
    that message ("install it with: pip install continuum-agent[mcp]") is
    the one thing the host never shows the user (issue #87/#93, #697).
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(_fake_missing_mcp(tmp_path)), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )

    report = run_doctor()

    assert report["healthy"] is False
    checks = _by_name(report)
    assert checks["sdk-import"]["status"] == "fail"
    assert "pip install continuum-agent[mcp]" in checks["sdk-import"]["fix"]
    handshake = checks["handshake"]
    assert handshake["status"] == "fail"
    assert "continuum-agent[mcp]" in handshake["detail"], (
        "the child's stderr names the fix; the doctor must surface it"
    )


def test_exe_not_on_host_path_is_a_failure_state(monkeypatch: Any, tmp_path: Path) -> None:
    """A script the host cannot spawn is a failure even when the code works.

    The diagnosis must name the scripts directory that is missing from PATH,
    because that directory is the fix. The handshake still runs against the
    ``python -m continuum.mcp`` fallback: proving the module form works is
    what separates "broken install" from "broken PATH".
    """
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    (tmp_path / "empty-path").mkdir()

    report = run_doctor()

    assert report["healthy"] is False
    checks = _by_name(report)
    resolution = checks["command-resolution"]
    assert resolution["status"] == "fail"
    assert "not on the PATH" in resolution["detail"]
    assert str(SCRIPTS_DIR) in resolution["fix"], "the fix names the directory to add"
    # The fallback handshake succeeding is the useful extra fact, not a pass.
    assert checks["handshake"]["status"] == "pass"
    assert checks["handshake"]["command"] == [sys.executable, "-u", "-m", "continuum.mcp"]


def test_the_probe_reports_the_wire_framing_it_observed(monkeypatch: Any) -> None:
    """Framing is reported as observed, not assumed by platform.

    The CRLF bug is upstream and Windows-only today (#839); a doctor that
    guessed "CRLF on win32" would go stale the day the SDK fixes it.
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    report = run_doctor()

    framing = _by_name(report)["wire-framing"]
    assert framing["status"] == "info"
    assert framing["detail"].startswith("response frames end with ")
    # A framing note must never flip the verdict by itself.
    assert report["healthy"] is True


def test_cli_json_output_and_exit_codes(monkeypatch: Any, tmp_path: Path) -> None:
    """``--json`` emits the machine-readable report; exit codes follow the verdict.

    The command must also not create a database as a side effect: it probes
    with a throwaway one, and the cwd must stay clean wherever it runs.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    out = io.StringIO()
    code = main(["--json", "mcp", "doctor"], out=out, err=io.StringIO())

    assert code == ExitCode.OK
    payload = json.loads(out.getvalue())
    assert payload["healthy"] is True
    assert {check["check"] for check in payload["checks"]} >= {
        "sdk-import",
        "command-resolution",
        "handshake",
    }
    assert not list(tmp_path.glob("*.db")), "doctor must not create a database in cwd"

    # And the rendered text names the check and its fix on the failure path.
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(_fake_missing_mcp(tmp_path)), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )
    out = io.StringIO()
    code = main(["mcp", "doctor"], out=out, err=io.StringIO())

    assert code == ExitCode.ERROR
    assert "pip install continuum-agent[mcp]" in out.getvalue()


def test_render_doctor_is_one_line_per_finding(monkeypatch: Any, tmp_path: Path) -> None:
    """Every check renders exactly one actionable line, plus its fix when failing.

    The issue's wording is "one actionable line per finding"; a check whose
    finding never reaches the output is a diagnosis the user cannot see.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    (tmp_path / "empty-path").mkdir()

    text = render_doctor(run_doctor())

    assert "[fail] command-resolution:" in text
    assert "fix:" in text
    assert "[ok]   handshake:" in text  # module fallback still works
    assert "not healthy" in text
    assert "remedies:" in text
