"""Trajectory reports (issues #393 and #1427)."""

from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
from typing import Any

from continuum.analysis.trajectory_report import (
    analyze_trajectory,
    build_trajectory_report,
    health_maybe_generate_trajectory_report,
    is_quiet_window,
    maybe_generate_trajectory_report,
    record_trajectory_report,
    render_trajectory_report,
)
from continuum.checkpoint import CheckpointManager
from continuum.events import Event, EventType
from continuum.models import Origin, Run, TrajectoryReport
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage


def _make_storage(run_id: str = "run_1") -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return storage


def _add_failed_action(storage: SQLiteStorage, run_id: str, action_type: str, key: str) -> None:
    from continuum.actions import ActionLedger

    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim(action_type, {"x": 1}, key=key)
    ledger.fail(outcome.key, error="failed", certain=True)


def _add_quiet_window_events(storage: SQLiteStorage, run_id: str, count: int = 3) -> None:
    for i in range(count):
        _add_failed_action(storage, run_id, "test.stall", f"k{i}")


def test_after_10_idle_compactions_newest_report_lists_top_stall_and_scar_rate() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        # Simulate 10 idle compaction windows by directly building reports for successive windows
        # Each window is synthetic: we add quiet events and then build a report for that window
        # without relying on CheckpointManager.checkpoint after compaction which would fail due to archived RUN_STARTED
        for window in range(10):
            start = storage.last_sequence(run_id)
            for i in range(3):
                _add_failed_action(storage, run_id, "test.stall", f"w{window}_k{i}")
            from continuum.actions import ActionLedger

            ledger = ActionLedger(storage, run_id)
            ledger.claim("test.scar", {"y": window}, key=f"scar_{window}")
            end = storage.last_sequence(run_id)
            # Simulate a compaction anchor for this window
            storage.append_event(
                run_id,
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": end},
                source=Origin.DETERMINISTIC,
            )
            report = maybe_generate_trajectory_report(
                storage, run_id, window_start=start, window_end=end
            )
            assert report is not None

        from heapq import merge

        all_events = list(
            merge(
                storage.read_archived_events(run_id),
                storage.read_events(run_id),
                key=lambda e: e.sequence,
            )
        )
        state2 = project(run_id, all_events)
        assert len(state2.trajectory_reports) >= 1
        newest = state2.trajectory_reports[-1]
        assert newest.scar_rate >= 0.0
        assert newest.scar_rate <= 1.0
        assert "test.stall" in newest.stall_sites or "test.stall" in newest.top_failure_action_types
        assert newest.top_failure_action_types
        assert newest.top_failure_action_types[0] == "test.stall"
        verify = storage.verify_events(run_id)
        assert verify.ok, f"verify failed: {verify.violations}"
        events = list(storage.read_events(run_id))
        report_events = [e for e in events if e.type is EventType.TRAJECTORY_REPORT]
        assert report_events
        for ev in report_events:
            payload = ev.payload
            assert "report_id" in payload
            assert "scar_rate" in payload
            dumped = json.dumps(payload, sort_keys=True).encode()
            assert len(dumped) < 2048
    finally:
        storage.close()


