"""The doctor must not call a portable registration stale (issue #1619).

`.mcp.json` in this repository bakes the form `docs/api/mcp.md` tells people to
commit:

```json
"command": "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp"
```

`os.path.expandvars` does not understand the shell's `${VAR:-default}` syntax, so
that text used to reach `_resolves` unchanged, fail to be absolute, fall through
to a PATH lookup for a name called `${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp`,
and be reported stale. Two consequences, both wrong:

- a clean clone exited `REQUIRES_REPAIR` (10) on a checkout that is fine;
- the printed remedy, `re-run continuum mcp install`, would replace the portable
  committed expression with an absolute path tied to whichever machine ran it.

So these tests pin the third state: a command a host expands before spawning is
`unverified`, which is not stale, and does not drive the repair exit code. They
also pin that a genuinely broken absolute path still reads stale, since that is
the case the state has to keep catching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.agents import diagnose
from continuum.cli.exitcodes import ExitCode
from continuum.mcp.authz import POLICY_ENV_VAR_ALIAS
from continuum.mcp.install import SERVER_NAME

PORTABLE_COMMAND = "${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp"


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    """An empty project root and home, so nothing leaks in from the real machine."""

    root = tmp_path / "project"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    return root, home


def write_mcp(path: Path, command: str, *, clients: str = "claude-code") -> Path:
    entry: dict[str, object] = {
        "type": "stdio",
        "command": command,
        "args": ["--db", "continuum.db"],
        "env": {POLICY_ENV_VAR_ALIAS: clients},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2), encoding="utf-8")
    return path


def scan(env: tuple[Path, Path]) -> diagnose.DoctorReport:
    root, home = env
    return diagnose.scan(root=root, home=home)


def row_for(report: diagnose.DoctorReport, ide: str) -> diagnose.IdeReport:
    matches = [i for i in report.ides if i.ide == ide and i.surface == "mcp"]
    assert matches, f"no mcp report for {ide}"
    return matches[0]


def exit_code(report: diagnose.DoctorReport) -> int:
    """The mapping the CLI applies, restated so the contract is testable."""

    if report.healthy:
        return ExitCode.OK
    if report.degraded:
        return ExitCode.REQUIRES_HUMAN
    if report.stale:
        return ExitCode.REQUIRES_REPAIR
    return ExitCode.UNSAFE


# --- the regression --------------------------------------------------------- #


def test_the_committed_portable_command_is_not_stale(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    row = row_for(scan(env), "claude-code")

    assert row.stale is False


def test_the_committed_portable_command_is_wired(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    row = row_for(scan(env), "claude-code")

    assert row.wired is True
    assert row.state == diagnose.WIRED


def test_the_committed_portable_command_does_not_ask_for_repair(env: tuple[Path, Path]) -> None:
    """The whole point: a clean clone must not exit REQUIRES_REPAIR."""

    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    assert exit_code(scan(env)) == ExitCode.OK


def test_a_clean_clone_of_this_repository_is_healthy(tmp_path: Path) -> None:
    """Scans the real `.mcp.json` this repository commits."""

    repo = Path(__file__).resolve().parent.parent
    root = tmp_path / "clone"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (root / ".mcp.json").write_text(
        (repo / ".mcp.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    report = diagnose.scan(root=root, home=home)
    row = row_for(report, "claude-code")

    assert row.wired is True
    assert row.stale is False
    assert exit_code(report) == ExitCode.OK


def test_the_remedy_does_not_ask_for_a_re_bake(env: tuple[Path, Path]) -> None:
    """Re-baking is the harmful half of the old advice, so it must not be printed."""

    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    row = row_for(scan(env), "claude-code")

    assert row.remedy is None or "mcp install" not in row.remedy


def test_the_expression_is_reported_as_unverified(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    row = row_for(scan(env), "claude-code")

    assert len(row.unverified) == 1
    assert PORTABLE_COMMAND in row.unverified[0]


def test_the_detail_says_the_doctor_cannot_verify_it(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    row = row_for(scan(env), "claude-code")

    assert any("cannot verify" in detail for detail in row.details)


def test_the_summary_counts_the_unverified_paths(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    row = row_for(scan(env), "claude-code")

    assert "cannot verify" in row.summary
    assert "stale" not in row.summary


def test_unverified_is_serialised(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", PORTABLE_COMMAND)

    payload = row_for(scan(env), "claude-code").to_dict()

    assert len(payload["unverified"]) == 1
    assert payload["stale"] is False


# --- stale still means stale ------------------------------------------------ #


def test_a_deleted_venv_is_still_reported_stale(env: tuple[Path, Path]) -> None:
    """The state has to keep catching the case it was built for."""

    root, _ = env
    write_mcp(root / ".mcp.json", "/nonexistent/venv/bin/continuum-mcp")

    row = row_for(scan(env), "claude-code")

    assert row.stale is True
    assert row.state == diagnose.STALE


def test_a_deleted_venv_still_asks_for_repair(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", "/nonexistent/venv/bin/continuum-mcp")

    assert exit_code(scan(env)) == ExitCode.REQUIRES_REPAIR


def test_a_stale_path_still_prints_the_re_bake_remedy(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", "/nonexistent/venv/bin/continuum-mcp")

    row = row_for(scan(env), "claude-code")

    assert row.remedy is not None
    assert "mcp install" in row.remedy


def test_a_stale_path_is_not_listed_as_unverified(env: tuple[Path, Path]) -> None:
    root, _ = env
    write_mcp(root / ".mcp.json", "/nonexistent/venv/bin/continuum-mcp")

    row = row_for(scan(env), "claude-code")

    assert row.unverified == ()


# --- the shapes that decide unverified -------------------------------------- #


@pytest.mark.parametrize(
    ("command", "unverified"),
    [
        ("${CLAUDE_PROJECT_DIR:-.}/.venv/bin/continuum-mcp", True),
        ("${HOME}/.local/bin/continuum-mcp", True),
        ("$HOME/.local/bin/continuum-mcp", True),
        ("/nonexistent/venv/bin/continuum-mcp", False),
        ("continuum-mcp", False),
        ("python3", False),
    ],
)
def test_a_dollar_sign_is_what_marks_a_command_unverifiable(command: str, unverified: bool) -> None:
    assert diagnose._is_shell_expression(command) is unverified


def test_an_empty_command_is_not_a_shell_expression() -> None:
    assert diagnose._is_shell_expression("") is False


def test_a_windows_path_with_a_drive_letter_is_not_a_shell_expression() -> None:
    assert diagnose._is_shell_expression(r"C:\Python311\Scripts\continuum-mcp.exe") is False
