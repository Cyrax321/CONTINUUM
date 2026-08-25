"""CLI behaviour, with emphasis on the exit-code contract.

``continuum resume "$RUN" && ./start-agent.sh`` is the line these tests exist to
protect. If an unsafe run ever exits 0, an agent gets launched onto stale state
or an unreconciled side effect — so the exit code is treated as a safety
guarantee, not a formatting detail.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.actions import ActionLedger, ProbeReconciler, Resolution, reconcile_pending
from continuum.checkpoint import CheckpointManager
from continuum.cli import ExitCode, main
from continuum.cli.exitcodes import exit_code_for
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import Finding, RecoveryMode, Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> Iterator[str]:
    """A seeded run: 60 documents, a dependency, and an interrupted side effect."""
    path = str(tmp_path / "demo.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        store.append_event(
            "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
        )
        store.append_event(
            "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
        )
        store.append_event(
            "run_1",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": "paper_128", "summary": "study", "source": "dataset"},
        )
        store.append_event(
            "run_1",
            EventType.FINDING_ADDED,
            {"finding_id": "finding_17", "claim": "X holds", "evidence": ["paper_128"]},
        )
        for i in range(60):
            store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})
        CheckpointManager(store).checkpoint(
            "run_1", environment=capture("run_1", StaticProvider(dataset="v3"))
        )
    yield path


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def interrupt_a_side_effect(db: str) -> None:
    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Anomaly"})


# --- creating a run from the CLI (issue #204) -------------------------------- #


def test_start_creates_a_run_the_whole_toolchain_can_use(tmp_path: Path) -> None:
    """The CLI could not originate work before `start`: the resume hint pointed
    at `continuum checkpoint <run_id>`, which fails on a run that does not
    exist yet."""
    path = str(tmp_path / "fresh.db")
    code, out, err = run("--db", path, "start", "myrun", "--goal", "Ship the thing")
    assert code == ExitCode.OK, err

    with SQLiteStorage(path) as store:
        assert store.get_run("myrun").goal == "Ship the thing"
        events = store.read_events("myrun")
    assert [e.type.value for e in events] == ["RUN_STARTED"]
    assert events[0].payload["goal"] == "Ship the thing"

    # The run is now visible everywhere a run must be.
    code, out, _ = run("--db", path, "runs")
    assert "myrun" in out
    # A human-asserted goal with no self-reported progress is genuinely safe
    # to resume: the verdict must be OK, not merely "found".
    code, out, _ = run("--db", path, "resume", "myrun")
    assert code == ExitCode.OK
    assert "RESUME" in out


def test_start_without_a_goal_is_a_usage_error(tmp_path: Path) -> None:
    path = str(tmp_path / "fresh.db")
    with pytest.raises(SystemExit):
        run("--db", path, "start", "myrun")


def test_starting_an_existing_run_fails_without_touching_history(db: str) -> None:
    before_events: int
    with SQLiteStorage(db) as store:
        before_events = store.last_sequence("run_1")

    code, _, err = run("--db", db, "start", "run_1", "--goal", "hijack")
    assert code == ExitCode.ERROR
    assert "already exists" in err

    with SQLiteStorage(db) as store:
        assert store.get_run("run_1").goal == "Analyze 100 documents"
        assert store.last_sequence("run_1") == before_events


# --- the exit-code contract ------------------------------------------------ #


def test_a_verified_run_exits_zero(db: str) -> None:
    code, out, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v3")
    assert code == ExitCode.OK
    assert "RESUME" in out


def test_a_changed_dependency_does_not_exit_zero(db: str) -> None:
    """The pipeline must short-circuit rather than launch onto stale state."""
    code, _, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v4")
    assert code != ExitCode.OK
    assert code == ExitCode.REQUIRES_REPAIR


def test_an_uncertain_side_effect_demands_a_human(db: str) -> None:
    interrupt_a_side_effect(db)
    code, out, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v3")
    assert code == ExitCode.REQUIRES_HUMAN
    assert "REQUEST_HUMAN" in out


def test_reconciling_restores_a_zero_exit(db: str) -> None:
    interrupt_a_side_effect(db)
    assert run("--db", db, "resume", "run_1", "--env", "dataset=v3")[0] != ExitCode.OK

    with SQLiteStorage(db) as store:
        reconcile_pending(
            ActionLedger(store, "run_1"),
            ProbeReconciler(lambda a: Resolution(occurred=True, external_id="481")),
        )

    assert run("--db", db, "resume", "run_1", "--env", "dataset=v3")[0] == ExitCode.OK


def test_omitting_the_environment_is_not_treated_as_unchanged(db: str) -> None:
    """Not checking is not the same as checking and finding nothing wrong."""
    code, _, _ = run("--db", db, "resume", "run_1")
    assert code != ExitCode.OK


def test_a_missing_run_is_distinguishable(db: str) -> None:
    code, _, err = run("--db", db, "inspect", "nosuchrun")
    assert code == ExitCode.NOT_FOUND
    assert "error:" in err


@pytest.mark.parametrize(
    "command,extra",
    [
        ("inspect", []),
        ("history", []),
        ("events", []),
        ("verify", []),
        ("actions", []),
        ("replay", []),
        ("resume", []),
        ("validate", []),
        ("show-contract", []),
        # Mutating commands were added after issue #202: `checkpoint` used to
        # fail with a ProjectionError (exit 1) instead of NOT_FOUND because it
        # projected before checking the run existed.
        ("checkpoint", []),
        ("confirm", []),
        ("attest", []),
        ("attest-verify", ["--attest", "irrelevant-on-missing-run.json"]),
        ("fork", ["--reason", "test"]),
    ],
)
def test_no_command_reports_success_for_a_run_that_does_not_exist(
    db: str, command: str, extra: list[str]
) -> None:
    """A typo'd run name must never look like a clean bill of health.

    An empty run has a trivially valid (empty) event chain and no recorded
    actions, so `verify` and `actions` would happily exit 0 — letting
    `continuum verify $TYPO && deploy` succeed against a name nobody has ever
    written to. Mutating commands owe the same distinction: `checkpoint`
    diagnosed a missing run as a projection error until issue #202.
    """
    code, _, err = run("--db", db, command, "definitely-not-a-run", *extra)
    assert code != ExitCode.OK, f"{command} reported success for a nonexistent run"
    assert code == ExitCode.NOT_FOUND, f"{command} misdiagnosed a missing run (exit {code})"
    assert "definitely-not-a-run" in err


def test_not_found_messages_are_not_double_quoted(db: str) -> None:
    """KeyError.__str__ repr-wraps its message; users should not see that."""
    _, _, err = run("--db", db, "history", "ghost")
    assert "no such run: 'ghost'" in err
    assert '"no such run' not in err


def test_only_resume_maps_to_a_zero_exit() -> None:
    for mode in RecoveryMode:
        code = exit_code_for(mode)
        assert (code == ExitCode.OK) == (mode is RecoveryMode.RESUME)


def test_an_unclassified_mode_is_never_mistaken_for_permission() -> None:
    """A mode added later, before anyone assigns it a code, must fail closed.

    Iterating the known modes cannot catch this: the fallback is only reached
    by a value the table has never heard of.
    """

    class FutureMode:
        value = "some_mode_invented_next_year"

    assert exit_code_for(FutureMode()) == ExitCode.UNSAFE  # type: ignore[arg-type]
    assert exit_code_for(FutureMode()) != ExitCode.OK  # type: ignore[arg-type]


def test_a_corrupted_chain_reports_corruption(db: str) -> None:
    import sqlite3

    raw = sqlite3.connect(db)
    raw.execute("UPDATE events SET payload = '{\"x\":1}' WHERE sequence = 2")
    raw.commit()
    raw.close()

    code, out, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.CORRUPTED
    assert "INTEGRITY FAILURE" in out
    assert "trusted through sequence 1" in out


# --- read-only commands stay read-only ------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ("inspect", "run_1"),
        ("history", "run_1"),
        ("events", "run_1"),
        ("verify", "run_1"),
        ("actions", "run_1"),
        ("replay", "run_1"),
        ("validate", "run_1"),
        ("resume", "run_1"),
        ("show-contract", "run_1"),
    ],
)
def test_inspection_never_mutates_the_run(db: str, argv: tuple[str, ...]) -> None:
    with SQLiteStorage(db) as store:
        before = (store.last_sequence("run_1"), list(store.list_versions("run_1")))

    run("--db", db, *argv)

    with SQLiteStorage(db) as store:
        after = (store.last_sequence("run_1"), list(store.list_versions("run_1")))
    assert before == after


def test_checkpoint_does_mutate(db: str) -> None:
    with SQLiteStorage(db) as store:
        before = store.last_sequence("run_1")

    code, out, _ = run("--db", db, "checkpoint", "run_1", "--trigger", "manual")
    assert code == ExitCode.OK
    assert "Checkpoint" in out

    with SQLiteStorage(db) as store:
        assert store.last_sequence("run_1") > before


# --- output ---------------------------------------------------------------- #


def test_json_output_is_parseable(db: str) -> None:
    code, out, _ = run("--db", db, "--json", "resume", "run_1", "--env", "dataset=v4")
    payload = json.loads(out)
    assert payload["mode"] == "repair_and_resume"
    assert payload["safe"] is False
    assert payload["contract"]["next_allowed_action"]
    assert payload["progress"]["completed"] == 60


def test_json_and_text_are_never_mixed(db: str) -> None:
    _, out, _ = run("--db", db, "--json", "inspect", "run_1")
    json.loads(out)  # would raise if prose leaked into the stream


def test_inspect_reports_verified_progress(db: str) -> None:
    _, out, _ = run("--db", db, "inspect", "run_1")
    assert "60 completed" in out
    assert "dataset: v3 [valid]" in out


def test_inspect_can_read_a_past_version(db: str) -> None:
    _, out, _ = run("--db", db, "inspect", "run_1", "--version", "0")
    assert "v0" in out


def test_history_lists_checkpoints(db: str) -> None:
    _, out, _ = run("--db", db, "history", "run_1")
    assert "VERSION" in out
    assert "v0" in out


def test_history_lists_every_checkpoint_sharing_a_version(db: str) -> None:
    """Issue #43: two checkpoints at one state version are two checkpoints.

    ``put_version`` returns the same version when the state fingerprint has not
    changed, so keying the listing by version hid whole checkpoints -- exactly
    the lineage ``history`` exists to show.
    """
    with SQLiteStorage(db) as store:
        manager = CheckpointManager(store)
        # No new events in between, so the state fingerprint is unchanged and
        # both checkpoints land on the same version.
        first = manager.checkpoint("run_1")
        second = manager.checkpoint("run_1")
        assert first.version == second.version
        assert first.checkpoint_id != second.checkpoint_id

    _, out, _ = run("--db", db, "--json", "history", "run_1")
    listed = json.loads(out)["checkpoints"]
    ids = [row["checkpoint_id"] for row in listed]
    assert first.checkpoint_id in ids
    assert second.checkpoint_id in ids
    assert len(ids) == len(set(ids)), "each checkpoint appears once"


def test_diff_compares_two_versions(db: str) -> None:
    with SQLiteStorage(db) as store:
        store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": 99})
        CheckpointManager(store).checkpoint("run_1")

    _, out, _ = run("--db", db, "diff", "run_1", "0", "1")
    assert "→" in out
    assert "completed" in out
    assert "completed: completed" not in out  # no duplicated field name


def test_events_can_be_windowed(db: str) -> None:
    _, out, _ = run("--db", db, "events", "run_1", "--after", "60", "--upto", "62")
    assert "61" in out and "62" in out
    assert "\n    1  " not in out


def test_actions_flags_unresolved_outcomes(db: str) -> None:
    interrupt_a_side_effect(db)
    code, out, _ = run("--db", db, "actions", "run_1")
    assert code == ExitCode.REQUIRES_HUMAN
    assert "unresolved outcomes" in out


def test_actions_on_a_clean_run_exits_zero(db: str) -> None:
    code, out, _ = run("--db", db, "actions", "run_1")
    assert code == ExitCode.OK
    assert "No actions recorded" in out


def test_the_contract_can_be_printed(db: str) -> None:
    _, out, _ = run("--db", db, "show-contract", "run_1", "--env", "dataset=v4")
    assert "recovery_status:" in out
    assert "next_allowed:" in out


def test_validate_with_dashboard_renders_the_phase_14_dashboard(db: str) -> None:
    code, out, _ = run("--db", db, "validate", "run_1", "--env", "dataset=v3", "--dashboard")
    assert code == ExitCode.OK
    assert "CONTINUUM RECOVERY DASHBOARD" in out
    assert "run_1" in out
    assert "safe to resume:" in out


def test_validate_without_dashboard_stays_machine_friendly(db: str) -> None:
    code, out, _ = run("--db", db, "validate", "run_1", "--env", "dataset=v3")
    assert code == ExitCode.OK
    assert "CONTINUUM RECOVERY DASHBOARD" not in out
    assert "RESUME" in out or "REPAIR" in out


def test_replay_rederives_state_from_events(db: str) -> None:
    _, out, _ = run("--db", db, "replay", "run_1")
    assert "60 completed" in out


def test_replay_upto_excluding_run_started_fails_cleanly(db: str) -> None:
    code, _, err = run("--db", db, "replay", "run_1", "--upto", "0")
    assert code == ExitCode.ERROR
    # The whole diagnostic matters: the boundary that failed *and* the way out.
    assert (
        "--upto 0 excludes the RUN_STARTED event for run 'run_1'; "
        "increase --upto or omit it to replay from the beginning"
    ) in err


def test_runs_lists_what_exists(db: str) -> None:
    _, out, _ = run("--db", db, "runs")
    assert "run_1" in out
    assert "Analyze 100 documents" in out


def test_an_empty_database_says_so(tmp_path: Path) -> None:
    code, out, _ = run("--db", str(tmp_path / "empty.db"), "runs")
    assert code == ExitCode.OK
    assert "No runs recorded" in out


def test_init_reports_where_storage_lives(tmp_path: Path) -> None:
    path = str(tmp_path / "new.db")
    code, out, _ = run("--db", path, "init")
    assert code == ExitCode.OK
    assert path in out
    assert Path(path).exists()


# --- argument handling ------------------------------------------------------ #


def test_no_command_prints_help() -> None:
    code, out, _ = run()
    assert code == ExitCode.OK
    assert "usage" in out.lower()


def test_a_malformed_env_flag_is_rejected(db: str) -> None:
    code, _, err = run("--db", db, "resume", "run_1", "--env", "dataset")
    assert code == ExitCode.ERROR
    assert "name=version" in err


def test_an_unopenable_database_reports_an_error_not_a_traceback(db: str) -> None:
    """A bad path is an operator mistake; a traceback buries the useful part."""
    code, _, err = run("--db", "/nonexistent-dir/agent.db", "runs")
    assert code == ExitCode.ERROR
    assert "error:" in err
    assert "Traceback" not in err


def test_the_reported_path_is_not_backslash_escaped(tmp_path: Path) -> None:
    """Regression for #94: the operator has to be able to copy the path back.

    ``!r`` escapes each backslash, so a Windows path was reported with every
    separator doubled -- not the path that was passed, and useless pasted into
    a shell or a config file.

    Pinned with a backslash in the *filename*, which is legal on POSIX, so the
    ubuntu-only CI can catch a regression that otherwise only shows on Windows.
    That blind spot is the reason this shipped: #81 was Windows-only for the
    same reason.
    """
    missing = tmp_path / "no-such-dir" / "back\\slash.db"

    code, _, err = run("--db", str(missing), "runs")

    assert code == ExitCode.ERROR
    assert str(missing) in err, "the path reported is not the path that was passed"
    assert "\\\\" not in err, "repr()-style escaping is back"


def test_an_empty_env_version_is_refused(db: str) -> None:
    """`--env dataset=` is nearly always an unexpanded shell variable.

    Accepting it would compare the empty string as a real version and report a
    spurious `v3 -> ` dependency change.
    """
    code, _, err = run("--db", db, "resume", "run_1", "--env", "dataset=")
    assert code == ExitCode.ERROR
    assert "empty version" in err


def test_postgres_url_is_routed_and_fails_clearly(db: str) -> None:
    code, _, err = run("--db", "postgresql://localhost/x", "runs")
    assert code == ExitCode.ERROR
    assert "psycopg" in err or "could not connect" in err


def test_benchmark_runs_and_reports_numbers() -> None:
    code, out, err = run("benchmark", "--total", "20")
    assert code == ExitCode.OK
    assert "continuum" in out
    assert "replay" in out
    assert "naive_checkpoint" in out
    # continuum shows no duplicate work and detects stale environments
    assert "12.93" in out or "16.61" in out or "compress" in out


def test_a_run_with_no_checkpoints_says_so(tmp_path: Path) -> None:
    path = str(tmp_path / "bare.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="bare", goal="g"))
    code, out, _ = run("--db", path, "history", "bare")
    assert code == ExitCode.OK
    assert "No checkpoints recorded" in out


def test_history_of_a_missing_run_is_not_found(db: str) -> None:
    code, _, err = run("--db", db, "history", "ghost")
    assert code == ExitCode.NOT_FOUND
    assert "error:" in err


def test_a_run_with_no_events_reports_not_found(tmp_path: Path) -> None:
    """CheckpointError is surfaced as NOT_FOUND, not as a crash."""
    path = str(tmp_path / "bare.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="bare", goal="g"))
    code, _, err = run("--db", path, "resume", "bare")
    assert code == ExitCode.NOT_FOUND
    assert "no checkpoint and no events" in err


def test_a_corrupted_record_is_reported_as_corruption(db: str) -> None:
    import sqlite3

    raw = sqlite3.connect(db)
    raw.execute("UPDATE runs SET status = 'not_a_status' WHERE run_id = 'run_1'")
    raw.commit()
    raw.close()

    code, _, err = run("--db", db, "runs")
    assert code == ExitCode.CORRUPTED
    assert "integrity error:" in err


def test_repair_records_the_plan_and_does_not_fake_a_safe_exit(db: str) -> None:
    """--repair must persist the repair plan, not merely suppress the hint."""
    before = SQLiteStorage(db).last_sequence("run_1")

    code, out, err = run("--db", db, "resume", "run_1", "--env", "dataset=v4", "--repair")
    assert code == ExitCode.REQUIRES_REPAIR
    assert "Repairs required" in out
    assert "--repair" not in err
    assert "Repair plan recorded" in err

    with SQLiteStorage(db) as store:
        after = store.last_sequence("run_1")
        events = store.read_events("run_1", after_sequence=before)
    assert after > before
    recorded = [e for e in events if e.type == EventType.RECOVERY_STARTED]
    assert recorded, "no RECOVERY_STARTED event was written"
    assert recorded[0].payload["mode"] == "repair_and_resume"
    assert recorded[0].payload["plan"]


def test_resume_without_repair_is_still_read_only(db: str) -> None:
    """Omitting --repair must not write anything, even when repairs are due."""
    before = SQLiteStorage(db).last_sequence("run_1")
    code, _, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v4")
    assert code == ExitCode.REQUIRES_REPAIR
    assert SQLiteStorage(db).last_sequence("run_1") == before


def test_tolerating_unknown_is_opt_in(db: str) -> None:
    interrupt_a_side_effect(db)
    strict, _, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v3")
    lenient, _, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v3", "--tolerate-unknown")
    assert strict == ExitCode.REQUIRES_HUMAN
    assert lenient != ExitCode.OK  # relaxed, but still never "safe"


def test_a_model_switch_can_be_declared(db: str) -> None:
    # No model was ever recorded for this run, so the requested comparison
    # cannot be made. Reporting OK would mean "no drift", which is
    # indistinguishable from a clean check: the fail-open pattern #49 closed for
    # model-specific assumptions, and #308 closes for the model itself. The gap
    # is reported instead, so a caller that explicitly asked for a drift check
    # does not receive a false clean bill of health.
    code, out, _ = run("--db", db, "validate", "run_1", "--env", "dataset=v3", "--model", "model-b")
    # REQUIRES_REPAIR, not REQUIRES_HUMAN: the remedy is to record which model
    # produced the state, which is mechanical rather than a judgement call.
    assert code == ExitCode.REQUIRES_REPAIR
    assert "Run: run_1" in out
    assert "no model recorded" in out


# --- invoked as a real process ---------------------------------------------- #


def _cli(db: str, *argv: str) -> subprocess.CompletedProcess[str]:
    # Inherit the parent environment rather than replacing it. Only PYTHONPATH
    # matters here. It makes the subprocess import continuum from src/ instead
    # of an installed copy. Passing a bare env= drops platform essentials: on
    # Windows, losing SystemRoot leaves the interpreter unable to initialise
    # Winsock, and every spawned process dies during startup on `import
    # _overlapped` long before the CLI is reached.
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "continuum.cli", "--db", db, *argv],
        env=env,
        capture_output=True,
        text=True,
    )


def test_the_cli_runs_as_a_module_without_warnings(db: str) -> None:
    result = _cli(db, "inspect", "run_1")
    assert result.returncode == ExitCode.OK
    assert "60 completed" in result.stdout
    assert "RuntimeWarning" not in result.stderr


def test_the_shell_pipeline_short_circuits_on_unsafe_state(db: str) -> None:
    """The behaviour the whole exit-code design exists to protect."""
    interrupt_a_side_effect(db)
    blocked = _cli(db, "resume", "run_1", "--env", "dataset=v4")
    assert blocked.returncode != 0

    with SQLiteStorage(db) as store:
        reconcile_pending(
            ActionLedger(store, "run_1"),
            ProbeReconciler(lambda a: Resolution(occurred=True, external_id="481")),
        )
    assert _cli(db, "resume", "run_1", "--env", "dataset=v3").returncode == 0


def test_report_and_hint_appear_in_the_right_order(db: str) -> None:
    """stdout is block-buffered when piped; the hint must not overtake the report."""
    result = _cli(db, "resume", "run_1", "--env", "dataset=v4")
    assert "Next permitted action" in result.stdout
    assert "--repair" in result.stderr


# --- replay actually verifies (issue #31) ---------------------------------- #


def test_replay_confirms_the_state_matches_the_stored_version(db: str) -> None:
    """The docstring's promise, now enforced: replay re-derives and compares."""
    code, out, _ = run("--db", db, "replay", "run_1")
    assert code == ExitCode.OK
    assert "Verification: matches stored version" in out