def test_reports_obey_min_authority_non_amplification() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        storage.append_event(
            run_id,
            EventType.TOOL_COMPLETED,
            {"path": "/tmp/x", "sha256": "abc"},
            source=Origin.EXTERNAL_AGENT,
        )
        _add_quiet_window_events(storage, run_id, count=2)
        CheckpointManager(storage).checkpoint(run_id, trigger="test")
        storage.compact_run(run_id)
        report = maybe_generate_trajectory_report(storage, run_id)
        assert report is not None
        assert report.derived_origin == Origin.EXTERNAL_AGENT.value
        from heapq import merge

        all_events = list(
            merge(
                storage.read_archived_events(run_id),
                storage.read_events(run_id),
                key=lambda e: e.sequence,
            )
        )
        state = project(run_id, all_events)
        assert state.trajectory_reports
        proj_report = state.trajectory_reports[0]
        assert proj_report.derived_origin == Origin.EXTERNAL_AGENT.value

        storage2 = _make_storage(run_id="run_2")
        try:
            storage2.append_event(
                "run_2",
                EventType.TOOL_COMPLETED,
                {"path": "/tmp/y", "sha256": "def"},
                source=Origin.DETERMINISTIC,
            )
            _add_quiet_window_events(storage2, "run_2", count=2)
            CheckpointManager(storage2).checkpoint("run_2", trigger="test")
            storage2.compact_run("run_2")
            report2 = maybe_generate_trajectory_report(storage2, "run_2")
            assert report2 is not None
            assert report2.derived_origin in (Origin.DETERMINISTIC.value, Origin.HUMAN.value)
        finally:
            storage2.close()
    finally:
        storage.close()


def test_zero_overhead_when_quiet_never_occurs() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        for window in range(3):
            start = storage.last_sequence(run_id)
            storage.append_event(
                run_id,
                EventType.WORK_COMPLETED,
                {"count": 1, "task_id": f"t{window}"},
                source=Origin.DETERMINISTIC,
            )
            end = storage.last_sequence(run_id)
            storage.append_event(
                run_id,
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": end},
                source=Origin.DETERMINISTIC,
            )
            report = maybe_generate_trajectory_report(
                storage, run_id, window_start=start, window_end=end
            )
            assert report is None

        from heapq import merge

        all_events = list(
            merge(
                storage.read_archived_events(run_id),
                storage.read_events(run_id),
                key=lambda e: e.sequence,
            )
        )
        state = project(run_id, all_events)
        assert state.trajectory_reports == []
        events = list(storage.read_events(run_id))
        report_events = [e for e in events if e.type is EventType.TRAJECTORY_REPORT]
        assert not report_events
    finally:
        storage.close()


def test_one_report_per_compaction_window_idempotent() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        _add_quiet_window_events(storage, run_id, count=2)
        CheckpointManager(storage).checkpoint(run_id, trigger="test")
        storage.compact_run(run_id)
        report1 = maybe_generate_trajectory_report(storage, run_id)
        assert report1 is not None
        window_end = report1.window_end
        report2 = maybe_generate_trajectory_report(storage, run_id)
        assert report2 is not None
        assert report2.report_id == report1.report_id
        assert report2.window_end == window_end
        events = list(storage.read_events(run_id))
        reports = [
            e
            for e in events
            if e.type is EventType.TRAJECTORY_REPORT and e.payload.get("window_end") == window_end
        ]
        assert len(reports) == 1

        report3 = maybe_generate_trajectory_report(
            storage, run_id, window_start=0, window_end=window_end
        )
        assert report3 is not None
        assert report3.report_id == report1.report_id
    finally:
        storage.close()


def test_bounded_size_per_report() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        for i in range(20):
            _add_failed_action(storage, run_id, f"test.type_{i}", f"key_{i}")
        CheckpointManager(storage).checkpoint(run_id, trigger="test")
        storage.compact_run(run_id)
        report = build_trajectory_report(storage, run_id, 0, storage.last_sequence(run_id))
        assert len(report.stall_sites) <= 5
        assert len(report.top_failure_action_types) <= 3
        dumped = json.dumps(report.model_dump(mode="json"), sort_keys=True).encode()
        assert len(dumped) < 2048
        recorded = record_trajectory_report(storage, run_id, report)
        assert recorded.report_id == report.report_id
    finally:
        storage.close()


