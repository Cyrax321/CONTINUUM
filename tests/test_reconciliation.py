from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.actions import (
    ActionLedger,
    AssumeNotOccurredReconciler,
    ManualReconciler,
    ProbeReconciler,
    Resolution,
    reconcile_pending,
    unresolved_actions,
)
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield storage
    storage.close()


@pytest.fixture
def ledger(store: SQLiteStorage) -> ActionLedger:
    return ActionLedger(store, "run_1")


def interrupted(ledger: ActionLedger, action_type: str = "github.create_issue") -> None:
    """Leave an action in flight, as a crash would."""
    outcome = ledger.claim(action_type, {"title": "Bug"})
    ledger.fail(outcome.key, "process died", certain=False)


# --- probing --------------------------------------------------------------- #


def test_a_probe_that_finds_the_effect_marks_it_completed(ledger: ActionLedger) -> None:
    interrupted(ledger)
    probe = ProbeReconciler(lambda a: Resolution(occurred=True, external_id="481"))

    report = reconcile_pending(ledger, probe)
    assert report.complete
    assert report.resolved_completed == ("github.create_issue",)
    assert not ledger.pending()
    assert not ledger.claim("github.create_issue", {"title": "Bug"}).fresh


def test_a_probe_that_finds_nothing_permits_a_retry(ledger: ActionLedger) -> None:
    interrupted(ledger)
    probe = ProbeReconciler(lambda a: Resolution(occurred=False, note="no such issue"))

    report = reconcile_pending(ledger, probe)
    assert report.resolved_failed == ("github.create_issue",)
    assert ledger.claim("github.create_issue", {"title": "Bug"}).fresh


def test_an_unreachable_probe_is_not_evidence_of_absence(ledger: ActionLedger) -> None:
    """The most dangerous mistake: treating a failed check as a clean bill."""
    interrupted(ledger)

    def unreachable(action: Action) -> Resolution:
        raise ConnectionError("api down")

    probe = ProbeReconciler(unreachable)
    report = reconcile_pending(ledger, probe)

    assert not report.complete
    assert report.unresolved == ("github.create_issue",)
    assert isinstance(probe.last_error, ConnectionError)
    assert unresolved_actions(ledger)[0].status is ActionStatus.REQUIRES_REVIEW


def test_a_probe_may_decline_to_decide(ledger: ActionLedger) -> None:
    interrupted(ledger)
    report = reconcile_pending(ledger, ProbeReconciler(lambda a: None))
    assert report.unresolved == ("github.create_issue",)


def test_the_probe_receives_the_recorded_action(ledger: ActionLedger) -> None:
    interrupted(ledger)
    seen: list[Action] = []

    reconcile_pending(
        ledger,
        ProbeReconciler(lambda a: seen.append(a) or Resolution(occurred=False)),
    )
    assert seen[0].action_type == "github.create_issue"
    assert seen[0].arguments == {"title": "Bug"}


# --- assuming, carefully --------------------------------------------------- #


def test_assuming_not_occurred_requires_asserting_idempotency() -> None:
    """The unsafe default must be impossible to reach by reflex."""
    with pytest.raises(ValueError, match="only safe for idempotent"):
        AssumeNotOccurredReconciler(idempotent=False)


def test_an_idempotent_operation_may_be_retried(ledger: ActionLedger) -> None:
    interrupted(ledger, "s3.put_object")
    report = reconcile_pending(ledger, AssumeNotOccurredReconciler(idempotent=True))

    assert report.resolved_failed == ("s3.put_object",)
    assert ledger.claim("s3.put_object", {"title": "Bug"}).fresh


def test_there_is_no_assume_occurred_strategy() -> None:
    """Assuming success without evidence silently drops work."""
    import continuum.actions.reconciliation as module

    assert not any("AssumeOccurred" in name for name in dir(module))


# --- escalation ------------------------------------------------------------ #


def test_manual_reconciliation_never_resolves_on_its_own(ledger: ActionLedger) -> None:
    interrupted(ledger, "payment.charge")
    report = reconcile_pending(ledger, ManualReconciler())

    assert not report.complete
    assert "STILL UNKNOWN" in report.render()
    assert unresolved_actions(ledger)[0].status is ActionStatus.REQUIRES_REVIEW


def test_no_reconciler_at_all_flags_rather_than_guesses(ledger: ActionLedger) -> None:
    interrupted(ledger)
    report = reconcile_pending(ledger)
    assert report.unresolved == ("github.create_issue",)


# --- per-type strategies --------------------------------------------------- #


def test_different_action_types_get_different_strategies(ledger: ActionLedger) -> None:
    """A file upload can be retried; a payment cannot."""
    interrupted(ledger, "s3.put_object")
    interrupted(ledger, "payment.charge")

    report = reconcile_pending(
        ledger,
        {
            "s3.put_object": AssumeNotOccurredReconciler(idempotent=True),
            "payment.charge": ManualReconciler(),
        },
    )
    assert report.resolved_failed == ("s3.put_object",)
    assert report.unresolved == ("payment.charge",)