def test_replay_reports_verification_in_json(db: str) -> None:
    code, out, _ = run("--db", db, "--json", "replay", "run_1")
    payload = json.loads(out)
    assert code == ExitCode.OK
    assert payload["verified"] is True
    assert "matches stored version" in payload["verification"]


def test_replay_still_verifies_after_more_work_than_the_last_version(db: str) -> None:
    """Events after the last checkpoint are normal, not corruption.

    The stored version projects a prefix; the log keeps growing. Comparing a
    full replay against it would flag every healthy run mid-flight.
    """
    with SQLiteStorage(db) as store:
        for i in range(60, 70):
            store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})

    code, out, _ = run("--db", db, "replay", "run_1")
    assert code == ExitCode.OK
    assert "matches stored version" in out


def test_replay_detects_a_tampered_stored_version(db: str) -> None:
    """A stored version the events do not project to must not exit 0."""
    with SQLiteStorage(db) as store:
        stored = store.latest_version("run_1")
        assert stored is not None
        # A phantom finding: structurally valid, so the storage layer's own
        # integrity check still passes and only the replay comparison can
        # catch it. (Nudging progress instead trips a model validator, which
        # would be testing SemanticState rather than replay.)
        tampered = stored.model_copy(
            update={
                "findings": [
                    *stored.findings,
                    Finding(finding_id="ghost", claim="never happened"),
                ]
            }
        )
        store.put_version(tampered, reason="tampered")

    code, out, err = run("--db", db, "replay", "run_1")
    assert code == ExitCode.CORRUPTED
    assert "DOES NOT match stored version" in out
    assert "run_1" in err