def test_digest_auditable_and_briefing_consumption() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        _add_quiet_window_events(storage, run_id, count=2)
        CheckpointManager(storage).checkpoint(run_id, trigger="test")
        storage.compact_run(run_id)
        report = maybe_generate_trajectory_report(storage, run_id)
        assert report is not None
        verify = storage.verify_events(run_id)
        assert verify.ok
        from heapq import merge

        all_events = list(
            merge(
                storage.read_archived_events(run_id),
                storage.read_events(run_id),
                key=lambda e: e.sequence,
            )
        )
        state = project(run_id, all_events)
        assert state.trajectory_reports
        import io
        import pathlib
        import tempfile

        from continuum.cli.main import main as cli_main

        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        file_storage = SQLiteStorage(tmp)
        try:
            file_storage.create_run(Run(run_id=run_id, goal="g"))
            for ev in all_events:
                file_storage.append_event(run_id, ev.type, ev.payload, source=ev.source)
            verify2 = file_storage.verify_events(run_id)
            assert verify2.ok
            out = io.StringIO()
            err = io.StringIO()
            code = cli_main(["--db", tmp, "briefing", "--run-id", run_id], out=out, err=err)
            assert code == 0
            text = out.getvalue()
            assert "trajectory reports" in text.lower() or "trajectory report" in text.lower()
            assert report.report_id in text or str(report.window_end) in text
        finally:
            file_storage.close()
            pathlib.Path(tmp).unlink(missing_ok=True)
    finally:
        storage.close()


def test_synthetic_archive_determinism() -> None:
    def _build_once() -> TrajectoryReport:
        storage = _make_storage()
        try:
            run_id = "run_1"
            for i in range(3):
                _add_failed_action(storage, run_id, "test.stall", f"k{i}")
            CheckpointManager(storage).checkpoint(run_id, trigger="test")
            storage.compact_run(run_id)
            report = build_trajectory_report(storage, run_id, 0, storage.last_sequence(run_id))
            return report
        finally:
            storage.close()

    r1 = _build_once()
    r2 = _build_once()
    assert r1.report_id == r2.report_id
    assert r1.scar_rate == r2.scar_rate
    assert r1.stall_sites == r2.stall_sites
    assert r1.top_failure_action_types == r2.top_failure_action_types


def test_health_idle_trigger_generates_for_quiet_and_not_for_busy() -> None:
    storage = _make_storage()
    try:
        run_id = "run_1"
        prev_end = 0
        for window in range(10):
            for i in range(3):
                _add_failed_action(storage, run_id, "test.stall", f"w{window}_k{i}")
            end = storage.last_sequence(run_id)
            storage.append_event(
                run_id,
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": end},
                source=Origin.DETERMINISTIC,
            )
            report = health_maybe_generate_trajectory_report(storage, run_id)
            assert report is not None, f"window {window} should be quiet and generate"
            assert report.window_end == end
            assert report.window_start == prev_end
            prev_end = end
        events = list(storage.read_events(run_id))
        reports = [e for e in events if e.type is EventType.TRAJECTORY_REPORT]
        assert len(reports) == 10
        storage.append_event(
            run_id,
            EventType.WORK_COMPLETED,
            {"count": 1, "task_id": "busy"},
            source=Origin.DETERMINISTIC,
        )
        end = storage.last_sequence(run_id)
        storage.append_event(
            run_id,
            EventType.EVENT_LOG_ANCHORED,
            {"anchor_sequence": end},
            source=Origin.DETERMINISTIC,
        )
        before = len(
            [e for e in storage.read_events(run_id) if e.type is EventType.TRAJECTORY_REPORT]
        )
        report_busy = health_maybe_generate_trajectory_report(storage, run_id)
        assert report_busy is None
        after = len(
            [e for e in storage.read_events(run_id) if e.type is EventType.TRAJECTORY_REPORT]
        )
        assert after == before
        via_health = health_maybe_generate_trajectory_report(storage, run_id)
        assert via_health is None
        direct = maybe_generate_trajectory_report(storage, run_id)
        assert direct is None
    finally:
        storage.close()


# --- the trigger and the renderer, directly (issue #1235) --------------------
#
# The tests above cover build/record/digest. is_quiet_window decides when the
# module runs at all, and render_trajectory_report is what a human sees; neither
# was named anywhere in the suite, so their own boundaries were unasserted.


