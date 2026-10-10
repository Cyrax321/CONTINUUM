"""The command surface: argument wiring, exit codes, and the JSON contract.

The library tests pin what the scan concludes. These pin what a shell gets:
which status code comes back, what lands on stdout, and that ``--json`` says
the same thing as the prose. An exit code is the part automation reads, so it
is the part worth testing hardest.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from continuum.cli.exitcodes import ExitCode
from continuum.cli.main import main as cli_main
from continuum.mcp.authz import POLICY_ENV_VAR_ALIAS
from continuum.mcp.install import SERVER_NAME


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run every case in an empty project with a clean policy environment."""

    root = tmp_path / "project"
    (tmp_path / "home").mkdir()
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)
    return root


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """An empty home, so no case can be decided by the developer's own config."""

    home = tmp_path / "fake-home"
    home.mkdir(exist_ok=True)
    return home


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli_main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def run_doctor(*extra: str, home: Path | None = None) -> tuple[int, str, str]:
    """Invoke the doctor against an isolated home.

    The doctor resolves ``~`` against the real home by default, which is
    correct for a person and wrong for a test: without this, a developer
    machine's own ``~/.claude.json`` decides whether a case passes.
    """

    assert home is not None, "pass an isolated home so the scan cannot read the real one"
    return run("doctor", "--home", str(home), *extra)


def write_mcp(root: Path, *, command: str, clients: str | None) -> None:
    entry: dict[str, object] = {"type": "stdio", "command": command, "args": ["--db", "/tmp/x.db"]}
    if clients is not None:
        entry["env"] = {POLICY_ENV_VAR_ALIAS: clients}
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {SERVER_NAME: entry}}), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #


def test_agents_install_writes_the_named_target(isolated: Path) -> None:
    code, out, _ = run("agents", "install", "--target", "cursor")
    assert code == ExitCode.OK
    assert "[installed] .cursor/rules/continuum.mdc" in out
    assert (isolated / ".cursor" / "rules" / "continuum.mdc").is_file()


def test_agents_install_refuses_a_hand_written_file(isolated: Path) -> None:
    (isolated / "AGENTS.md").write_text("# ours\n", encoding="utf-8")
    code, out, _ = run("agents", "install", "--target", "agents")
    assert code == ExitCode.ERROR
    assert "refused" in out
    assert (isolated / "AGENTS.md").read_text(encoding="utf-8") == "# ours\n"


def test_agents_install_is_idempotent(isolated: Path) -> None:
    run("agents", "install", "--target", "windsurf")
    first = (isolated / ".windsurfrules").read_bytes()
    code, out, _ = run("agents", "install", "--target", "windsurf")
    assert code == ExitCode.OK
    assert "[present]" in out
    assert (isolated / ".windsurfrules").read_bytes() == first


def test_agents_install_all_covers_every_registered_target(isolated: Path) -> None:
    from continuum.agents import TARGETS

    code, _, _ = run("agents", "install", "--all")
    assert code == ExitCode.OK
    for target in TARGETS.values():
        assert (isolated / target.path).is_file(), target.path


def test_agents_check_fails_on_a_drifted_copy(isolated: Path) -> None:
    run("agents", "install", "--target", "junie")
    target = isolated / ".junie" / "guidelines.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    code, out, _ = run("agents", "check", "--target", "junie")
    assert code == ExitCode.CORRUPTED, "a drifted committed copy must fail the check"
    assert "drifted" in out


def test_agents_check_passes_on_a_fresh_render(isolated: Path) -> None:
    run("agents", "install", "--target", "junie")
    code, out, _ = run("agents", "check", "--target", "junie")
    assert code == ExitCode.OK
    assert "current" in out


def test_agents_check_reports_an_unmanaged_file_without_failing(isolated: Path) -> None:
    """A conflict is not corruption; nothing this command owns is broken."""

    (isolated / "AGENTS.md").write_text("# ours\n", encoding="utf-8")
    code, out, _ = run("agents", "check", "--target", "agents")
    assert code == ExitCode.OK
    assert "unmanaged" in out