def test_replay_reports_a_mismatch_in_json(db: str) -> None:
    with SQLiteStorage(db) as store:
        stored = store.latest_version("run_1")
        assert stored is not None
        # A phantom finding: structurally valid, so the storage layer's own
        # integrity check still passes and only the replay comparison can
        # catch it. (Nudging progress instead trips a model validator, which
        # would be testing SemanticState rather than replay.)
        tampered = stored.model_copy(
            update={
                "findings": [
                    *stored.findings,
                    Finding(finding_id="ghost", claim="never happened"),
                ]
            }
        )
        store.put_version(tampered, reason="tampered")

    code, out, _ = run("--db", db, "--json", "replay", "run_1")
    payload = json.loads(out)
    assert code == ExitCode.CORRUPTED
    assert payload["verified"] is False


def test_replay_says_so_when_there_is_no_stored_version(tmp_path: Path) -> None:
    """Unverified must be reported, not silently counted as verified."""
    path = str(tmp_path / "bare.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="bare", goal="G"))
        store.append_event("bare", EventType.RUN_STARTED, {"goal": "G", "total": 1})

    code, out, _ = run("--db", path, "replay", "bare")
    assert code == ExitCode.OK
    assert "skipped (no stored version" in out


def test_replay_verification_is_independent_of_upto(db: str) -> None:
    """--upto narrows what is displayed, not what is verified."""
    code, out, _ = run("--db", db, "replay", "run_1", "--upto", "3")
    assert code == ExitCode.OK
    assert "matches stored version" in out


# --- event-chain attestation ------------------------------------------------ #


def test_attest_keygen_writes_pem_files(tmp_path: Path) -> None:
    priv = tmp_path / "signer.pem"
    code, out, _ = run("attest-keygen", "--out", str(priv))
    assert code == ExitCode.OK
    assert priv.exists()
    assert (tmp_path / "signer.pem.pub").exists()
    assert "PRIVATE KEY" in priv.read_text()


def test_attest_and_verify_round_trip(db: str, tmp_path: Path) -> None:
    from continuum.security.attestation import generate_keypair

    priv_pem, _ = generate_keypair()
    key_file = tmp_path / "signer.pem"
    key_file.write_text(priv_pem)

    attest_file = tmp_path / "run_1.attest.json"
    code, out, _ = run(
        "--db",
        db,
        "attest",
        "run_1",
        "--key",
        str(key_file),
        "--signer",
        "ci-bot",
        "--out",
        str(attest_file),
    )
    assert code == ExitCode.OK
    assert attest_file.exists()

    code, out, _ = run("--db", db, "attest-verify", "run_1", "--attest", str(attest_file))
    assert code == ExitCode.OK
    assert "SIGNED" in out


def test_attest_verify_reports_altered_after_new_event(db: str, tmp_path: Path) -> None:
    from continuum.security.attestation import generate_keypair

    priv_pem, _ = generate_keypair()
    key_file = tmp_path / "signer.pem"
    key_file.write_text(priv_pem)

    attest_file = tmp_path / "run_1.attest.json"
    run(
        "--db",
        db,
        "attest",
        "run_1",
        "--key",
        str(key_file),
        "--signer",
        "ci-bot",
        "--out",
        str(attest_file),
    )
    # A new event after signing shifts the head the attestation did not cover.
    with SQLiteStorage(db) as store:
        store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": 999})

    code, out, _ = run("--db", db, "attest-verify", "run_1", "--attest", str(attest_file))
    assert code == ExitCode.CORRUPTED
    assert "ALTERED" in out


def test_attest_verify_detects_an_in_place_payload_edit(db: str, tmp_path: Path) -> None:
    """An attestation must not pass on content that was rewritten under it.

    The verdict used to compare the signed ``chain_hash`` against the digest
    *stored* in the head row. Editing an event's payload straight through the
    database changes the payload and leaves every ``hash`` column untouched, so
    the head still matched and the verdict read SIGNED, "chain matches", on a run
    whose goal had been rewritten. `continuum verify` caught it in the same
    breath, because it recomputes; attest-verify did not, because it did not.

    This is the attack the signature exists to stop, so it gets its own test:
    appending an event (covered above) moves the head and is easy to notice,
    while an in-place edit is silent and is what an attacker with database access
    would actually do.
    """
    from continuum.security.attestation import generate_keypair

    priv_pem, _ = generate_keypair()
    key_file = tmp_path / "signer.pem"
    key_file.write_text(priv_pem)

    attest_file = tmp_path / "run_1.attest.json"
    code, _, _ = run(
        "--db",
        db,
        "attest",
        "run_1",
        "--key",
        str(key_file),
        "--signer",
        "ci-bot",
        "--out",
        str(attest_file),
    )
    assert code == ExitCode.OK

    head_before = _head_hash(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE events SET payload = ? WHERE run_id = 'run_1' AND sequence = 1",
            (json.dumps({"goal": "rewritten", "total": 100}),),
        )
    # The premise of the bug: the stored head digest is byte-identical, so any
    # check that trusts it cannot see the edit.
    assert _head_hash(db) == head_before

    code, out, _ = run("--db", db, "attest-verify", "run_1", "--attest", str(attest_file))
    assert code == ExitCode.CORRUPTED, f"a tampered chain must not verify: {out}"
    assert "ALTERED" in out
    assert "no longer verifies" in out


def _head_hash(db: str) -> str:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT hash FROM events WHERE run_id = 'run_1' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    return str(row[0])


# --- provenance view (issue #148) ------------------------------------------- #


def test_status_provenance_uses_canonical_labels(db: str) -> None:
    code, out, _ = run("--db", db, "status", "run_1", "--provenance")
    assert code == 0
    # Canonical labels are rendered, not the raw source enums.
    assert "observed" in out and "verified" in out
    assert "DETERMINISTIC" not in out


def test_status_plain_runs(db: str) -> None:
    code, out, _ = run("--db", db, "status", "run_1")
    assert code == 0
    assert "run_1" in out and "goal" in out


def test_status_provenance_json(db: str) -> None:
    code, out, _ = run("--db", db, "--json", "status", "run_1", "--provenance")
    assert code == 0
    data = json.loads(out)
    assert any(row["who"] == "observed" for row in data["provenance"])


# --- scoped recovery CLI smoke (issue #110) --------------------------------- #


def test_validate_is_read_only_and_produces_contract(db: str) -> None:
    # Read-only scoped-recovery entry. Supplying the declared env yields a safe
    # resume; omitting --env still runs without mutating state.
    code, out, _ = run("--db", db, "validate", "run_1", "--env", "dataset=v3")
    assert code == 0
    assert "CONTINUUM RECOVERY" in out
    assert "Recovery decision: RESUME" in out


def test_validate_json_carries_mode(db: str) -> None:
    code, out, _ = run("--db", db, "--json", "validate", "run_1", "--env", "dataset=v3")
    assert code == 0
    data = json.loads(out)
    assert data["mode"] == "resume"
    assert data["safe"] is True


# --- closing a run from the keyboard ------------------------------------------ #


def test_complete_closes_a_run_and_clears_it_from_active_resolution(
    tmp_path: Path,
) -> None:
    """Found missing during live testing: finished runs kept surfacing as the
    active run and hijacked every fresh session's resume."""
    path = str(tmp_path / "c.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="old", goal="finished work"))
    code, out, _ = run("--db", path, "--json", "complete", "old", "--summary", "shipped")
    assert code == ExitCode.OK, out
    payload = json.loads(out)
    assert payload["status"] == "completed"

    with SQLiteStorage(path) as store:
        assert store.get_run("old").status.value == "completed"
        events = [e.type.value for e in store.read_events("old")]
        assert "REVIEW_CONFIRMED" in events and "RUN_COMPLETED" in events
        # A completed run is terminal: it can never be offered for resume.
        assert store.get_active_run() is None


def test_complete_is_idempotent_enough_for_double_clicks(tmp_path: Path) -> None:
    path = str(tmp_path / "d.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="r", goal="g"))
    code, _, err = run("--db", path, "complete", "r")
    assert code == ExitCode.OK
    code, _, err = run("--db", path, "complete", "r")
    assert code == ExitCode.OK, err


def test_complete_unknown_run_is_not_found(tmp_path: Path) -> None:
    path = str(tmp_path / "e.db")
    with SQLiteStorage(path):
        pass
    code, _, err = run("--db", path, "complete", "ghost")
    assert code == ExitCode.NOT_FOUND


# --- verify reports coherence, not only integrity (issue #382) --------------- #


def _poison(path: str) -> None:
    """Append a TASK_UPDATED the fold rejects, through the normal write path.

    Mirrors what #364 allowed before it was fixed: `completed` past the `total`
    already on record. The event is hashed like any other, so the chain stays
    intact and only the projection breaks, which is the whole point here.
    """
    with SQLiteStorage(path) as store:
        store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 999, "failed": 0})


def test_verify_reports_a_run_whose_log_cannot_be_projected(db: str) -> None:
    """`verify` certified a run no projecting command could read (issue #382).

    An unprojectable log is perfectly intact, so the chain audit passes it and is
    right to. Reporting only that verdict meant the one command an operator
    reaches for during an incident was the one that could not see the incident.
    """
    _poison(db)
    code, out, _ = run("--db", db, "verify", "run_1")

    assert "no violations" in out, "the chain really is intact; do not hide that"
    assert "PROJECTION FAILURE" in out
    assert "sequence" in out and "TASK_UPDATED" in out
    assert "exceeds total" in out, "name the constraint, not just the pydantic header"
    assert code == ExitCode.CORRUPTED, "verify $RUN && resume must short-circuit"


def test_verify_still_passes_a_healthy_run(db: str) -> None:
    """The new check must not fail a run that folds; that would be worse."""
    code, out, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.OK
    assert "PROJECTION FAILURE" not in out


def test_verify_json_names_the_offending_event(db: str) -> None:
    """Scripts read the payload, not the prose."""
    _poison(db)
    code, out, _ = run("--db", db, "--json", "verify", "run_1")
    payload = json.loads(out)

    assert payload["ok"] is True, "chain integrity is unaffected"
    assert payload["projectable"] is False
    assert payload["projection_failed_at"]["type"] == "TASK_UPDATED"
    assert payload["projection_failed_at"]["sequence"] > 0
    assert code == ExitCode.CORRUPTED


def test_verify_json_marks_a_healthy_run_projectable(db: str) -> None:
    payload = json.loads(run("--db", db, "--json", "verify", "run_1")[1])
    assert payload["projectable"] is True
    assert "projection_failed_at" not in payload


def test_a_tampered_chain_is_not_also_projected(db: str, tmp_path: Path) -> None:
    """Do not describe events that cannot be trusted to say anything.

    Same reasoning the action-index repair already uses: folding a tampered log
    to report where it stops projecting would launder its contents into a
    diagnosis the operator might act on.
    """
    _poison(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET payload = ? WHERE sequence = 2", ('{"tampered": true}',))

    code, out, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.CORRUPTED
    assert "INTEGRITY FAILURE" in out
    assert "PROJECTION FAILURE" not in out, "integrity comes first; do not fold a tampered log"


def test_first_unprojectable_event_finds_the_earliest_break(db: str) -> None:
    """Two bad events must report the first, or a repair fixes the wrong one."""
    from continuum.state.semantic import first_unprojectable_event

    _poison(db)
    _poison(db)
    with SQLiteStorage(db) as store:
        events = list(store.read_events("run_1"))
        broken = first_unprojectable_event("run_1", events)

    assert broken is not None
    sequence, event_type, reason = broken
    bad = [e.sequence for e in events if e.type is EventType.TASK_UPDATED]
    assert sequence == min(bad)
    assert event_type == "TASK_UPDATED"
    assert "\n" not in reason, "the reason is rendered on one line"


def test_first_unprojectable_event_returns_none_for_a_sound_log(db: str) -> None:
    from continuum.state.semantic import first_unprojectable_event

    with SQLiteStorage(db) as store:
        assert first_unprojectable_event("run_1", store.read_events("run_1")) is None


def test_verify_projects_a_compacted_run_from_its_archive(db: str) -> None:
    """After compaction the live log starts at the anchor (issue #239).

    It no longer contains RUN_STARTED, so folding only the live tail reports
    every compacted run as unprojectable. The archive holds the prefix verbatim
    from sequence 1, so the two streams are folded together, the same merge
    `ActionLedger._replay` does.
    """
    assert run("--db", db, "compact", "run_1", "--force")[0] == ExitCode.OK

    code, out, err = run("--db", db, "verify", "run_1")
    assert code == ExitCode.OK, f"a healthy compacted run must still verify: {out}{err}"
    assert "PROJECTION FAILURE" not in out
    assert json.loads(run("--db", db, "--json", "verify", "run_1")[1])["projectable"] is True


# --- degrade instead of raise (issue #383) ----------------------------------- #


def test_resume_on_an_unprojectable_run_answers_instead_of_crashing(db: str) -> None:
    """resume is the command a crashed session reaches for first; on a poisoned
    log it used to die with the same pydantic traceback as everything else,
    while the action tools kept authorising side effects."""
    _poison(db)
    code, out, err = run("--db", db, "resume", "run_1", "--env", "dataset=v3")

    assert code == ExitCode.REQUIRES_HUMAN, out
    assert "stops folding at sequence" in out, "name where the log stops folding"
    assert "exceeds total" in out, "name the constraint, not just the pydantic header"
    assert "Traceback" not in out and "Traceback" not in err

    payload = json.loads(run("--db", db, "--json", "resume", "run_1", "--env", "dataset=v3")[1])
    assert payload["mode"] == "request_human"
    assert payload["safe"] is False
    assert payload["contract"]["recovery_status"] != "safe_to_resume"


def test_show_contract_on_an_unprojectable_run_carries_the_break(db: str) -> None:
    """The contract is the machine-readable artifact; it must not read clean.

    The prose rationale named the break from day one, but required_actions was
    empty and next_allowed rendered as "continue" over a requires_human
    verdict (#385 review): a caller keying on the structure saw nothing to do.
    """
    _poison(db)
    code, out, err = run("--db", db, "show-contract", "run_1")

    assert code == ExitCode.REQUIRES_HUMAN, out
    assert "repair_log:" in out, "required_actions must name real work"
    assert "next_allowed:      repair_log:" in out
    assert "continue" not in out
    assert "(through sequence " in out, "verified entries are qualified, not unqualified"
    assert "projection (invalid" in out


def test_status_on_an_unprojectable_run_names_the_break_and_fails(db: str) -> None:
    _poison(db)
    code, out, err = run("--db", db, "status", "run_1")

    # Not OK: the figures describe a prefix of the log, and exit 0 would wave
    # a poisoned run through a pipeline.
    assert code == ExitCode.CORRUPTED
    assert "PROJECTION FAILURE" in out
    assert "TASK_UPDATED" in out
    assert "Traceback" not in out and "Traceback" not in err


def test_inspect_on_an_unprojectable_run_reports_the_known_prefix(db: str) -> None:
    _poison(db)
    code, out, _ = run("--db", db, "inspect", "run_1")

    assert code == ExitCode.CORRUPTED
    assert "PROJECTION FAILURE" in out
    assert "0 completed" in out, "prefix figures, not the poisoned ones"


def test_replay_on_an_unprojectable_run_does_not_certify(db: str) -> None:
    _poison(db)
    code, out, err = run("--db", db, "replay", "run_1")

    assert code == ExitCode.CORRUPTED
    assert "PROJECTION FAILURE" in out
    assert "Traceback" not in out and "Traceback" not in err


def test_a_healthy_run_is_unaffected_in_both_modes(db: str) -> None:
    """No poison: status stays a clean exit 0 and resume still answers RESUME."""
    from continuum.state.semantic import project

    with SQLiteStorage(db) as store:
        raised = project("run_1", store.read_events("run_1"))
        degraded = project("run_1", store.read_events("run_1"), on_unprojectable="degrade")

    assert degraded == raised
    assert degraded.status.value == "valid"

    code, out, err = run("--db", db, "status", "run_1")
    assert code == ExitCode.OK, f"{out}{err}"
    assert "PROJECTION FAILURE" not in out

    code, out, err = run("--db", db, "resume", "run_1", "--env", "dataset=v3")
    assert code == ExitCode.OK, f"{out}{err}"
    assert (
        json.loads(run("--db", db, "--json", "resume", "run_1", "--env", "dataset=v3")[1])["mode"]
        == "resume"
    )


def test_a_healthy_compacted_run_still_resumes_after_degrade_landed(db: str) -> None:
    """Regression guard for constraint 5 (compaction).

    After compaction the live log has no RUN_STARTED; restore works only
    because the anchor checkpoint covers the prefix. The degrade wiring must
    not change that, and folding the post-anchor tail must merge nothing less
    than archive+live when it does run.
    """
    assert run("--db", db, "compact", "run_1", "--force")[0] == ExitCode.OK

    code, out, err = run("--db", db, "status", "run_1")
    assert code == ExitCode.OK, f"a healthy compacted run must read cleanly: {out}{err}"
    assert "PROJECTION FAILURE" not in out

    # The anchor checkpoint carries no environment snapshot, so the dataset
    # cannot be re-verified and resume lands on request_human; that is
    # pre-existing behaviour and must not turn into a projection failure.
    code, out, err = run("--db", db, "resume", "run_1", "--env", "dataset=v3")
    assert code in (ExitCode.OK, ExitCode.REQUIRES_HUMAN), f"{out}{err}"
    assert "PROJECTION FAILURE" not in out
    assert "Traceback" not in err