def _ev(event_type: EventType, payload: dict | None = None, sequence: int = 1) -> Event:
    return Event(run_id="run_1", sequence=sequence, type=event_type, payload=payload or {})


def test_is_quiet_window_empty_and_non_progressing_events_are_quiet() -> None:
    """Only events that record progress or a decision break the window."""
    assert is_quiet_window([]) is True
    assert is_quiet_window([_ev(EventType.STATE_CHECKPOINTED, {"v": 1})]) is True
    # An observation that carried no work is still quiet.
    assert is_quiet_window([_ev(EventType.WORK_COMPLETED, {"count": 0})]) is True
    # A completed unit that failed does not count as progress.
    assert is_quiet_window([_ev(EventType.WORK_COMPLETED, {"count": 1, "failed": True})]) is True


def test_is_quiet_window_progress_and_decisions_break_it() -> None:
    assert is_quiet_window([_ev(EventType.WORK_COMPLETED, {"count": 1})]) is False
    assert is_quiet_window([_ev(EventType.TASK_UPDATED, {"completed": 3})]) is False
    assert is_quiet_window([_ev(EventType.DECISION_CREATED, {"id": "d1"})]) is False


def test_is_quiet_window_ignores_progress_after_the_first_breaker() -> None:
    """A window is busy as soon as one progress event appears, wherever it sits."""
    quiet_then_busy = [
        _ev(EventType.STATE_CHECKPOINTED, {}, sequence=1),
        _ev(EventType.TASK_UPDATED, {"completed": 1}, sequence=2),
        _ev(EventType.STATE_CHECKPOINTED, {}, sequence=3),
    ]
    assert is_quiet_window(quiet_then_busy) is False


def test_is_quiet_window_rejects_an_unreadable_completed_counter() -> None:
    """A completed field we cannot read as an integer is not evidence of quiet:
    treating it as zero would call a busy window idle."""
    assert is_quiet_window([_ev(EventType.TASK_UPDATED, {"completed": "many"})]) is False
    assert is_quiet_window([_ev(EventType.TASK_UPDATED, {"completed": None})]) is True


def test_render_trajectory_report_names_the_stall_sites_and_scar_rate() -> None:
    """The renderer is the human-facing end of the feature: it must say what the
    report records, not a summary that drops the two fields a reader acts on."""
    storage = _make_storage()
    try:
        for i in range(3):
            _add_failed_action(storage, "run_1", "test.stall", f"k{i}")
        end = storage.last_sequence("run_1")
        report = build_trajectory_report(storage, "run_1", window_start=1, window_end=end)
        lines = render_trajectory_report(report)
        text = "\n".join(lines)
        assert text  # never an empty answer
        assert report.report_id in text
        assert f"window {report.window_start}->{report.window_end}" in text
        # scar_rate is rendered to two decimals, matching the report's rounded value
        assert f"scar_rate {report.scar_rate:.2f}" in text
        # The stall sites are what the reader would act on, so they appear by name.
        for site in report.stall_sites:
            assert site in text
    finally:
        storage.close()


def test_render_trajectory_report_is_honest_when_there_are_no_lessons() -> None:
    """A clean window has no stall sites and no failure types. The header still
    reports the window and the zero rate rather than emitting nothing, which
    would read as 'no data' instead of 'nothing went wrong'."""
    storage = _make_storage()
    try:
        end = storage.last_sequence("run_1")
        report = build_trajectory_report(storage, "run_1", window_start=1, window_end=end)
        assert report.stall_sites == []
        assert report.top_failure_action_types == []
        lines = render_trajectory_report(report)
        assert lines, "a report with no lessons still renders its header"
        text = "\n".join(lines)
        assert report.report_id in text
        assert "scar_rate 0.00" in text
        assert "stall_sites" not in text
        assert "top failures" not in text
    finally:
        storage.close()


