"""The benchmark runner's CLI surface (issue #682).

``benchmarks/run.py`` used to parse no arguments at all: ``--help`` ran the
full multi-minute suite instead of printing usage. These tests pin the
contract that questions get answered before any work starts.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

_RUNNER = pathlib.Path(__file__).resolve().parent.parent / "benchmarks" / "run.py"


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own repo's runner, fixed argv
        [sys.executable, str(_RUNNER), *argv],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_help_prints_usage_and_exits_zero_without_running() -> None:
    completed = _run("--help")
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.lower()
    # Help must describe what it runs and where it writes, not just the flags.
    assert "benchmarks/out/" in completed.stdout
    assert "fault-injection" in completed.stdout


def test_list_names_the_suites_and_runs_nothing() -> None:
    completed = _run("--list")
    assert completed.returncode == 0, completed.stderr
    for suite in ("phase6", "continuum-bench", "fault-injection", "horizon"):
        assert suite in completed.stdout, f"--list must name {suite}"


def test_unknown_flags_fail_fast_with_exit_two() -> None:
    completed = _run("--bogus")
    assert completed.returncode == 2
    assert "unrecognized arguments" in completed.stderr
