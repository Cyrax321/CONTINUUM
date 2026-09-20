"""The compaction boundary: ``continuum precompact`` and its wiring (issue #449).

Context compaction is the one interruption that is scheduled rather than
accidental: the harness announces it, and everything the transcript held that
was never recorded disappears a moment later. Claude Code exposes PreCompact
for exactly this, but until now `hooks install` wired SessionStart, PostToolUse
and (opt-in) PreToolUse while PreCompact was left as a hand-edit in
docs/guides/embed-claude-code.md - so the durability of the most predictable
loss in the system depended on the operator remembering a JSON snippet.

Two halves are pinned here: the command seals a checkpoint and leaves the
snapshots the guide promises without ever failing its host, and `hooks install`
wires it (`hooks remove` unwires it) as one of the kinds this project owns.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from continuum.cli import ExitCode, main
from continuum.clienthooks import CLIENT_PROFILES, _is_continuum_hook
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cwd with a database in it.

    The snapshots and .continuum/resume.json are written relative to the
    working directory, because a hook runs with the project root as cwd. Tests
    chdir for the same reason rather than passing paths around.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def db_path(project: Path) -> str:
    return str(project / "continuum.db")


def start_run(project: Path, run_id: str = "compacting", goal: str = "survive compaction") -> str:
    code, _, err = run("--db", db_path(project), "start", run_id, "--goal", goal)
    assert code == ExitCode.OK, err
    return run_id


# --- the command -------------------------------------------------------------- #


def test_precompact_seals_a_checkpoint_on_the_active_run(project: Path) -> None:
    start_run(project)
    code, out, err = run("--db", db_path(project), "--json", "precompact")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["active_run"] == "compacting"
    assert payload["checkpoint_id"]
    assert payload["failures"] == []

    with SQLiteStorage(db_path(project)) as store:
        checkpoints = store.list_checkpoints("compacting")
    assert [c.checkpoint_id for c in checkpoints] == [payload["checkpoint_id"]]


def test_the_trigger_records_why_the_checkpoint_was_taken(project: Path) -> None:
    """``context_pressure``, not ``manual``.

    The existing ContextPressurePolicy can only fire when the agent volunteers
    its token counts; this hook is the same signal observed from outside the
    model, so it is recorded as the same trigger. A checkpoint labelled
    "manual" would be indistinguishable afterwards from one somebody typed.
    """
    start_run(project)
    code, out, err = run("--db", db_path(project), "--json", "precompact")
    assert code == ExitCode.OK, err
    assert json.loads(out)["trigger"] == "context_pressure"

    with SQLiteStorage(db_path(project)) as store:
        checkpoints = store.list_checkpoints("compacting")
    assert checkpoints[0].trigger == "context_pressure"


def test_both_snapshots_land_where_the_guide_says_to_read_them(project: Path) -> None:
    start_run(project, goal="prove the snapshots")
    code, out, err = run("--db", db_path(project), "--json", "precompact")
    assert code == ExitCode.OK, err
    snapshots = json.loads(out)["snapshots"]
    assert snapshots == {
        "resume": ".continuum/precompact-resume.json",
        "verify": ".continuum/precompact-verify.json",
    }

    resume = json.loads((project / ".continuum/precompact-resume.json").read_text())
    # The fields the PreCompact section of the guide tells operators to inspect.
    assert resume["run_id"] == "compacting"
    assert resume["goal"] == "prove the snapshots"
    assert resume["mode"] == "resume"
    assert "verified" in resume["contract"] and "invalidated" in resume["contract"]

    verify = json.loads((project / ".continuum/precompact-verify.json").read_text())
    assert verify["ok"] is True
    assert verify["checked"] > 0


def test_the_reported_paths_read_the_same_on_every_platform(project: Path) -> None:
    """One hook, one path, whatever the host separator is.

    ``str(Path(".continuum/precompact-resume.json"))`` is
    ``.continuum\\precompact-resume.json`` on Windows, so recording the paths
    that way had the same hook report one thing on Linux and another on Windows
    while the guide names exactly one. These values are a published contract, so
    they are normalised where they are recorded rather than at each reader. Only
    a Windows runner can fail this, which is where the bug lived.
    """
    start_run(project)
    code, out, err = run("--db", db_path(project), "--json", "precompact")
    assert code == ExitCode.OK, err
    reported = json.loads(out)["snapshots"]
    assert reported
    for path in reported.values():
        assert "\\" not in path, path
        assert (project / path).is_file()


def test_the_checkpoint_leaves_resume_json_for_the_next_session(project: Path) -> None:
    """End to end: compaction now, instant detection later (#394 meets #449).

    The SessionStart briefing short-circuits on .continuum/resume.json to stay
    fast, which means a compaction that never checkpointed leaves the next
    session with nothing to detect. The checkpoint this hook takes is what
    writes that file.
    """
    start_run(project)
    assert not (project / ".continuum/resume.json").exists()

    code, _, err = run("--db", db_path(project), "precompact")
    assert code == ExitCode.OK, err
    assert (project / ".continuum/resume.json").exists()

    code, out, err = run("--db", db_path(project), "briefing")
    assert code == ExitCode.OK, err
    assert "compacting" in out


def test_no_active_run_is_not_an_error(project: Path) -> None:
    """Hooks fire for every session in this directory, including ones with
    nothing to do with CONTINUUM. A wall of failures at every compaction would
    pressure the operator into uninstalling the instrumentation."""
    code, out, err = run("--db", db_path(project), "--json", "precompact")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["active_run"] is None
    assert payload["checkpoint_id"] is None
    assert not (project / ".continuum/precompact-resume.json").exists()


def test_an_explicit_run_id_is_honoured(project: Path) -> None:
    start_run(project, "first")
    start_run(project, "second")
    code, out, err = run("--db", db_path(project), "--json", "precompact", "--run-id", "first")
    assert code == ExitCode.OK, err
    assert json.loads(out)["active_run"] == "first"


def test_a_run_that_does_not_exist_is_reported_not_invented(project: Path) -> None:
    """Silence for a missing run would be the wrong lesson from "never fail the
    host": an operator who baked a run id into the hook command wants to hear
    that it is wrong."""
    start_run(project)
    code, _, err = run("--db", db_path(project), "precompact", "--run-id", "typo")
    assert code == ExitCode.NOT_FOUND
    assert "typo" in err


def test_an_unwritable_snapshot_is_reported_and_the_checkpoint_still_stands(
    project: Path,
) -> None:
    """The checkpoint is the durable half and it is already in the chain.

    A directory sitting where the snapshot goes stands in for the read-only or
    full working tree: the write fails, the failure is named, and the exit
    status stays 0 because taking the host's compaction down would cost more
    than the snapshot is worth.
    """
    start_run(project)
    (project / ".continuum").mkdir(parents=True, exist_ok=True)
    (project / ".continuum/precompact-resume.json").mkdir()

    code, out, err = run("--db", db_path(project), "--json", "precompact")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["checkpoint_id"]
    assert list(payload["snapshots"]) == ["verify"]
    assert any("precompact-resume.json" in failure for failure in payload["failures"])

    with SQLiteStorage(db_path(project)) as store:
        assert len(store.list_checkpoints("compacting")) == 1


def test_the_failure_is_visible_in_the_text_output_too(project: Path) -> None:
    """A stale snapshot from an earlier compaction read as if it described this
    one is the failure mode; the human path has to say so as well."""
    start_run(project)
    (project / ".continuum").mkdir(parents=True, exist_ok=True)
    (project / ".continuum/precompact-verify.json").mkdir()

    code, out, err = run("--db", db_path(project), "precompact")
    assert code == ExitCode.OK, err
    assert "snapshot not written" in out
    assert "precompact-verify.json" in out


# --- the wiring --------------------------------------------------------------- #


def _commands_under(settings: Path, event_name: str) -> list[str]:
    groups = json.loads(settings.read_text())["hooks"].get(event_name, [])
    return [h["command"] for g in groups if isinstance(g.get("hooks"), list) for h in g["hooks"]]


def _recipes_from_the_guide() -> list[str]:
    """Every PreCompact command docs/guides/embed-claude-code.md tells operators
    to paste, read out of the guide itself.

    Copying the compound into this file would let the two drift, and drift is the
    whole failure mode: the installer has to recognise what the guide actually
    published, not what a test once said it published.
    """
    guide = Path(__file__).resolve().parents[1] / "docs/guides/embed-claude-code.md"
    commands: list[str] = []
    for block in re.findall(r"```json\n(.*?)```", guide.read_text(encoding="utf-8"), re.DOTALL):
        if '"PreCompact"' not in block:
            continue
        for group in json.loads(block)["hooks"]["PreCompact"]:
            commands += [hook["command"] for hook in group["hooks"]]
    return commands


def _write_recipe(settings: Path, command: str, *, matcher: str = "") -> None:
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreCompact": [
                        {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
                    ]
                }
            }
        )
    )


def test_install_wires_precompact_for_claude_code(tmp_path: Path) -> None:
    """The issue's first ask: install, then read the settings back."""
    settings = tmp_path / "settings.json"
    code, out, err = run("--json", "hooks", "install", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err

    data = json.loads(settings.read_text())
    entry = data["hooks"]["PreCompact"]
    assert len(entry) == 1
    # The empty matcher is the one the guide's hand-written recipe uses.
    assert entry[0]["matcher"] == ""
    assert entry[0]["hooks"][0]["command"].split()[-1] == "precompact"

    wired = {h["kind"]: h["event"] for h in json.loads(out)["hooks"]}
    assert wired["precompact"] == "PreCompact"


def test_remove_unwires_precompact(tmp_path: Path) -> None:
    """The issue's second ask. The kind had to join _INSTALLED_KINDS for this to
    pass: a hook the remover does not recognise survives an uninstall and keeps
    pointing at a database the operator believes they detached from."""
    settings = tmp_path / "settings.json"
    run("hooks", "install", "claude-code", "--settings", str(settings))
    assert _commands_under(settings, "PreCompact")

    code, out, _ = run("--json", "hooks", "remove", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK
    assert json.loads(out)["removed"] is True
    assert "PreCompact" not in json.loads(settings.read_text()).get("hooks", {})


def test_no_precompact_skips_it(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    code, out, err = run(
        "--json", "hooks", "install", "claude-code", "--no-precompact", "--settings", str(settings)
    )
    assert code == ExitCode.OK, err
    assert "PreCompact" not in json.loads(settings.read_text())["hooks"]
    assert "precompact" not in {h["kind"] for h in json.loads(out)["hooks"]}
    # Nothing was there to take out, so nothing is claimed to have been.
    assert json.loads(out)["unwired"] == []


def test_no_precompact_takes_out_an_entry_an_earlier_install_wrote(tmp_path: Path) -> None:
    """Opting out has to undo, not merely decline.

    The flag means "do not seal a checkpoint at every compaction". Run against a
    directory where the hook is already wired, skipping the install while leaving
    the entry in place honours the word and none of the meaning: the hook keeps
    firing, and the operator who just asked for it to stop has no way to tell.
    """
    settings = tmp_path / "settings.json"
    code, _, err = run("hooks", "install", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert _commands_under(settings, "PreCompact")

    code, out, err = run(
        "--json", "hooks", "install", "claude-code", "--no-precompact", "--settings", str(settings)
    )
    assert code == ExitCode.OK, err
    assert "PreCompact" not in json.loads(settings.read_text())["hooks"]
    assert json.loads(out)["unwired"] == [{"event": "PreCompact", "kind": "precompact"}]
    # Declining one hook is not uninstalling the others.
    assert _commands_under(settings, "PostToolUse")
    assert _commands_under(settings, "SessionStart")


def test_no_precompact_leaves_a_hand_written_recipe_alone(tmp_path: Path) -> None:
    """The flag makes room for the pinned-run recipe, so it must not delete it.

    Following the active run is what the managed hook does; the guide's compound
    is how you pin one instead, and opting out of the managed hook is what stops
    the two from firing together. Removing the entry the operator wrote would
    leave them with no compaction checkpoint at all, which is the opposite of
    what the flag was reached for. Only the command this installer writes goes.
    """
    recipes = _recipes_from_the_guide()
    assert recipes, "the guide no longer publishes a PreCompact command to keep"
    for index, recipe in enumerate(recipes):
        settings = tmp_path / f"kept-{index}.json"
        _write_recipe(settings, recipe)
        code, out, err = run(
            "--json",
            "hooks",
            "install",
            "claude-code",
            "--no-precompact",
            "--settings",
            str(settings),
        )
        assert code == ExitCode.OK, err
        assert _commands_under(settings, "PreCompact") == [recipe]
        assert json.loads(out)["unwired"] == []


@pytest.mark.parametrize("client", ("gemini", "codex"))
def test_a_client_with_no_compaction_event_gets_no_hook(tmp_path: Path, client: str) -> None:
    """Wiring a hook to an event the harness never fires would look like
    durability without being any. Only Claude Code documents a compaction hook,
    so only its profile carries compact_event."""
    assert "compact_event" not in CLIENT_PROFILES[client]
    settings = tmp_path / f"{client}.json"
    code, out, err = run("--json", "hooks", "install", client, "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert "PreCompact" not in json.loads(settings.read_text())["hooks"]
    assert "precompact" not in {h["kind"] for h in json.loads(out)["hooks"]}


def test_three_installs_leave_one_precompact_entry(tmp_path: Path) -> None:
    """The #484 duplication trap, re-armed for the new kind: the installer and
    the remover read one list of kinds, so an added kind is recognised by both
    or neither."""
    settings = tmp_path / "settings.json"
    last = ""
    for _ in range(3):
        code, last, err = run(
            "--json", "hooks", "install", "claude-code", "--settings", str(settings)
        )
        assert code == ExitCode.OK, err
    assert len(_commands_under(settings, "PreCompact")) == 1
    statuses = {h["kind"]: h["status"] for h in json.loads(last)["hooks"]}
    assert statuses["precompact"] == "present"


def test_a_moved_precompact_command_is_repointed_not_duplicated(tmp_path: Path) -> None:
    """A moved virtualenv must self-heal rather than leave a dead binary wired,
    and must not leave two entries to fire at every compaction."""
    settings = tmp_path / "settings.json"
    _write_recipe(settings, "/old/venv/bin/continuum precompact")
    code, out, err = run("--json", "hooks", "install", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    commands = _commands_under(settings, "PreCompact")
    assert len(commands) == 1
    assert "/old/venv" not in commands[0]
    statuses = {h["kind"]: h["status"] for h in json.loads(out)["hooks"]}
    assert statuses["precompact"] == "updated"


def test_the_recipe_the_guide_publishes_counts_as_ours() -> None:
    """It cannot end in its kind, so the shape check alone does not see it.

    Every other hook this project installs ends in the subcommand it runs, which
    is what makes an existing entry the same hook rather than a different one.
    The compaction recipe predates the ``precompact`` subcommand: it is a
    compound of ``continuum`` calls that ends in a redirect. Recognising it is
    what the two tests below rest on.
    """
    recipes = _recipes_from_the_guide()
    assert recipes, "the guide no longer publishes a PreCompact command to adopt"
    for recipe in recipes:
        assert _is_continuum_hook({"command": recipe}, "precompact"), recipe


def test_install_adopts_a_hand_pasted_recipe_instead_of_firing_twice(tmp_path: Path) -> None:
    """The #484 duplication trap, in the shape an upgrading operator meets it.

    Anyone who wired PreCompact before it was automated pasted the guide's
    compound. Left unrecognised, ``hooks install`` appends its own entry beside
    it and both fire at every compaction: one checkpoint on the active run, one
    on whatever run id the paste happens to name, plus two snapshot writes
    racing for the same two files.
    """
    recipes = _recipes_from_the_guide()
    assert recipes, "the guide no longer publishes a PreCompact command to adopt"
    for index, recipe in enumerate(recipes):
        settings = tmp_path / f"adopted-{index}.json"
        _write_recipe(settings, recipe)
        code, out, err = run(
            "--json", "hooks", "install", "claude-code", "--settings", str(settings)
        )
        assert code == ExitCode.OK, err
        commands = _commands_under(settings, "PreCompact")
        assert commands != [recipe], recipe
        assert len(commands) == 1, commands
        assert commands[0].split()[-1] == "precompact"
        statuses = {h["kind"]: h["status"] for h in json.loads(out)["hooks"]}
        assert statuses["precompact"] == "updated"


def test_remove_takes_out_a_hand_pasted_recipe_too(tmp_path: Path) -> None:
    """``hooks remove`` that leaves a ``continuum checkpoint`` firing at every
    compaction is a lie: it keeps writing to a database the operator believes
    they detached from, and nothing in the settings file says so."""
    recipes = _recipes_from_the_guide()
    assert recipes, "the guide no longer publishes a PreCompact command to remove"
    for index, recipe in enumerate(recipes):
        settings = tmp_path / f"removed-{index}.json"
        _write_recipe(settings, recipe)
        code, out, err = run(
            "--json", "hooks", "remove", "claude-code", "--settings", str(settings)
        )
        assert code == ExitCode.OK, err
        assert json.loads(out)["removed"] is True
        assert "PreCompact" not in json.loads(settings.read_text()).get("hooks", {})


def test_a_stranger_that_merely_names_the_snapshot_is_left_alone(tmp_path: Path) -> None:
    """Recognition has to stay narrow enough to rewrite somebody's config on.

    The snapshot filenames are the fingerprint, so on their own they would claim
    any tool that reads what this project writes: a backup job archiving the
    file, a jq pipeline watching it. Ours also has to invoke our CLI.
    """
    watcher = "cp .continuum/precompact-resume.json /backups/"
    assert not _is_continuum_hook({"command": watcher}, "precompact")

    settings = tmp_path / "settings.json"
    _write_recipe(settings, watcher)
    code, _, err = run("hooks", "install", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert watcher in _commands_under(settings, "PreCompact")

    code, _, err = run("hooks", "remove", "claude-code", "--settings", str(settings))
    assert code == ExitCode.OK, err
    assert _commands_under(settings, "PreCompact") == [watcher]


# --- the Codex gap ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "guide",
    ("docs/guides/embed-codex.md", "docs/recipes/codex.md"),
)
def test_the_codex_matcher_matches_what_the_guides_document(guide: str) -> None:
    """Issue #449 gap 2, at the level the issue asks for: keep the docs honest.

    Codex hooks traverse shell-like calls only, so `apply_patch` and MCP tools
    do not fire `observe` there. That limitation is documented as a literal
    matcher in two places, and a widened profile with stale guides would leave
    operators believing in coverage they do not have. Pinning the string in CI
    is what makes the docs and the code fail together instead of diverging.
    """
    matcher = CLIENT_PROFILES["codex"]["write_matcher"]
    text = (Path(__file__).resolve().parents[1] / guide).read_text(encoding="utf-8")
    assert matcher in text, f"{guide} does not document the matcher {matcher!r}"