def test_render_trajectory_report_labels_a_derived_origin() -> None:
    """The label distinguishes a report the machine distilled from one an agent
    asserted, so a reader knows how much to trust the figures."""
    report = TrajectoryReport(
        report_id="rep-abc",
        window_start=0,
        window_end=9,
        compaction_seq=9,
        attempts=4,
        scar_rate=0.5,
        stall_sites=["fetch.invoice"],
        top_failure_action_types=["fetch.invoice"],
        derived_origin="external_agent",
    )
    text = "\n".join(render_trajectory_report(report))
    assert "unverified (derived from external_agent)" in text

    report_machine = report.model_copy(update={"derived_origin": "deterministic"})
    assert "derived from deterministic" in "\n".join(render_trajectory_report(report_machine))


# --- the on-demand analyser and its counts (issue #1427) --------------------
#
# The quiet-time generator answers "what happened while I was idle". The
# analyser answers the question an operator asks mid-run: how much has this run
# tried, how much is still unsettled, and where does it keep getting stuck. Its
# counts are the contract, so each is pinned to the event shape that produces
# it rather than to a magic number.


def _ledger(storage: SQLiteStorage, run_id: str) -> Any:
    from continuum.actions import ActionLedger

    return ActionLedger(storage, run_id)


def _claim_and_fail(
    storage: SQLiteStorage, run_id: str, action_type: str, key: str, certain: bool = True
) -> None:
    ledger = _ledger(storage, run_id)
    outcome = ledger.claim(action_type, {"x": 1}, key=key)
    ledger.fail(outcome.key, error="failed", certain=certain)


def test_total_attempts_counts_claims_not_settlements() -> None:
    """A claim and its settlement are two events but one attempt.

    The ledger records both the claim and its outcome as ACTION_RECORDED, so
    counting events would double-count every settled action. Only an action
    entering flight is an attempt; re-claiming a failed key is a second one.
    """
    storage = _make_storage()
    try:
        run_id = "run_1"
        ledger = _ledger(storage, run_id)
        for i in range(3):
            outcome = ledger.claim("work.one", {"x": i}, key=f"k{i}")
            ledger.complete(outcome.key, result={"r": i})
        _claim_and_fail(storage, run_id, "work.two", "retried")
        # A second claim on the same failed key is a retry, not a no-op.
        ledger.claim("work.two", {"x": 2}, key="retried")

        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        # Three settled claims, the failed claim, and its retry.
        assert report.total_attempts == 5
        assert report.attempts == 4, "distinct keys, since nothing forked or recovered"
    finally:
        storage.close()


def test_uncertain_count_tracks_side_effects_reconciliation_must_settle() -> None:
    """An action failed without certainty is a side effect still in question.

    Reconciling it settles the question, so the count drops; the report folds
    the whole window and the last word on the key is what it shows.
    """
    storage = _make_storage()
    try:
        run_id = "run_1"
        ledger = _ledger(storage, run_id)
        _claim_and_fail(storage, run_id, "work.certain", "certain_key", certain=True)
        uncertain = ledger.claim("work.uncertain", {"x": 1}, key="uncertain_key")
        ledger.fail(uncertain.key, error="maybe", certain=False)

        end = storage.last_sequence(run_id)
        mid = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert mid.uncertain_count == 1

        ledger.reconcile(uncertain.key, occurred=False, note="probe confirmed absence")
        end2 = storage.last_sequence(run_id)
        settled = build_trajectory_report(storage, run_id, window_start=1, window_end=end2)
        assert settled.uncertain_count == 0
    finally:
        storage.close()


