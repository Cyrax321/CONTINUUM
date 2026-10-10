"""Client installers beyond Claude Code (issue #209).

Gemini CLI, Codex CLI and Qwen Code expose hook surfaces with the same stdin
contract (tool_name + tool_input JSON) but different settings layouts, event
names and matchers. Wiring is data-driven from CLIENT_PROFILES; these tests pin
each profile's installed shape, idempotency, removal, and the Codex feature
flag hint.

The client list is derived from CLIENT_PROFILES rather than written out here.
A hand-copied tuple is a second source of truth that goes stale the moment a
profile lands, and a client silently excluded from it is exactly the silent
failure this suite exists to catch: a profile can install cleanly and still
never run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from continuum.cli import ExitCode, main
from continuum.clienthooks import _INSTALLED_KINDS, CLIENT_PROFILES, install_client_hook


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def write_gate_registry(root: Path) -> Path:
    """Create the registry ``--with-gate`` reads, relative to the cwd.

    ``install`` refuses the flag when the registry is absent (see
    ``cmd_install``), so a test exercising the gate has to put one on disk
    first or it is exercising the error path instead.
    """
    path = root / ".continuum" / "gate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tools": {"Write": {"key_template": "{file_path}"}}}))
    return path


import io  # noqa: E402

#: Every profiled client, read from the table itself.
CLIENTS = tuple(CLIENT_PROFILES)


class _OsFlavour:
    """``os`` with a chosen ``name``, leaving the real module in place.

    ``clienthooks`` picks its quoting convention by reading ``os.name``, and
    the tests below need that branch. Patching ``os.name`` on the module
    itself would also change what ``pathlib.Path`` returns, because pathlib
    reads the same global: on a POSIX host ``Path(...)`` would become
    ``WindowsPath`` and raise ``NotImplementedError`` inside
    ``_is_managed_hook``, which is production code that has to keep working on
    whichever platform it is really running on. Standing in for the module the
    code under test reads keeps the real ``os`` intact for everything else.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, item: str) -> object:
        return getattr(os, item)


def _as_windows(monkeypatch: pytest.MonkeyPatch, name: str = "nt") -> None:
    """Make ``clienthooks`` read ``os.name`` as ``name`` and nothing else."""
    from continuum import clienthooks

    monkeypatch.setattr(clienthooks, "os", _OsFlavour(name))


#: Clients whose hook reference documents no compaction event, so the
#: installer must wire no precompact hook for them. Derived the same way,
#: because the point of the test is that the key's absence is honoured.
NO_COMPACT_CLIENTS = tuple(
    c for c, profile in CLIENT_PROFILES.items() if "compact_event" not in profile
)

#: Clients that do document one, so the degradation below is not mistaken for
#: the hook simply never being wired anywhere.
COMPACT_CLIENTS = tuple(c for c, profile in CLIENT_PROFILES.items() if "compact_event" in profile)


@pytest.mark.parametrize("client", CLIENTS)
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


@pytest.mark.parametrize("client", CLIENTS)
def test_install_is_idempotent_per_client(tmp_path: Path, client: str) -> None:
    settings = tmp_path / "settings.json"
    for _ in range(2):
        code, _, err = run("--json", "hooks", "install", client, "--settings", str(settings))
        assert code == ExitCode.OK, err
    data = json.loads(settings.read_text())
    profile = CLIENT_PROFILES[client]
    assert len(data["hooks"][profile["post_event"]]) == 1
    assert len(data["hooks"][profile["start_event"]]) == 1


def test_gemini_gate_uses_before_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_gate_registry(tmp_path)
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


def test_remove_cleans_each_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_gate_registry(tmp_path)
    for client in CLIENTS:
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
    import os
    import tempfile

    project = Path(tempfile.mkdtemp())
    # The worktree's src comes first on PYTHONPATH so the subprocesses import
    # the tree under test, not an installed continuum (issue #837).
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [str(Path(__file__).resolve().parents[1] / "src"), os.environ.get("PYTHONPATH")],
            )
        ),
    }
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "init"], cwd=project, capture_output=True, env=env
    )
    subprocess.run(
        [sys.executable, "-m", "continuum.cli", "start", "g", "--goal", "gemini"],
        cwd=project,
        capture_output=True,
        env=env,
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
        env=env,
    )
    assert result.returncode == ExitCode.OK, result.stderr


@pytest.mark.parametrize("client", CLIENTS)
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


@pytest.mark.parametrize("client", CLIENTS)
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
    write_gate_registry(tmp_path)
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
    assert Path(payload["settings"]) == Path(CLIENT_PROFILES["claude-code"]["settings"])
    assert not (tmp_path / ".claude").exists()


