"""The doctor has to be right about three states that look alike from outside.

A registration that is present but stale, one that is present and authorized,
and one that is present but silently read-only all produce a working-looking
IDE. Only one of them can record anything. These tests pin each state to a
distinct verdict, and pin the exit-code contract that falls out of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.agents import diagnose
from continuum.cli.exitcodes import ExitCode
from continuum.mcp.authz import POLICY_ENV_VAR_ALIAS, UNKNOWN_CALLER
from continuum.mcp.install import HOST_PROFILES, SERVER_NAME


def doctor_exit_code(report: diagnose.DoctorReport) -> int:
    """The mapping the CLI applies, restated so the contract is testable."""

    if report.healthy:
        return ExitCode.OK
    if report.degraded:
        return ExitCode.REQUIRES_HUMAN
    if report.stale:
        return ExitCode.REQUIRES_REPAIR
    return ExitCode.UNSAFE


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    """An empty project root and home, so nothing leaks in from the real machine."""

    root = tmp_path / "project"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    return root, home


def write_mcp(path: Path, *, command: str, clients: str | None) -> Path:
    """Write a project-scope MCP registration in the shape install writes."""

    entry: dict[str, object] = {"type": "stdio", "command": command, "args": ["--db", "/tmp/x.db"]}
    if clients is not None:
        entry["env"] = {POLICY_ENV_VAR_ALIAS: clients}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2),
        encoding="utf-8",
    )
    return path


def live_command() -> str:
    import shutil

    found = shutil.which("continuum-mcp")
    assert found, "the continuum-mcp console script must be on PATH for this test"
    return found


def scan(env: tuple[Path, Path], **kwargs: object) -> diagnose.DoctorReport:
    root, home = env
    return diagnose.scan(root=root, home=home, **kwargs)  # type: ignore[arg-type]


def ide_for(report: diagnose.DoctorReport, ide: str, surface: str) -> diagnose.IdeReport:
    matches = [i for i in report.ides if i.ide == ide and i.surface == surface]
    assert matches, f"no {surface} report for {ide}"
    return matches[0]


def test_wired_current_authorized_ide_reports_healthy(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registration that resolves and grants its client is simply healthy."""

    root, _ = env
    write_mcp(root / ".mcp.json", command=live_command(), clients="claude-code")
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)

    report = scan(env)
    row = ide_for(report, "claude-code", "mcp")
    assert row.wired is True
    assert row.stale is False
    assert row.degraded is False
    assert row.state == diagnose.WIRED
    assert doctor_exit_code(report) == ExitCode.OK
    assert report.healthy is True


def test_stale_baked_path_is_reported_stale(env: tuple[Path, Path]) -> None:
    """A virtualenv that has been deleted is the classic stale registration."""

    root, _ = env
    write_mcp(root / ".mcp.json", command="/nonexistent/venv/bin/continuum-mcp", clients="claude-code")

    report = scan(env)
    row = ide_for(report, "claude-code", "mcp")
    assert row.wired is True
    assert row.stale is True
    assert row.state == diagnose.STALE
    assert any("/nonexistent/venv/bin/continuum-mcp" in d for d in row.details)
    assert "continuum mcp install --host claude-code" in (row.remedy or "")
    assert doctor_exit_code(report) == ExitCode.REQUIRES_REPAIR
    assert report.healthy is False


def test_read_only_degraded_client_is_reported_degraded_naming_both_strings(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent failure: wired, resolvable, and unable to record anything.

    Both strings matter. The name the host expects to be permitted
    (``claude-code``), and the set the policy actually grants (nothing). A
    report that said only "degraded" would leave the operator guessing.
    """

    root, _ = env
    write_mcp(root / ".mcp.json", command=live_command(), clients=None)
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)

    report = scan(env)
    row = ide_for(report, "claude-code", "mcp")
    assert row.wired is True
    assert row.stale is False
    assert row.degraded is True
    assert row.state == diagnose.DEGRADED
    detail = " ".join(row.details)
    assert "claude-code" in detail, "the expected client name must be named"
    assert UNKNOWN_CALLER in detail, "the empty grant must be named"
    assert doctor_exit_code(report) == ExitCode.REQUIRES_HUMAN


def test_policy_file_granting_the_client_clears_degraded(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix must actually clear the verdict, or the report is not actionable."""

    root, _ = env
    write_mcp(root / ".mcp.json", command=live_command(), clients=None)
    policy = root / ".continuum" / "mcp-policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps(["claude-code"]), encoding="utf-8")
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)

    row = ide_for(scan(env), "claude-code", "mcp")
    assert row.degraded is False
    assert row.state == diagnose.WIRED


