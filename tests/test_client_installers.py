"""Client installers beyond Claude Code (issue #209).

Gemini CLI and Codex CLI expose hook surfaces with the same stdin contract
(tool_name + tool_input JSON) but different settings layouts, event names
and matchers. Wiring is data-driven from CLIENT_PROFILES; these tests pin
each profile's installed shape, idempotency, removal, and the Codex feature
flag hint.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from continuum.cli import ExitCode, main
from continuum.clienthooks import CLIENT_PROFILES, install_client_hook


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


import io  # noqa: E402


@pytest.mark.parametrize("client", ("claude-code", "gemini", "codex"))
def test_install_writes_the_profiled_shape(tmp_path: Path, client: str) -> None:
    profile = CLIENT_PROFILES[client]
    settings = tmp_path / "settings.json"

    code, out, err = run("--json", "hooks", "install", client, "--settings", str(settings))
    assert code == ExitCode.OK, err

    data = json.loads(settings.read_text())
    hooks_obj = data["hooks"]
    post = hooks_obj[profile["post_event"]]
    assert len(post) == 1
    assert post[0]["matcher"] == profile["write_matcher"]
    command = post[0]["hooks"][0]["command"]
    assert command.split()[-1] == "observe"

    payload = json.loads(out)
    assert payload["hooks"][0]["event"] == profile["post_event"]
    assert payload["settings"] == str(settings)


@pytest.mark.parametrize("client", ("claude-code", "gemini", "codex"))
def test_install_is_idempotent_per_client(tmp_path: Path, client: str) -> None:
    settings = tmp_path / "settings.json"
    for _ in range(2):
        code, _, err = run("--json", "hooks", "install", client, "--settings", str(settings))
        assert code == ExitCode.OK, err
    data = json.loads(settings.read_text())
    profile = CLIENT_PROFILES[client]
    assert len(data["hooks"][profile["post_event"]]) == 1
    assert len(data["hooks"][profile["start_event"]]) == 1


def test_gemini_gate_uses_before_tool(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    code, out, _ = run(
        "--json", "hooks", "install", "gemini", "--with-gate", "--settings", str(settings)
    )
    assert code == ExitCode.OK
    data = json.loads(settings.read_text())
    assert data["hooks"]["AfterTool"][0]["matcher"] == "write_file|replace"
    before = data["hooks"]["BeforeTool"]
    assert before[0]["matcher"] == ".*"
    assert before[0]["hooks"][0]["command"].split()[-1] == "gate"


def test_remove_cleans_each_client(tmp_path: Path) -> None:
    for client in ("claude-code", "gemini", "codex"):
        settings = tmp_path / f"{client}.json"
        run("--json", "hooks", "install", client, "--with-gate", "--settings", str(settings))
        code, out, _ = run("--json", "hooks", "remove", client, "--settings", str(settings))
        assert code == ExitCode.OK
        assert json.loads(out)["removed"] is True
        data = json.loads(settings.read_text())
        # No continuum entries survive; unrelated content would.
        assert "hooks" not in data or all(
            not group.get("hooks") for group in data.get("hooks", {}).values()
        )


def test_codex_install_hints_when_feature_flag_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    settings = tmp_path / "settings.json"
    code, out, _ = run("--json", "hooks", "install", "codex", "--settings", str(settings))
    assert code == ExitCode.OK
    payload = json.loads(out)
    assert "codex_hooks" in payload["feature_flag_hint"]


def test_codex_install_stays_silent_when_flag_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text("[features]\ncodex_hooks = true\n")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    settings = tmp_path / "settings.json"
    code, out, _ = run("--json", "hooks", "install", "codex", "--settings", str(settings))
    assert code == ExitCode.OK
    assert "feature_flag_hint" not in json.loads(out)


def test_codex_install_hints_when_feature_flag_commented_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "# .codex/config.toml\n# example: codex_hooks = true\n[features]\n# not set\n"
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    settings = tmp_path / "settings.json"
    code, out, _ = run("--json", "hooks", "install", "codex", "--settings", str(settings))
    assert code == ExitCode.OK
    payload = json.loads(out)
    assert "feature_flag_hint" in payload
    assert "codex_hooks" in payload["feature_flag_hint"]


def test_gemini_payload_shape_matches_the_observation_contract() -> None:
    """Gemini AfterTool payloads carry the same tool_name/tool_input fields;
    prove `continuum observe` accepts one verbatim through the real CLI."""
    import tempfile

    project = Path(tempfile.mkdtemp())
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "init"], cwd=project, capture_output=True
    )
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "start", "g", "--goal", "gemini"],
        cwd=project,
        capture_output=True,
    )
    artifact = project / "out.txt"
    artifact.write_text("written by gemini")
    gemini_payload = json.dumps(
        {
            "hook_event_name": "AfterTool",
            "tool_name": "write_file",
            "tool_input": {"file_path": str(artifact), "content": "x"},
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "continuum.cli", "--db", str(project / "continuum.db"), "observe"],
        input=gemini_payload,
        capture_output=True,
        text=True,
        cwd=project,
    )
    assert result.returncode == ExitCode.OK, result.stderr


@pytest.mark.parametrize("client", ("claude-code", "gemini", "codex"))
def test_default_settings_path_comes_from_the_profile(
    tmp_path: Path, client: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caught live: an explicit --settings was tested everywhere, so a
    hardcoded CLI default silently sent every client's hooks to Claude
    Code's settings file. The default must come from the profile."""
    monkeypatch.chdir(tmp_path)
    code, _, err = run("--json", "hooks", "install", client)
    assert code == ExitCode.OK, err
    expected = CLIENT_PROFILES[client]["settings"]
    assert (tmp_path / expected).exists(), expected