def test_remove_reports_hooks_not_just_the_observation_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report has to cover what the removal actually does.

    ``remove_claude_code_hook`` takes out every kind in ``_INSTALLED_KINDS``,
    not just observe, while the text said "Removed observation hook". An
    operator who installed the gate too was told only the observation hook
    went, which is the wrong thing to believe about the file that decides
    whether their side effects are still guarded.
    """
    monkeypatch.chdir(tmp_path)
    write_gate_registry(tmp_path)
    settings = tmp_path / "settings.json"
    run("hooks", "install", "claude-code", "--with-gate", "--settings", str(settings))
    assert "gate" in _installed_kinds(settings)

    code, out, err = run("hooks", "remove", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert "Removed CONTINUUM hooks from" in out
    assert "observation hook" not in out
    assert _installed_kinds(settings) == set()


@pytest.mark.parametrize("client", CLIENTS)
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


@pytest.mark.parametrize("client", CLIENTS)
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


def test_with_gate_refuses_an_absent_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--with-gate`` with no registry installs a guard that disarms itself.

    The gate reads a *missing* registry as "no gate configured" and returns exit 0
    for an unclaimed side-effecting call, so wiring the hook in that state looks
    armed to the operator and allows every call. Failing before touching settings
    is the honest outcome; the message names the file the flag needs.
    """
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "settings.json"
    code, _, err = run(
        "--json", "hooks", "install", "claude-code", "--with-gate", "--settings", str(settings)
    )
    assert code == ExitCode.ERROR
    # The message prints DEFAULT_GATE_CONFIG_PATH, which is a forward-slash
    # relative path on POSIX and a backslash one on Windows; the registry name
    # is what the assertion cares about, not the separator.
    assert ".continuum" + (os.sep == "\\" and "\\" or "/") + "gate.json does not exist" in err
    assert not settings.exists(), "nothing should have been written to settings"


def test_with_gate_refuses_an_unreadable_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that fails to load is reported, not installed against."""
    monkeypatch.chdir(tmp_path)
    registry = write_gate_registry(tmp_path)
    registry.write_text("{not json")
    settings = tmp_path / "settings.json"
    code, _, err = run(
        "--json", "hooks", "install", "claude-code", "--with-gate", "--settings", str(settings)
    )
    assert code == ExitCode.ERROR
    assert "--with-gate needs a readable gate registry" in err
    assert not settings.exists()


def test_with_gate_warns_when_the_registry_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty registry is a warning, not an error: it is a valid starting state.

    A registry with a ``tools`` map and no entries still installs a working gate;
    the operator just has not named anything yet. Telling them and continuing is
    right, unlike the two cases above where the installed hook could not enforce.
    """
    monkeypatch.chdir(tmp_path)
    registry = write_gate_registry(tmp_path)
    registry.write_text(json.dumps({"tools": {}}))
    settings = tmp_path / "settings.json"
    code, _, err = run(
        "--json", "hooks", "install", "claude-code", "--with-gate", "--settings", str(settings)
    )
    assert code == ExitCode.OK, err
    assert "registers no tools" in err
    assert "gate" in _installed_kinds(settings)


@pytest.mark.parametrize("client", NO_COMPACT_CLIENTS)
def test_a_client_without_a_compaction_event_gets_no_precompact_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    """The absence of ``compact_event`` has to mean "wire nothing", not
    "wire something plausible".

    Installing a precompact entry against an event the harness never fires
    looks exactly like durability in the settings file and in the install
    report, while sealing no checkpoint at all. Both the report and the file
    are asserted here so neither half can drift from the profile again.
    """
    profile = CLIENT_PROFILES[client]
    assert "compact_event" not in profile

    monkeypatch.chdir(tmp_path)
    write_gate_registry(tmp_path)

    settings = tmp_path / "settings.json"
    code, out, err = run(
        "--json", "hooks", "install", client, "--with-gate", "--settings", str(settings)
    )
    assert code == ExitCode.OK, err

    payload = json.loads(out)
    assert "precompact" not in {h["kind"] for h in payload["hooks"]}

    data = json.loads(settings.read_text())
    assert "precompact" not in _installed_kinds(settings)
    # No event key named like a compaction hook, whatever the client calls it.
    assert not [event for event in data["hooks"] if "compact" in event.lower()]


@pytest.mark.parametrize("client", COMPACT_CLIENTS)
def test_a_client_with_a_compaction_event_does_get_one(tmp_path: Path, client: str) -> None:
    """The flip side, so the test above cannot pass by the key being unread.

    Without this, deleting ``compact_event`` from every profile would leave
    the no-precompact test green while quietly removing the checkpoint that
    keeps a compacted session resumable.
    """
    profile = CLIENT_PROFILES[client]
    settings = tmp_path / "settings.json"
    code, out, err = run("--json", "hooks", "install", client, "--settings", str(settings))
    assert code == ExitCode.OK, err

    event = profile["compact_event"]
    payload = json.loads(out)
    precompact = [h for h in payload["hooks"] if h["kind"] == "precompact"]
    assert len(precompact) == 1
    assert precompact[0]["event"] == event
    # It was wired, not removed by an opt-out nobody passed.
    assert payload["unwired"] == []

    commands = _installed_commands(settings, event)
    assert len(commands) == 1
    assert commands[0].split()[-1] == "precompact"