def test_analyze_trajectory_folds_the_archive_after_compaction() -> None:
    """The analyser sees the archived prefix, not just the live log.

    Compaction moves most of a long run out of the live log. An operator
    inspecting that run must still get figures covering the whole history,
    which is the whole point of distilling from the archive.
    """
    storage = _make_storage()
    try:
        run_id = "run_1"
        for i in range(4):
            _claim_and_fail(storage, run_id, "archived.stall", f"k{i}")
        CheckpointManager(storage).checkpoint(run_id, trigger="test")
        storage.compact_run(run_id)
        live_events = len(list(storage.read_events(run_id)))
        archived_events = len(list(storage.read_archived_events(run_id)))
        assert archived_events > live_events, "the fixture must actually compact something"

        outcome = _ledger(storage, run_id).claim("live.work", {"x": 1}, key="live_key")
        _ledger(storage, run_id).complete(outcome.key, result={"r": 1})

        report = analyze_trajectory(storage, run_id)
        assert report is not None
        assert report.window_start == 0
        assert report.total_attempts == 5, "four archived claims plus the live one"
        assert report.scar_rate == 0.0, "every action reached a terminal status"
        assert "archived.stall" in report.stall_sites, "the stall site lives in the archive"
        assert report.digest_matches()
    finally:
        storage.close()


def test_analyze_trajectory_is_none_for_a_run_with_no_events() -> None:
    """A run that was created but never recorded anything has no history to fold."""
    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="bare", goal="g"))
        assert analyze_trajectory(storage, "bare") is None
    finally:
        storage.close()


def test_digest_is_recomputable_from_the_reports_own_fields() -> None:
    """The id is a prefix of a digest the model recomputes, with no outside input.

    That is what makes a stored report auditable: the events it summarises can
    be folded again and the digest compared, without the run id or any other
    context the payload does not carry.
    """
    storage = _make_storage()
    try:
        run_id = "run_1"
        _claim_and_fail(storage, run_id, "work.stall", "k0")
        _claim_and_fail(storage, run_id, "work.stall", "k1", certain=False)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=99)
        assert report.report_id == report.digest()[: len(report.report_id)]
        assert report.digest_matches() is True

        # Hand-editing a field after the fact changes the digest, which is how a
        # tampered or stale report is told apart from a faithful one.
        drifted = report.model_copy(update={"scar_rate": 0.0})
        assert drifted.digest() != report.digest()
        assert drifted.digest_matches() is False
    finally:
        storage.close()


def test_digest_is_stable_across_folds_of_the_same_window() -> None:
    """Two folds of identical events yield identical digests and ids."""

    def _fold_once() -> TrajectoryReport:
        storage = _make_storage()
        try:
            for i in range(3):
                _claim_and_fail(storage, "run_1", "work.stall", f"k{i}")
            return build_trajectory_report(storage, "run_1", window_start=1, window_end=99)
        finally:
            storage.close()

    first = _fold_once()
    second = _fold_once()
    assert first.digest() == second.digest()
    assert first.report_id == second.report_id


def test_old_report_payload_without_the_new_counts_still_loads() -> None:
    """A report written before #1427 has no total_attempts, and must still parse.

    The field defaults rather than being required, so an existing database stays
    readable instead of becoming unprojectable the day the schema widens.
    """
    legacy = TrajectoryReport(
        report_id="legacy1234",
        window_start=0,
        window_end=5,
        compaction_seq=5,
        attempts=2,
        scar_rate=0.5,
    )
    assert legacy.total_attempts == 0
    assert legacy.uncertain_count == 0


# --- the CLI surface (issue #1427) -------------------------------------------