def _installed_kinds(settings: Path) -> set[str]:
    """The last word of every hook command in the file, across all events."""
    hooks = json.loads(settings.read_text()).get("hooks", {})
    return {
        str(h.get("command", "")).split()[-1]
        for groups in hooks.values()
        if isinstance(groups, list)
        for g in groups
        if isinstance(g.get("hooks"), list)
        for h in g["hooks"]
        if str(h.get("command", "")).strip()
    }


@pytest.mark.parametrize("client", ("claude-code", "gemini", "codex"))
def test_remove_defaults_to_the_same_file_install_wrote(
    tmp_path: Path, client: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #580: the fix above was only ever applied to the installer.

    ``--settings`` defaults to None for both subcommands and its help already
    promises "default: per client profile", but only install fell back, so the
    uninstall the guides name (``continuum hooks remove claude-code``) raised
    ``TypeError`` on ``Path(None)`` before reading anything. The pair has to
    resolve the same file, or install is a one-way door for anyone who did not
    write the path down.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / CLIENT_PROFILES[client]["settings"]
    code, _, err = run("--json", "hooks", "install", client, "--with-gate")
    assert code == ExitCode.OK, err
    assert {"observe", "briefing", "gate"} <= _installed_kinds(settings)

    code, out, err = run("--json", "hooks", "remove", client)
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["removed"] is True
    assert Path(payload["settings"]) == Path(CLIENT_PROFILES[client]["settings"])
    assert _installed_kinds(settings) == set()


def test_remove_without_settings_is_quiet_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstalling twice, or in a directory that was never wired, is a no-op.

    The absent file is the common case for the crash in #580: a hook command is
    the one thing an operator runs without arguments, so the failure had to be a
    reported nothing-to-do rather than a traceback.
    """
    monkeypatch.chdir(tmp_path)
    code, out, err = run("--json", "hooks", "remove", "claude-code")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["removed"] is False
    assert payload["settings"] == CLIENT_PROFILES["claude-code"]["settings"]
    assert not (tmp_path / ".claude").exists()


def test_remove_reports_hooks_not_just_the_observation_hook(tmp_path: Path) -> None:
    """The report has to cover what the removal actually does.

    ``remove_claude_code_hook`` takes out every kind in ``_INSTALLED_KINDS``,
    not just observe, while the text said "Removed observation hook". An
    operator who installed the gate too was told only the observation hook
    went, which is the wrong thing to believe about the file that decides
    whether their side effects are still guarded.
    """
    settings = tmp_path / "settings.json"
    run("hooks", "install", "claude-code", "--with-gate", "--settings", str(settings))
    assert "gate" in _installed_kinds(settings)

    code, out, err = run("hooks", "remove", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert "Removed CONTINUUM hooks from" in out
    assert "observation hook" not in out
    assert _installed_kinds(settings) == set()


@pytest.mark.parametrize("client", ("claude-code", "gemini", "codex"))
def test_briefing_is_wired_on_session_start(tmp_path: Path, client: str) -> None:
    """No CLAUDE.md required: the briefing rides the client's own
    SessionStart event so state reaches the model deterministically."""
    profile = CLIENT_PROFILES[client]
    settings = tmp_path / "settings.json"
    code, _, err = run("--json", "hooks", "install", client, "--settings", str(settings))
    assert code == ExitCode.OK, err
    data = json.loads(settings.read_text())
    starts = data["hooks"][profile["start_event"]]
    ours = [
        g
        for g in starts
        if isinstance(g.get("hooks"), list)
        and any(h.get("command", "").split()[-1] == "briefing" for h in g["hooks"])
    ]
    assert len(ours) == 1


@pytest.mark.parametrize("client", ("claude-code", "gemini", "codex"))
def test_three_installs_leave_one_start_group(tmp_path: Path, client: str) -> None:
    """Repro for #484: briefing was duplicated on every install. Three runs
    must leave exactly one SessionStart group and the third reports present."""
    profile = CLIENT_PROFILES[client]
    settings = tmp_path / "settings.json"
    last_payload: dict[str, object] | None = None
    for _ in range(3):
        code, out, err = run("--json", "hooks", "install", client, "--settings", str(settings))
        assert code == ExitCode.OK, err
        last_payload = json.loads(out)  # type: ignore[assignment]
    assert last_payload is not None
    data = json.loads(settings.read_text())
    assert len(data["hooks"][profile["post_event"]]) == 1
    assert len(data["hooks"][profile["start_event"]]) == 1
    statuses = {str(h["kind"]): str(h["status"]) for h in last_payload["hooks"]}  # type: ignore[union-attr]
    assert statuses["briefing"] == "present"
    assert statuses["observe"] == "present"


def test_briefing_repoint_reuses_group(tmp_path: Path) -> None:
    """Repro for #484 repointing path: a moved venv must repoint briefing
    rather than duplicate it; old command gone, count stays 1, present after."""
    from continuum.clienthooks import install_client_hook

    s = tmp_path / "settings.json"
    assert (
        install_client_hook(
            s, "/old/venv/bin/continuum briefing", event_name="SessionStart", matcher=""
        )
        == "installed"
    )
    assert (
        install_client_hook(
            s, "/new/venv/bin/continuum briefing", event_name="SessionStart", matcher=""
        )
        == "updated"
    )
    data = json.loads(s.read_text())
    groups = data["hooks"]["SessionStart"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == "/new/venv/bin/continuum briefing"
    assert "/old/venv" not in json.dumps(data)
    assert (
        install_client_hook(
            s, "/new/venv/bin/continuum briefing", event_name="SessionStart", matcher=""
        )
        == "present"
    )


def _installed_commands(settings: Path, event_name: str) -> list[str]:
    """Every command wired under ``event_name``, in file order, duplicates included."""
    groups = json.loads(settings.read_text())["hooks"][event_name]
    return [h["command"] for g in groups if isinstance(g.get("hooks"), list) for h in g["hooks"]]


def test_an_install_of_one_kind_does_not_repoint_another(tmp_path: Path) -> None:
    """Only an entry of the same kind counts as the one being installed (#484).

    The kinds used to be checked as a set, so any continuum entry sharing the
    event and matcher matched: installing observe where a briefing was already
    wired repointed the briefing and reported "updated", silently dropping a
    hook the caller never named. Reading the kind off the command keeps the two
    entries apart.
    """
    settings = tmp_path / "settings.json"
    briefing = "/venv/bin/continuum briefing"
    observe = "/venv/bin/continuum observe"
    assert (
        install_client_hook(settings, briefing, event_name="SessionStart", matcher="")
        == "installed"
    )
    assert (
        install_client_hook(settings, observe, event_name="SessionStart", matcher="") == "installed"
    )
    assert _installed_commands(settings, "SessionStart") == [briefing, observe]


def test_a_command_of_no_known_kind_is_appended_never_matched(tmp_path: Path) -> None:
    """The kind is read off the command, so an unknown one matches nothing (issue #484).

    Deriving the kind is what stops an install of one kind repointing another, and
    the flip side has to hold too: a command this module did not build -- including
    one whose quoting cannot be parsed at all -- carries no kind, so it is appended
    rather than mistaken for ours, and unparseable quoting does not raise.
    """
    settings = tmp_path / "settings.json"
    unknown = "/venv/bin/continuum inspect"
    malformed = '"C:\\no\\closing\\quote continuum briefing'
    for command in (unknown, malformed, unknown):
        status = install_client_hook(settings, command, event_name="SessionStart", matcher="")
        assert status == "installed"
    assert _installed_commands(settings, "SessionStart") == [unknown, malformed, unknown]