def test_agents_remove_takes_only_what_it_wrote(isolated: Path) -> None:
    run("agents", "install", "--target", "windsurf")
    (isolated / "AGENTS.md").write_text("# ours\n", encoding="utf-8")

    code, out, _ = run("agents", "remove", "--target", "windsurf")
    assert code == ExitCode.OK
    assert not (isolated / ".windsurfrules").exists()

    code, out, _ = run("agents", "remove", "--target", "agents")
    assert code == ExitCode.OK
    assert "left alone" in out
    assert (isolated / "AGENTS.md").exists()


def test_agents_list_names_every_target(isolated: Path) -> None:
    code, out, _ = run("agents", "list")
    assert code == ExitCode.OK
    assert ".cursor/rules/continuum.mdc" in out
    assert ".github/copilot-instructions.md" in out


def test_agents_rejects_an_unknown_target(isolated: Path) -> None:
    with pytest.raises(SystemExit):
        run("agents", "install", "--target", "not-an-ide")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def live_command() -> str:
    import shutil

    found = shutil.which("continuum-mcp")
    assert found, "the continuum-mcp console script must be on PATH for this test"
    return found


def test_doctor_is_ok_when_nothing_is_broken(isolated: Path, fake_home: Path) -> None:
    write_mcp(isolated, command=live_command(), clients="claude-code")
    code, out, _ = run_doctor(home=fake_home)
    assert code == ExitCode.OK, out
    assert "healthy:" in out


def test_doctor_reports_stale_as_requires_repair(isolated: Path, fake_home: Path) -> None:
    write_mcp(isolated, command="/gone/venv/bin/continuum-mcp", clients="claude-code")
    code, out, _ = run_doctor(home=fake_home)
    assert code == ExitCode.REQUIRES_REPAIR, out
    assert "stale" in out


def test_doctor_reports_degraded_as_requires_human(isolated: Path, fake_home: Path) -> None:
    """The silent failure gets its own code, distinct from a stale path."""

    write_mcp(isolated, command=live_command(), clients=None)
    code, out, _ = run_doctor(home=fake_home)
    assert code == ExitCode.REQUIRES_HUMAN, out
    assert "read-only-degraded" in out


def test_doctor_json_agrees_with_the_prose(isolated: Path, fake_home: Path) -> None:
    """--json is a documented contract: the two forms must not disagree."""

    write_mcp(isolated, command="/gone/venv/bin/continuum-mcp", clients=None)

    _, prose, _ = run_doctor(home=fake_home)
    code, raw, _ = run_doctor("--json", home=fake_home)
    payload = json.loads(raw)

    assert payload["healthy"] is False
    # Several host profiles share one project file (.mcp.json is read by both
    # claude-code and gemini), so membership is the meaningful assertion.
    assert "claude-code" in payload["stale"]
    assert "claude-code" in payload["degraded"]
    assert code == ExitCode.REQUIRES_HUMAN
    for row in payload["ides"]:
        assert row["ide"] in prose
        assert row["state"] in prose or row["state"] == "not-configured"


def test_doctor_never_creates_a_database(isolated: Path, fake_home: Path) -> None:
    """Checking whether a host is wired must not create an empty database."""

    run_doctor(home=fake_home)
    assert not (isolated / "continuum.db").exists()


def test_doctor_accepts_a_root_it_is_not_standing_in(
    isolated: Path, fake_home: Path, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    write_mcp(other, command=live_command(), clients="claude-code")
    code, _, _ = run("doctor", "--root", str(other), "--home", str(fake_home))
    assert code == ExitCode.OK


def test_help_lists_both_new_commands() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "continuum.cli", "--help"],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert "agents" in out.stdout
    assert "doctor" in out.stdout