def test_a_default_covers_unlisted_types(ledger: ActionLedger) -> None:
    interrupted(ledger, "unlisted.action")
    report = reconcile_pending(
        ledger,
        {"other.action": ManualReconciler()},
        default=ProbeReconciler(lambda a: Resolution(occurred=True, external_id="x")),
    )
    assert report.resolved_completed == ("unlisted.action",)


def test_nothing_to_reconcile_reports_cleanly(ledger: ActionLedger) -> None:
    report = reconcile_pending(ledger, ManualReconciler())
    assert report.complete
    assert report.render() == "nothing to reconcile"


def test_the_report_renders_all_three_outcomes(ledger: ActionLedger) -> None:
    interrupted(ledger, "a.confirmed")
    interrupted(ledger, "b.absent")
    interrupted(ledger, "c.unknown")

    report = reconcile_pending(
        ledger,
        {
            "a.confirmed": ProbeReconciler(lambda x: Resolution(occurred=True)),
            "b.absent": ProbeReconciler(lambda x: Resolution(occurred=False)),
            "c.unknown": ManualReconciler(),
        },
    )
    rendered = report.render()
    assert "confirmed as performed: a.confirmed" in rendered
    assert "confirmed as not performed: b.absent" in rendered
    assert "STILL UNKNOWN" in rendered


# --- the real thing: a crash between effect and record --------------------- #


_WORKER = """
import os, sys
from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Run, UnknownSideEffect
from continuum.storage import SQLiteStorage

db, side_effects, mode = sys.argv[1], sys.argv[2], sys.argv[3]
store = SQLiteStorage(db)
try:
    store.get_run("run_1")
except Exception:
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

ledger = ActionLedger(store, "run_1")

def perform():
    # the "external system": an append-only file we can count afterwards
    with open(side_effects, "a") as fh:
        fh.write("issue-created\\n")
    return "481"

if mode == "crash":
    outcome = ledger.claim("github.create_issue", {"title": "Bug"})
    if outcome.fresh:
        external_id = perform()
        os._exit(9)          # died after the effect, before recording it
else:
    try:
        outcome = ledger.claim("github.create_issue", {"title": "Bug"})
    except UnknownSideEffect as exc:
        print("REFUSED_BLIND_RETRY")
        sys.exit(0)
    if outcome.fresh:
        external_id = perform()
        ledger.complete(outcome.key, external_id=external_id)
        print("PERFORMED")
    else:
        print(f"SKIPPED_DUPLICATE external_id={outcome.external_id}")
"""


def _run_worker(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "worker.py"
    script.write_text(textwrap.dedent(_WORKER))
    return subprocess.run(
        [
            sys.executable,
            str(script),
            str(tmp_path / "agent.db"),
            str(tmp_path / "effects.log"),
            mode,
        ],
        # Inherit the parent environment; only PYTHONPATH is added, so the
        # worker imports continuum from src/. A bare env= drops SystemRoot on
        # Windows and the worker dies on `import _overlapped` during startup.
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True,
        text=True,
    )


def test_a_crash_after_the_effect_does_not_silently_duplicate_it(
    tmp_path: Path,
) -> None:
    """The exactly-once gap, exercised with real processes and a real side effect."""
    crashed = _run_worker(tmp_path, "crash")
    assert crashed.returncode == 9

    effects = (tmp_path / "effects.log").read_text().splitlines()
    assert effects == ["issue-created"]  # the effect really happened

    resumed = _run_worker(tmp_path, "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert "REFUSED_BLIND_RETRY" in resumed.stdout

    # the crucial assertion: no second issue was created
    effects = (tmp_path / "effects.log").read_text().splitlines()
    assert effects == ["issue-created"]


def test_after_reconciliation_the_run_continues_without_duplicating(
    tmp_path: Path,
) -> None:
    _run_worker(tmp_path, "crash")

    with SQLiteStorage(tmp_path / "agent.db") as store:
        ledger = ActionLedger(store, "run_1")
        assert len(ledger.pending()) == 1

        # a probe confirms the issue exists in the external system
        report = reconcile_pending(
            ledger, ProbeReconciler(lambda a: Resolution(occurred=True, external_id="481"))
        )
        assert report.complete

    resumed = _run_worker(tmp_path, "resume")
    assert "SKIPPED_DUPLICATE external_id=481" in resumed.stdout
    assert (tmp_path / "effects.log").read_text().splitlines() == ["issue-created"]


def test_a_clean_run_performs_the_effect_exactly_once(tmp_path: Path) -> None:
    first = _run_worker(tmp_path, "resume")
    assert "PERFORMED" in first.stdout

    second = _run_worker(tmp_path, "resume")
    assert "SKIPPED_DUPLICATE" in second.stdout
    assert (tmp_path / "effects.log").read_text().splitlines() == ["issue-created"]