def _run_cli(db_path: str, argv: list[str]) -> tuple[int, str, str]:
    from continuum.cli.main import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    code = cli_main(["--db", db_path, *argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _populated_db() -> tuple[str, str]:
    """A file-backed database with a run that stalled, for CLI inspection."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    storage = SQLiteStorage(path)
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    _claim_and_fail(storage, "run_1", "work.stall", "k0")
    _claim_and_fail(storage, "run_1", "work.stall", "k1")
    _claim_and_fail(storage, "run_1", "work.risky", "k2", certain=False)
    storage.close()
    return path, "run_1"


def test_cli_report_trajectory_names_the_counts_and_the_digest() -> None:
    path, run_id = _populated_db()
    try:
        code, text, err = _run_cli(path, ["report", "--trajectory", run_id])
        assert code == 0, err
        assert "trajectory report" in text
        assert "total claimed 3" in text, "three claims, not three claims plus their settlements"
        assert "uncertain 1" in text, "work.risky failed without certainty"
        assert "scar_rate" in text
        assert "digest" in text
        # No report has been persisted yet, so the CLI says so instead of
        # claiming an audit it never ran.
        assert "no stored report to audit" in text
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_cli_report_trajectory_json_emits_the_full_report() -> None:
    path, run_id = _populated_db()
    try:
        code, text, _err = _run_cli(path, ["report", "--trajectory", run_id, "--json"])
        assert code == 0
        payload = json.loads(text)
        report = payload["trajectory_report"]
        assert report["total_attempts"] == 3
        assert report["uncertain_count"] == 1
        assert payload["stored_report_ids"] == []
        # The digest in the envelope is the one the report recomputes, so a
        # consumer can hold the CLI output against a later fold.
        reloaded = TrajectoryReport.model_validate(report)
        assert payload["digest"] == reloaded.digest()
        assert reloaded.report_id == payload["digest"][: len(reloaded.report_id)]
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_cli_report_audits_the_stored_reports_the_run_persisted() -> None:
    """A stored report that no longer hashes to its own id is reported, not echoed.

    The quiet-time path persists a TRAJECTORY_REPORT per window, and each is
    auditable on its own terms: fold its stored fields again and the digest must
    match. One that does not was edited after it was built, or written by a
    version that hashed different fields; either is an integrity failure, and
    the exit code says so instead of returning OK.
    """
    path, run_id = _populated_db()
    try:
        storage = SQLiteStorage(path)
        try:
            stored = analyze_trajectory(storage, run_id)
            assert stored is not None
            record_trajectory_report(storage, run_id, stored)
        finally:
            storage.close()

        code, text, _err = _run_cli(path, ["report", "--trajectory", run_id])
        assert code == 0
        assert "all verify" in text
        payload = json.loads(_run_cli(path, ["report", "--trajectory", run_id, "--json"])[1])
        assert payload["unverified_stored_report_ids"] == []
        assert payload["stored_report_ids"] == [stored.report_id]

        # Rewrite the stored report with an id its own fields do not hash to.
        storage = SQLiteStorage(path)
        try:
            event = next(
                e for e in storage.read_events(run_id) if e.type is EventType.TRAJECTORY_REPORT
            )
            payload = dict(event.payload)
            payload["report_id"] = "tampered00000"
            storage.append_event(run_id, EventType.TRAJECTORY_REPORT, payload, source=Origin.HUMAN)
        finally:
            storage.close()

        code, text, _err = _run_cli(path, ["report", "--trajectory", run_id])
        assert code == 3, "a report that cannot be traced to its fields is an integrity failure"
        assert "fail their own digest check" in text
        payload = json.loads(_run_cli(path, ["report", "--trajectory", run_id, "--json"])[1])
        assert "tampered00000" in payload["unverified_stored_report_ids"]
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_cli_report_without_a_kind_is_a_usage_error() -> None:
    path, run_id = _populated_db()
    try:
        code, _text, err = _run_cli(path, ["report", run_id])
        assert code == 1
        assert "--trajectory" in err
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_cli_report_unknown_run_is_not_found() -> None:
    path, _run_id = _populated_db()
    try:
        code, _text, err = _run_cli(path, ["report", "--trajectory", "no_such_run"])
        assert code == 2
        assert "no_such_run" in err
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_cli_report_run_with_no_events_says_so() -> None:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        storage = SQLiteStorage(path)
        storage.create_run(Run(run_id="empty", goal="g"))
        storage.close()
        code, text, _err = _run_cli(path, ["report", "--trajectory", "empty"])
        assert code == 0
        assert "nothing to report on" in text
        payload = json.loads(_run_cli(path, ["report", "--trajectory", "empty", "--json"])[1])
        assert payload["trajectory_report"] is None
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