@pytest.mark.parametrize("client", CLIENTS)
def test_remove_leaves_the_users_own_hooks_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    """Removal must delete this project's entries and nothing else.

    ``_remove_hooks`` keeps a matcher group that still holds hooks a stranger
    wrote, and drops only the entries the predicate recognises. The three
    shapes below cover where a user's own hook can sit: sharing a matcher with
    ours, alone in its own group, and on an event we never write.
    """
    monkeypatch.chdir(tmp_path)
    write_gate_registry(tmp_path)
    profile = CLIENT_PROFILES[client]
    settings = tmp_path / "settings.json"
    seeded = {
        "unrelatedKey": {"keep": True},
        "hooks": {
            # Shares our matcher, so only per-hook ownership can save it.
            profile["post_event"]: [
                {
                    "matcher": profile["write_matcher"],
                    "hooks": [{"type": "command", "command": "my-own-collector"}],
                }
            ],
            profile["start_event"]: [
                {"matcher": "", "hooks": [{"type": "command", "command": "my-own-greeter"}]}
            ],
            "SomeEventWeNeverWrite": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "unrelated-tool"}]}
            ],
        },
    }
    settings.write_text(json.dumps(seeded))

    code, _, err = run(
        "--json", "hooks", "install", client, "--with-gate", "--settings", str(settings)
    )
    assert code == ExitCode.OK, err
    code, out, err = run("--json", "hooks", "remove", client, "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert json.loads(out)["removed"] is True

    data = json.loads(settings.read_text())
    assert data["unrelatedKey"] == {"keep": True}
    survivors = sorted(
        command for event in data["hooks"] for command in _installed_commands(settings, event)
    )
    assert survivors == ["my-own-collector", "my-own-greeter", "unrelated-tool"]
    # None of ours survived, checked against _INSTALLED_KINDS rather than a
    # list written here: the whole point of that tuple is that it is the one
    # place a hook kind is named, and a test that repeats the list would let
    # a new kind drift out of this assertion exactly as it did in #484.
    assert _installed_kinds(settings) & set(_INSTALLED_KINDS) == set()


@pytest.mark.parametrize(
    ("command_parts", "platform"),
    [
        # A POSIX venv path, joined POSIX-style.
        (["/home/user/my project/.venv/bin/continuum", "observe"], "posix"),
        # The same path joined cmd.exe-style: spaces must survive either way.
        (["/home/user/my project/.venv/bin/continuum", "observe"], "nt"),
        # Windows paths routinely contain spaces; the branch exists for them.
        ([r"C:\Program Files\continuum\Scripts\continuum.exe", "observe"], "nt"),
        ([r"C:\Program Files\continuum\Scripts\continuum.exe", "observe"], "posix"),
        # The gate form carries an extra token and must round-trip too.
        ([r"C:\Users\Jane Doe\venv\Scripts\continuum.exe", "--db", r"D:\my db\a.db", "gate"], "nt"),
    ],
)
def test_join_and_split_command_round_trip_on_a_path_with_spaces(
    monkeypatch: pytest.MonkeyPatch, command_parts: list[str], platform: str
) -> None:
    """``_split_command`` is the inverse of ``_join_command`` on both families.

    The two shells disagree on quoting, so the join is not the inverse of a
    single split: the round trip only holds if each branch is paired with its
    own. An unpaired branch is how every installed hook dies quietly on
    Windows, because the recogniser stops seeing its own command and then
    appends a duplicate on the next install (#484, #526).
    """
    from continuum import clienthooks

    _as_windows(monkeypatch, platform)
    joined = clienthooks._join_command(command_parts)
    assert clienthooks._split_command(joined) == command_parts


def test_a_windows_path_stays_ours_under_the_cmd_exe_convention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round trip is only half the contract: install and remove both decide
    ownership through :func:`_is_managed_hook`, so a command that survives the
    round trip but stops being recognised would still append a duplicate on
    every re-run (#484, #526).

    Paired deliberately with the branch that can actually host it: a Windows
    path is only a native executable path under ``cmd.exe``, and the same
    string under POSIX shlex is correctly not one.
    """
    from continuum import clienthooks

    _as_windows(monkeypatch, "nt")
    command = clienthooks._join_command(
        [r"C:\Program Files\continuum\Scripts\continuum.exe", "observe"]
    )
    assert command.startswith('"'), "cmd.exe quoting is expected on this branch"
    assert clienthooks._split_command(command) == [
        r"C:\Program Files\continuum\Scripts\continuum.exe",
        "observe",
    ]
    assert clienthooks._is_managed_hook({"command": command}, "observe")
