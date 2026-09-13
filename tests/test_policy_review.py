"""The advisory policy-review report (issue #743).

The contract under test has three clauses beyond the arithmetic:

* **read-only** - building the report must not touch the log, so a maintainer
  can run it against a live database mid-run;
* **honest** - outcomes the log does not record surface as ``unknown``, never
  as an inference;
* **deterministic** - identical history yields a byte-identical report, so two
  runs a week apart can be diffed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.cli import ExitCode, main
from continuum.events import EventType, Origin
from continuum.models import Run
from continuum.recovery.policy_review import build_policy_review, render_policy_review
from continuum.storage import SQLiteStorage

# Every payload field a RepairStep dump carries in a real RECOVERY_STARTED
# event (cmd_resume writes ``step.model_dump()``).
_STEP = {"blocking": True, "reason": "", "requires_human": False}


def _repair(store: SQLiteStorage, run_id: str, steps: list[dict]) -> None:
    store.append_event(
        run_id, EventType.RECOVERY_STARTED, {"mode": "repair_and_resume", "plan": steps}
    )


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "demo.db")


@pytest.fixture
def seeded(db_path: str) -> str:
    """Two runs with mixed repair kinds, side effects, and human gates."""
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.LLM)
        # Attempt one: a dependency repair that holds, plus a human-gated
        # reconciliation. It ends recorded as completed.
        _repair(
            store,
            "run_1",
            [
                _STEP | {"kind": "revalidate_dependency", "target": "dataset"},
                _STEP | {"kind": "reconcile_action", "target": "act_1", "requires_human": True},
            ],
        )
        store.append_event("run_1", EventType.RECOVERY_COMPLETED, {"mode": "resume"})
        # Attempt two: the same dependency target needs repairing again -
        # drift after an automatic repair - and has no outcome event yet.
        _repair(store, "run_1", [_STEP | {"kind": "revalidate_dependency", "target": "dataset"}])
        # Side effects: one confirmed, one absence-confirmed, one in flight,
        # one completed then compensated.
        ledger = ActionLedger(store, "run_1")
        found = ledger.claim("github.create_issue", {"title": "x"}).action
        ledger.reconcile(found.action_id, occurred=True)
        absent = ledger.claim("github.create_issue", {"title": "y"}).action
        ledger.reconcile(absent.action_id, occurred=False)
        ledger.claim("slack.post", {"channel": "ops"})
        charge = ledger.claim("payments.charge", {"invoice": "1"}).action
        ledger.complete(charge.action_id)
        ledger.compensate(charge.action_id, note="wrong amount")
        store.append_event(
            "run_1",
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "ap_1", "subject": "s"},
            source=Origin.HUMAN,
        )
        store.append_event(
            "run_1",
            EventType.APPROVAL_GRANTED,
            {"approval_id": "ap_1", "subject": "s"},
            source=Origin.HUMAN,
        )
        # A second run whose only history is a single blocked attempt.
        store.create_run(Run(run_id="run_2", goal="h"))
        store.append_event("run_2", EventType.RUN_STARTED, {"goal": "h"}, source=Origin.LLM)
        _repair(store, "run_2", [_STEP | {"kind": "human_review", "target": "policy"}])
        store.append_event("run_2", EventType.RECOVERY_BLOCKED, {"mode": "request_human"})
    return db_path


def _rows(report: dict, section: str) -> dict[str, dict]:
    return {row["action_type"]: row for row in report[section]}


def _run_cli(db: str, *argv: str) -> tuple[int, str, str]:
    import io

    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, *argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# --- the arithmetic ---------------------------------------------------------- #


def test_repairs_group_by_kind_with_outcomes_and_repeats(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        report = build_policy_review(store, "run_1")
    rows = _rows(report, "repair_kinds")
    dep = rows["revalidate_dependency"]
    assert dep["attempts"] == 2
    # First attempt ended completed, the second has no outcome event: unknown,
    # never inferred from the presence of a later attempt.
    assert dep["outcomes"] == {"completed": 1, "blocked": 0, "unknown": 1}
    assert dep["repeated_repairs"] == 1, "same kind+target twice is drift after repair"
    assert rows["reconcile_action"]["human_required"] == 1


def test_blocked_outcomes_count_when_present(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        report = build_policy_review(store, "run_2")
    row = _rows(report, "repair_kinds")["human_review"]
    assert row["outcomes"] == {"completed": 0, "blocked": 1, "unknown": 0}


def test_side_effects_group_by_action_type(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        report = build_policy_review(store, "run_1")
    rows = _rows(report, "side_effect_actions")
    github = rows["github.create_issue"]
    assert github["claims"] == 2
    assert github["reconciled_effect_found"] == 1
    assert github["reconciled_absent"] == 1
    assert github["unsettled"] == 0
    # complete() re-records the action: still one claim, not two.
    payments = rows["payments.charge"]
    assert payments["claims"] == 1 and payments["compensated"] == 1
    assert rows["slack.post"]["unsettled"] == 1


def test_human_gates_counted(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        report = build_policy_review(store, "run_1")
    gates = report["human_gates"]
    assert gates["approval_requested"] == 1 and gates["approval_granted"] == 1
    assert gates["approval_revoked"] == 0
    assert gates["human_required_repairs"] == 1


def test_all_runs_scope_merges_every_run(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        report = build_policy_review(store)
    assert report["window"]["runs"] == 2 and report["window"]["scope"] == "all-runs"
    assert {r["action_type"] for r in report["repair_kinds"]} >= {
        "revalidate_dependency",
        "human_review",
    }
    # run_2's blocked outcome is visible in the merged report too.
    human_review = _rows(report, "repair_kinds")["human_review"]
    assert human_review["outcomes"]["blocked"] == 1


# --- honesty about gaps ------------------------------------------------------- #


def test_absent_signals_are_reported_not_inferred(db_path: str) -> None:
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="r", goal="g"))
        store.append_event("r", EventType.RUN_STARTED, {"goal": "g"})
        report = build_policy_review(store, "r")
    assert report["repair_kinds"] == [] and report["side_effect_actions"] == []
    assert report["human_gates"] == {
        "approval_requested": 0,
        "approval_granted": 0,
        "approval_revoked": 0,
        "human_required_repairs": 0,
    }
    # The notes tell the reader what unknown means instead of hiding it.
    assert any("unknown" in note for note in report["notes"])


def test_empty_storage_is_an_empty_report_not_an_error(db_path: str) -> None:
    with SQLiteStorage(db_path) as store:
        report = build_policy_review(store)
    assert report["window"]["runs"] == 0
    assert report["window"]["first_event_at"] is None


def test_unparseable_plan_steps_are_counted_not_crashed(db_path: str) -> None:
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="r", goal="g"))
        store.append_event("r", EventType.RUN_STARTED, {"goal": "g"})
        store.append_event(
            "r",
            EventType.RECOVERY_STARTED,
            {"mode": "repair_and_resume", "plan": [{"kind": "revalidate_dependency"}, "junk"]},
        )
        report = build_policy_review(store, "r")
    # The readable step is counted; the unreadable one is surfaced, not dropped
    # silently and not guessed at.
    assert _rows(report, "repair_kinds")["revalidate_dependency"]["attempts"] == 1
    assert report["unparsed_plan_steps"] == 1


# --- compaction survival ------------------------------------------------------ #


def test_compaction_does_not_lose_the_report(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        before = build_policy_review(store, "run_1")
        # Push everything but the tail into the archive.
        result = store.compact_run("run_1", through_sequence=8)
        assert result["archived"] > 0
        after = build_policy_review(store, "run_1")
        # An archived fact is still a recorded fact (#239): the aggregates
        # are unchanged. Only the compact_survival split moves - that is what
        # compaction is - and it moves toward "archived", proving survival.
        for section in ("repair_kinds", "side_effect_actions"):
            for before_row, after_row in zip(
                _rows(before, section).values(), _rows(after, section).values(), strict=True
            ):
                assert {k: v for k, v in after_row.items() if k != "compact_survival"} == {
                    k: v for k, v in before_row.items() if k != "compact_survival"
                }
    # And the rows prove the survival: their events now live in the archive.
    dep = _rows(after, "repair_kinds")["revalidate_dependency"]
    assert dep["compact_survival"]["archived"] > 0
    assert dep["compact_survival"]["survived_compaction"] is True
    assert after["window"]["archived_events"] > 0


def test_history_split_across_archive_and_live_still_reports(seeded: str) -> None:
    """Archived-only history: the report reads the whole recorded past.

    The compacted prefix is only reachable through the archive; the attempt
    row must survive with its outcome, and prove it came from the archive.
    """
    with SQLiteStorage(seeded) as store:
        # run_2: seq1 RUN_STARTED (the anchor), seq2 RECOVERY_STARTED,
        # seq3 RECOVERY_BLOCKED. Archive everything past the anchor.
        result = store.compact_run("run_2", through_sequence=2)
        assert result["archived"] >= 1
        report = build_policy_review(store, "run_2")
    row = _rows(report, "repair_kinds")["human_review"]
    assert row["attempts"] == 1 and row["outcomes"]["blocked"] == 1
    assert row["compact_survival"]["archived"] >= 1
    assert row["compact_survival"]["survived_compaction"] is True


# --- determinism and read-only ----------------------------------------------- #


def test_report_is_deterministic(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        first = build_policy_review(store)
        second = build_policy_review(store)
    assert first == second
    assert [r["action_type"] for r in first["repair_kinds"]] == sorted(
        r["action_type"] for r in first["repair_kinds"]
    )
    assert [r["action_type"] for r in first["side_effect_actions"]] == sorted(
        r["action_type"] for r in first["side_effect_actions"]
    )


def test_running_the_report_is_read_only(seeded: str) -> None:
    # Event-level proof: if the report wrote anything, the log fold or the
    # chain verification would see it.
    with SQLiteStorage(seeded) as store:
        before_events = [e.model_dump(mode="json") for e in store.read_all_events("run_1")]
        build_policy_review(store)
        build_policy_review(store, "run_1")
        after_events = [e.model_dump(mode="json") for e in store.read_all_events("run_1")]
        integrity = store.verify_events("run_1")
    assert before_events == after_events
    assert integrity.ok, [v.kind for v in integrity.violations]


def test_report_never_feeds_the_engine(seeded: str) -> None:
    # The advisory clause: assessing before and after a report must produce
    # the identical decision, byte for byte.
    from continuum.recovery.engine import RecoveryEngine

    with SQLiteStorage(seeded) as store:
        engine = RecoveryEngine(store)
        before = engine.assess("run_1")
        build_policy_review(store, "run_1")
        after = engine.assess("run_1")
    assert before.mode == after.mode
    # Volatile per-assessment fields (created_at, liveness age) differ by
    # construction; the decision itself must be identical.
    volatile = {"created_at", "liveness", "integrity_hash", "post_checkpoint_observations"}
    assert before.contract.model_dump(mode="json", exclude=volatile) == after.contract.model_dump(
        mode="json", exclude=volatile
    )


# --- CLI surface --------------------------------------------------------------- #


def test_cli_text_output(seeded: str) -> None:
    code, out, err = _run_cli(seeded, "policy-review", "run_1")
    assert code == ExitCode.OK, err
    assert "repair attempts by action type" in out
    assert "revalidate_dependency" in out and "github.create_issue" in out
    assert "never feeds plan_repairs" in out


def test_cli_json_output_is_the_report(seeded: str) -> None:
    code, out, _ = _run_cli(seeded, "--json", "policy-review")
    assert code == ExitCode.OK
    payload = json.loads(out)
    assert payload["report"] == "policy-review" and payload["advisory"] is True
    assert payload["window"]["scope"] == "all-runs"
    with SQLiteStorage(seeded) as store:
        assert payload == build_policy_review(store)


def test_render_is_stable_text(seeded: str) -> None:
    with SQLiteStorage(seeded) as store:
        report = build_policy_review(store, "run_1")
        lines = render_policy_review(report)
        assert lines == render_policy_review(build_policy_review(store, "run_1"))
    assert lines[0] == "Advisory policy review (read-only; never feeds plan_repairs)"