def test_unconfigured_ide_is_reported_plainly_not_as_failure(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IDE nobody wired is the normal state of a machine, not a defect."""

    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)
    report = scan(env)

    row = ide_for(report, "cursor", "mcp")
    assert row.wired is False
    assert row.state == diagnose.NOT_CONFIGURED
    assert "nothing configured" in row.summary
    assert row.remedy is None
    assert report.healthy is True
    assert doctor_exit_code(report) == ExitCode.OK


def test_stale_and_degraded_together_name_both(env: tuple[Path, Path], monkeypatch) -> None:
    """Both faults at once must not collapse into whichever check ran last."""

    root, _ = env
    write_mcp(root / ".mcp.json", command="/gone/continuum-mcp", clients=None)
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)

    report = scan(env)
    row = ide_for(report, "claude-code", "mcp")
    assert row.stale and row.degraded
    assert row.state == f"{diagnose.STALE}+{diagnose.DEGRADED}"
    # Degradation outranks staleness for the exit code: re-running the install
    # repairs the path but does not grant the permission, so a pipeline that
    # only retried on REQUIRES_REPAIR would loop forever.
    assert doctor_exit_code(report) == ExitCode.REQUIRES_HUMAN


def test_every_known_host_and_client_is_covered(env: tuple[Path, Path]) -> None:
    """A host nobody checked is a host this command claims about and misses."""

    report = scan(env)
    for host in HOST_PROFILES:
        assert ide_for(report, host, "mcp").ide == host
    from continuum.clienthooks import CLIENT_PROFILES

    for client in CLIENT_PROFILES:
        assert ide_for(report, client, "hooks").ide == client


def test_hook_client_wired_and_current(env: tuple[Path, Path]) -> None:
    """Hook wiring is reported on its own surface, not folded into mcp."""

    root, _ = env
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "continuum observe"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    row = ide_for(scan(env), "claude-code", "hooks")
    assert row.wired is True
    assert row.state == diagnose.WIRED


def test_hook_client_with_dead_command_is_stale(env: tuple[Path, Path]) -> None:
    root, _ = env
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": "/gone/venv/bin/continuum observe"}
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    row = ide_for(scan(env), "claude-code", "hooks")
    assert row.stale is True
    assert "/gone/venv/bin/continuum" in row.summary


def test_instruction_target_drift_shows_in_the_doctor(env: tuple[Path, Path], tmp_path: Path) -> None:
    """The doctor covers the instruction surface too, not only the wired ones."""

    from continuum.agents import generator

    root, _ = env
    generator.install("claude", root=root)
    assert ide_for(scan(env), "claude", "instructions").state == diagnose.CURRENT

    (root / "CLAUDE.md").write_text(
        (root / "CLAUDE.md").read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8"
    )
    row = ide_for(scan(env), "claude", "instructions")
    assert row.state == "drifted", "an edited body is drift, not a dead path"
    assert row.remedy is not None


def test_json_form_matches_the_human_form(env: tuple[Path, Path]) -> None:
    """--json is a documented contract, so both forms must agree state for state."""

    root, _ = env
    write_mcp(root / ".mcp.json", command="/gone/continuum-mcp", clients=None)

    report = scan(env)
    payload = report.to_dict()
    assert payload["healthy"] == report.healthy
    assert payload["stale"] == [i.ide for i in report.stale]
    assert payload["degraded"] == [i.ide for i in report.degraded]

    by_key = {(row["ide"], row["surface"]): row for row in payload["ides"]}
    for ide in report.ides:
        row = by_key[(ide.ide, ide.surface)]
        assert row["state"] == ide.state
        assert row["wired"] == ide.wired
        assert row["stale"] == ide.stale
        assert row["degraded"] == ide.degraded

    text = diagnose.render_report(report)
    for ide in report.ides:
        assert ide.ide in text
    verdict = [line for line in text.splitlines() if line.startswith(("healthy:", "not healthy:"))]
    assert len(verdict) == 1, text
    assert verdict[0].startswith("healthy:") is report.healthy


def test_unparseable_config_is_reported_not_fatal(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One IDE with a stray comma must not take the whole report down."""

    root, _ = env
    (root / ".mcp.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)

    report = scan(env)
    row = ide_for(report, "claude-code", "mcp")
    assert row.state == diagnose.NOT_CONFIGURED
    assert any("could not be parsed" in d for d in row.details)


def test_toml_config_is_scanned(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Format support is data, not code: toml is read without a new branch.

    The profile is pointed at a toml file because that is the shape a toml
    host will have once the format-adapter track adds one; today no host uses
    toml, so without pointing a profile at one the reader would be untested.
    """

    root, _ = env
    monkeypatch.setitem(
        HOST_PROFILES,
        "claude-code",
        {
            "project_settings": "config.toml",
            "local_settings": "config.toml",
            "user_settings": "config.toml",
            "local_projects_key": "mcpServers",
            "mutating_clients": "claude-code",
        },
    )
    (root / "config.toml").write_text(
        f'[mcpServers."{SERVER_NAME}"]\ncommand = "{live_command()}"\n'
        f'env.CONTINUUM_MCP_MUTATING_CLIENTS = "claude-code"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)

    report = diagnose.scan(root=root, home=env[1])
    row = ide_for(report, "claude-code", "mcp")
    assert row.state == diagnose.WIRED, row.details


def test_config_reader_handles_each_format(tmp_path: Path) -> None:
    """The reader covers every format the profiles use, via the format layer."""

    (tmp_path / "a.json").write_text('{"k": 1}', encoding="utf-8")
    (tmp_path / "a.toml").write_text("[t]\nk = 1\n", encoding="utf-8")
    (tmp_path / "a.yaml").write_text("k: 1\n", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "a.md").write_text("# not a config", encoding="utf-8")

    assert diagnose._read_config(tmp_path / "a.json") == {"k": 1}
    assert diagnose._read_config(tmp_path / "a.toml") == {"t": {"k": 1}}
    assert diagnose._read_config(tmp_path / "a.yaml") == {"k": 1}
    assert diagnose._read_config(tmp_path / "broken.json") is None
    assert diagnose._read_config(tmp_path / "a.md") is None
    assert diagnose._read_config(tmp_path / "missing.json") is None


def test_a_yaml_host_is_read_not_reported_unconfigured(env: tuple[Path, Path]) -> None:
    """The regression this whole coupling is about.

    ``continue`` registers at ``.continue/config.yaml``. A reader that parses
    only json and toml returns nothing for it, so the doctor calls a
    configured IDE ``not-configured``. That is a wrong answer, not a missing
    one, and it would survive every test that only exercises json hosts.
    """

    root, _ = env
    from continuum.mcp.install import HOST_PROFILES

    yaml_hosts = [
        host
        for host, profile in HOST_PROFILES.items()
        if any(str(v).endswith((".yaml", ".yml")) for k, v in profile.items() if k.endswith("settings"))
    ]
    assert yaml_hosts, "no yaml host profile exists, so this guard would pass vacuously"

    host = yaml_hosts[0]
    profile = HOST_PROFILES[host]
    config = root / profile["project_settings"]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'mcpServers:\n  "{SERVER_NAME}":\n    command: "{live_command()}"\n'
        f'    env:\n      {POLICY_ENV_VAR_ALIAS}: "{profile["mutating_clients"]}"\n',
        encoding="utf-8",
    )

    row = ide_for(scan(env), host, "mcp")
    assert row.wired is True, f"{host} reads YAML and must be seen as wired: {row.details}"
    assert row.state == diagnose.WIRED, row.details


def test_local_scope_registration_is_found(env: tuple[Path, Path], monkeypatch) -> None:
    """Local scope nests under projects/<abs path>; a scan that misses it lies."""

    root, home = env
    settings = home / ".claude.json"
    settings.write_text(
        json.dumps(
            {
                "projects": {
                    str(root): {
                        "mcpServers": {
                            SERVER_NAME: {
                                "type": "stdio",
                                "command": live_command(),
                                "args": ["--db", "/tmp/x.db"],
                                "env": {POLICY_ENV_VAR_ALIAS: "claude-code"},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(POLICY_ENV_VAR_ALIAS, raising=False)
    monkeypatch.delenv("CONTINUUM_MCP_ALLOW", raising=False)
    assert ide_for(scan(env), "claude-code", "mcp").state == diagnose.WIRED


def test_deep_scan_folds_in_the_server_verdict(env: tuple[Path, Path]) -> None:
    """--deep consumes mcp.doctor rather than approximating its handshake."""

    report = scan(env, deep=True, timeout=1.0)
    assert report.server is not None
    assert "checks" in report.server
    # The folded verdict must actually gate health, or --deep is decoration.
    assert report.healthy == report._server_healthy


def test_health_never_reports_ok_when_unmapped(env: tuple[Path, Path]) -> None:
    """The fail-closed rule: anything unknown is not OK."""

    report = scan(env)
    for ide in report.ides:
        assert ide.state in (
            diagnose.WIRED,
            diagnose.STALE,
            diagnose.DEGRADED,
            diagnose.NOT_CONFIGURED,
            diagnose.CURRENT,
            "unmanaged",
        ), f"unmapped state {ide.state!r} would be treated as unknown"
    assert isinstance(report.healthy, bool)
