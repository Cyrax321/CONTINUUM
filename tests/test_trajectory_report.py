"""Trajectory reports (issues #393 and #1427)."""

from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
from typing import Any

import pytest

from continuum.analysis.trajectory_report import (
    TRAJECTORY_REPORT_CAP_BYTES,
    _attempts_in_window,
    _history_window,
    _last_compaction_window,
    _latest_actions,
    _locate_action_type_step,
    _plan_step_spans,
    _stall_sites,
    _top_failure_types,
    _total_attempts,
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
from continuum.models import Action, ActionStatus, Origin, Run, TrajectoryReport
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


def _add_completed_action(storage: SQLiteStorage, run_id: str, action_type: str, key: str) -> None:
    from continuum.actions import ActionLedger

    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim(action_type, {"x": 1}, key=key)
    ledger.complete(outcome.key, result={"ok": True})


def test_stall_sites_ignore_fully_successful_operations() -> None:
    """A successful operation still writes a 'started' ACTION_RECORDED event.

    Counting raw events reported two successful ops of the same type as a
    repeated stall site despite zero failures; folding to the latest action per
    key (as _scar_rate does) fixes it (issue #1345)."""
    storage = _make_storage()
    try:
        _add_completed_action(storage, "run_1", "send_email", "email:m1")
        _add_completed_action(storage, "run_1", "send_email", "email:m2")
        end = storage.last_sequence("run_1")
        report = build_trajectory_report(storage, "run_1", window_start=1, window_end=end)
        assert report.stall_sites == []
    finally:
        storage.close()


def test_a_single_failure_does_not_reach_the_repeated_threshold() -> None:
    """One failed op emits 'started' + 'failed'; the raw-event tally hit the
    >= 2 'repeated' threshold on that alone. After the fold, a lone failure is
    only the single top site, and a genuinely repeated failure elsewhere is what
    the >= 2 threshold selects (issue #1345)."""
    storage = _make_storage()
    try:
        _add_completed_action(storage, "run_1", "send_email", "email:ok")
        _add_failed_action(storage, "run_1", "send_email", "email:once")
        _add_failed_action(storage, "run_1", "charge_card", "card:1")
        _add_failed_action(storage, "run_1", "charge_card", "card:2")
        end = storage.last_sequence("run_1")
        report = build_trajectory_report(storage, "run_1", window_start=1, window_end=end)
        # charge_card failed twice (repeated); send_email failed once, so it is
        # not a repeated stall site and the successful send is never counted.
        assert report.stall_sites == ["charge_card"]
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


def test_a_pre_1462_report_id_is_rerecognised_not_treated_as_tampering() -> None:
    """A report written before the model computed its own digest still names itself.

    #1461 changed the id's basis, so every stored report written before it fails
    ``digest_matches``. That is not corruption: the older basis hashed the run id
    alongside the analytical fields, and it is reproducible from the payload plus
    the run the audit is already scoped to (#1462).
    """
    legacy = TrajectoryReport(
        report_id="4ddbba1cb8b0144c",
        window_start=0,
        window_end=8,
        compaction_seq=8,
        attempts=3,
        scar_rate=0.2,
        stall_sites=["x"],
        top_failure_action_types=["x"],
        derived_origin="deterministic",
    )
    # The id the older derivation actually produced for these fields.
    assert legacy.legacy_digest("run_1")[: len(legacy.report_id)] == legacy.report_id
    assert legacy.digest_matches() is False
    assert legacy.legacy_digest_matches("run_1") is True

    # The older basis hashed the lists sorted, so their fold order does not
    # change the id a stored report carries.
    reordered = legacy.model_copy(
        update={"stall_sites": ["a", "b"], "top_failure_action_types": ["d", "c"]}
    )
    canonical_order = reordered.model_copy(
        update={"stall_sites": ["b", "a"], "top_failure_action_types": ["c", "d"]}
    )
    assert reordered.legacy_digest("run_1") == canonical_order.legacy_digest("run_1")

    # A report whose fields were edited after the fact matches neither basis.
    tampered = legacy.model_copy(update={"scar_rate": 0.0})
    assert tampered.legacy_digest_matches("run_1") is False
    assert tampered.digest_matches() is False


def test_the_byte_cap_cannot_strip_the_lists_the_legacy_id_was_derived_from() -> None:
    """The pre-#1461 cap loop cannot change what a stored report's id names.

    That writer derived the id, then shed trailing list entries in a 2048-byte
    budget loop without recomputing it. The loop is unreachable: the model's own
    field validators bound both lists to five 128-character entries *before* the
    id is computed, so a maximally-sized report still fits the budget with room
    to spare, and the stored lists are always the ones the id was derived from.
    A report at that ceiling still verifies against the older basis.
    """
    biggest = "x" * 128
    ceiling = TrajectoryReport(
        report_id="pending",
        window_start=0,
        window_end=999_999,
        compaction_seq=999_999,
        attempts=1000,
        scar_rate=0.9999,
        stall_sites=[biggest] * 5,
        top_failure_action_types=[biggest] * 5,
        derived_origin="deterministic",
    )
    # The budget the pre-#1461 writer measured, over the fields it carried.
    legacy_payload = {
        k: v
        for k, v in ceiling.model_dump(mode="json").items()
        if k not in ("total_attempts", "uncertain_count")
    }
    assert len(json.dumps(legacy_payload, sort_keys=True).encode()) < TRAJECTORY_REPORT_CAP_BYTES

    # The lists the id was derived from are the lists the report stores.
    expected_id = ceiling.legacy_digest("run_1")[:16]
    ceiling = ceiling.model_copy(update={"report_id": expected_id})
    assert ceiling.legacy_digest_matches("run_1") is True


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


def test_cli_report_reads_a_pre_1462_database_as_legacy_not_corrupt() -> None:
    """A stored report written before #1461 exits OK, not CORRUPTED (#1462).

    The audit used to treat an id that failed the current digest as tampering,
    which reported a corruption that was not there on every database holding a
    report written before the digest became self-computable. Such a report is
    now recognised as older and named separately, and only a report matching
    neither basis still fails the run.
    """
    path, run_id = _populated_db()
    try:
        # A report as the pre-#1461 writer would have persisted it: the fields
        # the older basis hashed, with the id that basis produced for them.
        legacy = TrajectoryReport(
            report_id="4ddbba1cb8b0144c",
            window_start=0,
            window_end=8,
            compaction_seq=8,
            attempts=3,
            scar_rate=0.2,
            stall_sites=["x"],
            top_failure_action_types=["x"],
        )
        storage = SQLiteStorage(path)
        try:
            storage.append_event(
                run_id,
                EventType.TRAJECTORY_REPORT,
                legacy.model_dump(mode="json"),
                source=Origin.HUMAN,
            )
        finally:
            storage.close()

        code, text, _err = _run_cli(path, ["report", "--trajectory", run_id])
        assert code == 0, "an older report is authentic, not an integrity failure"
        assert "fail their own digest check" not in text
        assert "written before #1461" in text

        payload = json.loads(_run_cli(path, ["report", "--trajectory", run_id, "--json"])[1])
        assert payload["unverified_stored_report_ids"] == []
        assert payload["legacy_stored_report_ids"] == ["4ddbba1cb8b0144c"]
        assert payload["stored_report_ids"] == ["4ddbba1cb8b0144c"]

        # A report matching neither basis still fails, so the audit keeps its teeth.
        storage = SQLiteStorage(path)
        try:
            payload = dict(legacy.model_dump(mode="json"))
            payload["report_id"] = "tampered00000"
            storage.append_event(run_id, EventType.TRAJECTORY_REPORT, payload, source=Origin.HUMAN)
        finally:
            storage.close()

        code, text, _err = _run_cli(path, ["report", "--trajectory", run_id])
        assert code == 3
        assert "fail their own digest check" in text
        payload = json.loads(_run_cli(path, ["report", "--trajectory", run_id, "--json"])[1])
        assert payload["unverified_stored_report_ids"] == ["tampered00000"]
        assert payload["legacy_stored_report_ids"] == ["4ddbba1cb8b0144c"]
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


def test_stall_sites_join_to_plan_step_span() -> None:
    """Stall sites name the action type and the plan step whose span contains it (issue #1463)."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        # Plan step 1
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_fetch",
                        "title": "fetch records",
                        "status": "working",
                        "depends_on": [],
                    },
                    {
                        "id": "step_pay",
                        "title": "process payment",
                        "status": "pending",
                        "depends_on": ["step_fetch"],
                    },
                ],
            },
        )
        _add_failed_action(storage, run_id, "fetch_invoice", "inv:1")
        _add_failed_action(storage, run_id, "fetch_invoice", "inv:2")

        # Advance to plan step 2
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_fetch",
                        "title": "fetch records",
                        "status": "done",
                        "depends_on": [],
                    },
                    {
                        "id": "step_pay",
                        "title": "process payment",
                        "status": "working",
                        "depends_on": ["step_fetch"],
                    },
                ],
            },
        )
        _add_failed_action(storage, run_id, "charge_card", "card:1")
        _add_failed_action(storage, run_id, "charge_card", "card:2")

        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert "fetch_invoice@step_fetch" in report.stall_sites
        assert "charge_card@step_pay" in report.stall_sites
    finally:
        storage.close()


def test_stall_sites_fallback_without_plan_records_bare_action_type() -> None:
    """A run without any plan upserted falls back to the bare action type (issue #1463)."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        _add_failed_action(storage, run_id, "fetch_invoice", "inv:1")
        _add_failed_action(storage, run_id, "fetch_invoice", "inv:2")
        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert report.stall_sites == ["fetch_invoice"]
    finally:
        storage.close()


def test_render_trajectory_report_includes_located_stall_sites() -> None:
    """The rendered trajectory report displays located stall sites (issue #1463)."""
    report = TrajectoryReport(
        report_id="rep-1234",
        window_start=1,
        window_end=20,
        compaction_seq=20,
        attempts=4,
        total_attempts=4,
        uncertain_count=0,
        scar_rate=0.5,
        stall_sites=["fetch_invoice@step_fetch", "charge_card@step_pay"],
    )
    lines = render_trajectory_report(report)
    assert any(
        "stall_sites: fetch_invoice@step_fetch, charge_card@step_pay" in line for line in lines
    )


def test_stall_sites_with_partial_plan_upsert_merges() -> None:
    """Partial PLAN_UPSERT merges update cumulative plan state without losing earlier steps."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_1",
                        "title": "first step",
                        "status": "working",
                    },
                    {
                        "id": "step_2",
                        "title": "second step",
                        "status": "pending",
                    },
                ],
            },
        )
        _add_failed_action(storage, run_id, "first_action", "act:1")
        _add_failed_action(storage, run_id, "first_action", "act:2")

        # Partial upsert only marks step_1 done; step_2 is implicitly next (still pending)
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_1",
                        "title": "first step",
                        "status": "done",
                    },
                ],
            },
        )
        _add_failed_action(storage, run_id, "second_action", "act:3")
        _add_failed_action(storage, run_id, "second_action", "act:4")

        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert "first_action@step_1" in report.stall_sites
        assert "second_action@step_2" in report.stall_sites
    finally:
        storage.close()


def test_stall_sites_after_plan_completion_records_bare_action_type() -> None:
    """When a plan marks all units done, subsequent stalls fall back to bare action type."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_1",
                        "title": "only step",
                        "status": "working",
                    },
                ],
            },
        )
        # Advance and complete plan
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_1",
                        "title": "only step",
                        "status": "done",
                    },
                ],
            },
        )
        # Stalling action happens after the plan finished
        _add_failed_action(storage, run_id, "cleanup_task", "clean:1")
        _add_failed_action(storage, run_id, "cleanup_task", "clean:2")

        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert report.stall_sites == ["cleanup_task"]
    finally:
        storage.close()


def test_stall_sites_when_all_remaining_units_blocked() -> None:
    """When all remaining units are blocked, subsequent stalls fall back to bare action type."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {
                        "id": "step_1",
                        "title": "step one",
                        "status": "blocked",
                    },
                ],
            },
        )
        _add_failed_action(storage, run_id, "fallback_task", "task:1")
        _add_failed_action(storage, run_id, "fallback_task", "task:2")

        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert report.stall_sites == ["fallback_task"]
    finally:
        storage.close()


def test_stall_sites_reupsert_unit_without_status_defaults_to_pending() -> None:
    """Re-upserting a unit without status resets it to pending matching projection semantics."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        # Initial upsert: step_a is done, step_b is working
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {"id": "step_a", "title": "A", "status": "done"},
                    {"id": "step_b", "title": "B", "status": "working"},
                ],
            },
        )
        # Re-upsert step_a with no status (resets to pending)
        storage.append_event(
            run_id,
            EventType.PLAN_UPSERT,
            {
                "plan_id": "p1",
                "units": [
                    {"id": "step_a", "title": "A"},
                    {"id": "step_b", "title": "B", "status": "done"},
                ],
            },
        )
        # step_b is done, step_a reset to pending, so step_a becomes active
        _add_failed_action(storage, run_id, "retry_step_a", "act:1")
        _add_failed_action(storage, run_id, "retry_step_a", "act:2")

        end = storage.last_sequence(run_id)
        report = build_trajectory_report(storage, run_id, window_start=1, window_end=end)
        assert "retry_step_a@step_a" in report.stall_sites
    finally:
        storage.close()


def test_plan_step_spans_and_locate_coverage() -> None:
    """Comprehensive branch coverage for _plan_step_spans and _locate_action_type_step."""
    # Empty spans
    assert _locate_action_type_step("act", [], {}, []) is None

    # Events with malformed PLAN_UPSERT payload (non-dict payload, non-list units, non-dict units, empty id)
    ev_malformed_payload = Event.model_construct(
        run_id="r1",
        sequence=1,
        type=EventType.PLAN_UPSERT,
        payload="not-a-dict",
    )
    ev_malformed_units = Event(
        run_id="r1",
        sequence=2,
        type=EventType.PLAN_UPSERT,
        payload={"units": "not-a-list"},
    )
    ev_malformed_unit_entries = Event(
        run_id="r1",
        sequence=3,
        type=EventType.PLAN_UPSERT,
        payload={
            "units": [
                None,
                123,
                {"id": ""},
                {"step_id": "step_alt", "status": "in_progress"},
            ]
        },
    )
    spans = _plan_step_spans([ev_malformed_payload, ev_malformed_units, ev_malformed_unit_entries])
    assert len(spans) == 1
    assert spans[0] == ("step_alt", 3, 3)

    # _plan_step_spans when no transitions occurred
    assert (
        _plan_step_spans([Event(run_id="r1", sequence=1, type=EventType.TASK_UPDATED, payload={})])
        == []
    )

    # _locate_action_type_step when stalling_keys match ACTION_RECONCILED and ACTION_COMPENSATED
    latest_actions = {
        "key1": Action(
            run_id="r1", action_id="a1", action_type="stall_op", status=ActionStatus.FAILED
        ),
    }
    events = [
        Event(run_id="r1", sequence=1, type=EventType.ACTION_RECONCILED, payload={"key": "key1"}),
        Event(run_id="r1", sequence=2, type=EventType.ACTION_COMPENSATED, payload={"key": "key1"}),
    ]
    spans_2 = [("step_x", 1, 5)]
    assert _locate_action_type_step("stall_op", events, latest_actions, spans_2) == "step_x"

    # _locate_action_type_step when payload has no raw_key but has action dict
    events_no_raw_key = [
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.ACTION_RECORDED,
            payload={
                "action": {
                    "run_id": "r1",
                    "action_id": "a2",
                    "action_type": "stall_op",
                    "status": "failed",
                }
            },
        ),
    ]
    assert (
        _locate_action_type_step("stall_op", events_no_raw_key, latest_actions, spans_2) == "step_x"
    )

    # _locate_action_type_step when stalling_keys not matched, fallback loop for ACTION_RECORDED
    events_mismatched_key = [
        Event(run_id="r1", sequence=1, type=EventType.WORK_COMPLETED, payload={}),
        Event(
            run_id="r1",
            sequence=2,
            type=EventType.ACTION_RECORDED,
            payload={
                "key": "other_key",
                "action": {
                    "run_id": "r1",
                    "action_id": "a2",
                    "action_type": "stall_op",
                    "status": "failed",
                },
            },
        ),
    ]
    assert (
        _locate_action_type_step("stall_op", events_mismatched_key, latest_actions, spans_2)
        == "step_x"
    )

    # _locate_action_type_step when action_type is completely missing from events
    assert (
        _locate_action_type_step("unknown_op", events_mismatched_key, latest_actions, spans_2)
        is None
    )

    # _locate_action_type_step when events have non-dict payload
    ev_non_dict_payload = [
        Event.model_construct(
            run_id="r1", sequence=1, type=EventType.ACTION_RECORDED, payload="not-dict"
        ),
    ]
    assert (
        _locate_action_type_step("stall_op", ev_non_dict_payload, latest_actions, spans_2) is None
    )

    # _locate_action_type_step when event sequences fall outside all spans
    assert (
        _locate_action_type_step("stall_op", events, latest_actions, [("step_far", 10, 20)]) is None
    )

    # _stall_sites with an event missing key
    ev_no_key = [
        Event(run_id="r1", sequence=1, type=EventType.ACTION_RECORDED, payload={}),
    ]
    assert _stall_sites(ev_no_key) == []


def test_last_compaction_window_anchor_fallbacks_and_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _make_storage(run_id="run_anchor")
    try:
        # Fallback when read_archived_events raises (lines 494-495)
        monkeypatch.setattr(
            storage,
            "read_archived_events",
            lambda run_id: (_ for _ in ()).throw(RuntimeError("archive read failure")),
        )
        # Event with invalid anchor_sequence falling back to ev.sequence (lines 504-505)
        storage.append_event(
            "run_anchor",
            EventType.EVENT_LOG_ANCHORED,
            {"anchor_sequence": "corrupt_not_an_int"},
            source=Origin.DETERMINISTIC,
        )
        ev_anchored = list(storage.read_events("run_anchor"))[-1]
        assert _last_compaction_window(storage, "run_anchor") == (0, ev_anchored.sequence)

        # Event with None anchor_sequence and unparseable sequence field falling back to ev.sequence
        storage2 = _make_storage(run_id="run_anchor_seq")
        try:
            storage2.append_event(
                "run_anchor_seq",
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": None, "sequence": "also_corrupt"},
                source=Origin.DETERMINISTIC,
            )
            ev2 = list(storage2.read_events("run_anchor_seq"))[-1]
            assert _last_compaction_window(storage2, "run_anchor_seq") == (0, ev2.sequence)
        finally:
            storage2.close()

        # Multi-anchor window: anchors [3, 7] -> (3, 7)
        storage3 = _make_storage(run_id="run_multi")
        try:
            storage3.append_event(
                "run_multi",
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": 3},
                source=Origin.DETERMINISTIC,
            )
            storage3.append_event(
                "run_multi",
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": 7},
                source=Origin.DETERMINISTIC,
            )
            assert _last_compaction_window(storage3, "run_multi") == (3, 7)
        finally:
            storage3.close()

        # Refusal when window_end <= window_start (anchor <= 0)
        storage4 = _make_storage(run_id="run_nonpos")
        try:
            storage4.append_event(
                "run_nonpos",
                EventType.EVENT_LOG_ANCHORED,
                {"anchor_sequence": 0},
                source=Origin.DETERMINISTIC,
            )
            assert _last_compaction_window(storage4, "run_nonpos") is None
        finally:
            storage4.close()

        # No anchors: require_anchor=True returns None (line 508)
        storage5 = _make_storage(run_id="run_no_anchors")
        try:
            assert _last_compaction_window(storage5, "run_no_anchors", require_anchor=True) is None

            # No anchors: require_anchor=False with events present returns (0, window_end) (branch 514->520)
            assert _last_compaction_window(storage5, "run_no_anchors", require_anchor=False) == (
                0,
                1,
            )

            # health_maybe_generate_trajectory_report delegates with require_anchor=True (line 568)
            assert health_maybe_generate_trajectory_report(storage5, "run_no_anchors") is None

            # No anchors: storage.last_sequence raises (lines 512-513)
            monkeypatch.setattr(
                storage5,
                "last_sequence",
                lambda run_id: (_ for _ in ()).throw(RuntimeError("last sequence failure")),
            )
            assert _last_compaction_window(storage5, "run_no_anchors") is None
        finally:
            storage5.close()

        # No anchors: empty run with last_sequence == 0 (line 515)
        storage_empty = SQLiteStorage(":memory:")
        try:
            storage_empty.create_run(Run(run_id="run_empty", goal="g"))
            assert _last_compaction_window(storage_empty, "run_empty") is None
        finally:
            storage_empty.close()
    finally:
        storage.close()


def test_record_trajectory_report_idempotency_and_malformed_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _make_storage(run_id="run_rec")
    try:
        report = TrajectoryReport(
            report_id="test_id_12345678",
            window_start=0,
            window_end=10,
            compaction_seq=10,
            attempts=1,
            total_attempts=1,
            uncertain_count=0,
            scar_rate=0.0,
            stall_sites=[],
            top_failure_action_types=[],
            created_at="2026-01-01T00:00:00Z",
            derived_origin=Origin.DETERMINISTIC.value,
        )

        # 1. Archive-merge fallback when read_archived_events raises (lines 471-472)
        monkeypatch.setattr(
            storage,
            "read_archived_events",
            lambda run_id: (_ for _ in ()).throw(RuntimeError("archive read failure")),
        )
        saved = record_trajectory_report(storage, "run_rec", report)
        assert saved.report_id == report.report_id

        # 2. Malformed prior report payload handling and short-circuit idempotent re-record (lines 478-482)
        storage2 = _make_storage(run_id="run_idempotent")
        try:
            # Append a malformed TRAJECTORY_REPORT event (same window_end, but bad payload)
            storage2.append_event(
                "run_idempotent",
                EventType.TRAJECTORY_REPORT,
                {"window_end": 10, "report_id": 12345},
                source=Origin.DETERMINISTIC,
            )
            # Append a valid TRAJECTORY_REPORT event with matching window_end
            storage2.append_event(
                "run_idempotent",
                EventType.TRAJECTORY_REPORT,
                report.model_dump(mode="json"),
                source=Origin.DETERMINISTIC,
            )

            events_before = list(storage2.read_events("run_idempotent"))
            res = record_trajectory_report(storage2, "run_idempotent", report)
            assert res.report_id == report.report_id
            events_after = list(storage2.read_events("run_idempotent"))
            # Ensure no new event was appended (idempotent short-circuit after skipping malformed)
            assert len(events_after) == len(events_before)
        finally:
            storage2.close()
    finally:
        storage.close()


def test_trajectory_report_shrink_loop_and_digest_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from continuum.analysis import trajectory_report as tr_mod

    storage = _make_storage(run_id="run_shrink")
    try:
        _add_failed_action(storage, "run_shrink", "action_a", "k1")
        _add_failed_action(storage, "run_shrink", "action_a", "k2")
        _add_failed_action(storage, "run_shrink", "action_b", "k3")
        _add_failed_action(storage, "run_shrink", "action_b", "k4")
        end = storage.last_sequence("run_shrink")

        # Cap set to 320: candidate sheds 1 stall site (leaving ['action_a'])
        monkeypatch.setattr(tr_mod, "TRAJECTORY_REPORT_CAP_BYTES", 320)
        rep1 = build_trajectory_report(storage, "run_shrink", 0, end)
        assert rep1.stall_sites == ["action_a"]
        assert rep1.top_failure_action_types == ["action_a", "action_b"]
        assert rep1.report_id == rep1.digest()[:16]

        # Cap set to 300: candidate sheds all stall sites and 1 top failure action type
        monkeypatch.setattr(tr_mod, "TRAJECTORY_REPORT_CAP_BYTES", 300)
        rep2 = build_trajectory_report(storage, "run_shrink", 0, end)
        assert rep2.stall_sites == []
        assert rep2.top_failure_action_types == ["action_a"]
        assert rep2.report_id == rep2.digest()[:16]

        # Cap set to 10: sheds all stall sites and failure types, hits break (line 420)
        monkeypatch.setattr(tr_mod, "TRAJECTORY_REPORT_CAP_BYTES", 10)
        rep3 = build_trajectory_report(storage, "run_shrink", 0, end)
        assert rep3.stall_sites == []
        assert rep3.top_failure_action_types == []
        assert rep3.report_id == rep3.digest()[:16]
    finally:
        storage.close()


def test_maybe_generate_trajectory_report_refusal_paths_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Derived window is None (line 536)
    storage_empty = SQLiteStorage(":memory:")
    try:
        storage_empty.create_run(Run(run_id="r_empty", goal="g"))
        assert maybe_generate_trajectory_report(storage_empty, "r_empty") is None
    finally:
        storage_empty.close()

    # 2. Explicit window with window_end <= window_start (line 540)
    storage = _make_storage(run_id="run_maybe")
    try:
        assert (
            maybe_generate_trajectory_report(storage, "run_maybe", window_start=5, window_end=5)
            is None
        )
        assert (
            maybe_generate_trajectory_report(storage, "run_maybe", window_start=5, window_end=4)
            is None
        )

        # 3. Window with no events in range (line 555)
        assert (
            maybe_generate_trajectory_report(storage, "run_maybe", window_start=50, window_end=60)
            is None
        )

        # 4. Non-quiet window (line 557)
        storage.append_event("run_maybe", EventType.WORK_COMPLETED, {"count": 1, "failed": False})
        assert (
            maybe_generate_trajectory_report(
                storage, "run_maybe", window_start=0, window_end=storage.last_sequence("run_maybe")
            )
            is None
        )

        # 5. Archive-merge fallback when read_archived_events raises (lines 545-546)
        storage2 = _make_storage(run_id="run_quiet")
        try:
            _add_quiet_window_events(storage2, "run_quiet", count=2)
            end = storage2.last_sequence("run_quiet")
            first = True

            def fail_first(run_id: str) -> list[Event]:
                nonlocal first
                if first:
                    first = False
                    raise RuntimeError("archive read failure")
                return []

            monkeypatch.setattr(storage2, "read_archived_events", fail_first)
            report = maybe_generate_trajectory_report(
                storage2, "run_quiet", window_start=0, window_end=end
            )
            assert report is not None
            assert report.window_end == end

            # 6. Prior report with matching window_end fails validation (lines 551-552)
            storage3 = _make_storage(run_id="run_malformed_prior")
            try:
                storage3.append_event(
                    "run_malformed_prior",
                    EventType.TRAJECTORY_REPORT,
                    {"window_end": 10, "scar_rate": "invalid_value"},
                    source=Origin.DETERMINISTIC,
                )
                assert (
                    maybe_generate_trajectory_report(
                        storage3, "run_malformed_prior", window_start=0, window_end=10
                    )
                    is None
                )
            finally:
                storage3.close()
        finally:
            storage2.close()
    finally:
        storage.close()


def test_scan_helpers_parse_and_validation_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. is_quiet_window WORK_COMPLETED unparseable count (lines 54-55)
    ev_work_bad = Event(
        run_id="r",
        sequence=1,
        type=EventType.WORK_COMPLETED,
        payload={"count": "unparseable", "failed": False},
    )
    assert is_quiet_window([ev_work_bad]) is False

    ev_work_bad_failed = Event(
        run_id="r",
        sequence=1,
        type=EventType.WORK_COMPLETED,
        payload={"count": "unparseable", "failed": True},
    )
    assert is_quiet_window([ev_work_bad_failed]) is True

    # is_quiet_window TASK_UPDATED unparseable completed vs zero completed (lines 62-65)
    ev_task_bad = Event(
        run_id="r",
        sequence=1,
        type=EventType.TASK_UPDATED,
        payload={"completed": "unparseable"},
    )
    assert is_quiet_window([ev_task_bad]) is False

    ev_task_zero = Event(
        run_id="r",
        sequence=1,
        type=EventType.TASK_UPDATED,
        payload={"completed": 0},
    )
    assert is_quiet_window([ev_task_zero]) is True

    # 2. _latest_actions payload validation fallback (lines 90-91)
    ev_bad_action = Event(
        run_id="r",
        sequence=1,
        type=EventType.ACTION_RECORDED,
        payload={"key": "k1", "action": "not_a_dict"},
    )
    assert _latest_actions([ev_bad_action]) == {}

    # 3. _total_attempts payload validation fallback (lines 140-141)
    assert _total_attempts([ev_bad_action]) == 0

    # 4. _top_failure_types validation and fallback branches (lines 322-323, 327-328, 334-335)
    assert _top_failure_types([ev_bad_action]) == []

    # TOOL_FAILED without tool_name falling back to action_type (lines 327-328)
    ev_tool_action_type = Event(
        run_id="r",
        sequence=1,
        type=EventType.TOOL_FAILED,
        payload={"action_type": "fallback_tool_type"},
    )
    assert _top_failure_types([ev_tool_action_type]) == ["fallback_tool_type"]

    # TOOL_FAILED without tool_name or action_type falling back to "unknown" (lines 327-328)
    ev_tool_empty = Event(
        run_id="r",
        sequence=1,
        type=EventType.TOOL_FAILED,
        payload={},
    )
    assert _top_failure_types([ev_tool_empty]) == ["unknown"]

    # Secondary loop validation failure and started/unknown fallback (lines 334-335, branch 331->330)
    ev_other = Event(run_id="r", sequence=3, type=EventType.RUN_STARTED, payload={})
    ev_started = Event(
        run_id="r",
        sequence=2,
        type=EventType.ACTION_RECORDED,
        payload={
            "action": Action(
                run_id="r",
                action_id="a1",
                action_type="started_op",
                status=ActionStatus.STARTED,
            ).model_dump(mode="json"),
        },
    )
    assert _top_failure_types([ev_bad_action, ev_other, ev_started]) == ["started_op"]

    # 5. _attempts_in_window recovery markers and keyless events (lines 348, 350-354)
    ev_rec = Event(run_id="r", sequence=1, type=EventType.RECOVERY_STARTED, payload={})
    ev_fork = Event(run_id="r", sequence=2, type=EventType.RUN_FORKED, payload={})
    ev_lesson = Event(run_id="r", sequence=3, type=EventType.ATTEMPT_LESSON, payload={})
    assert _attempts_in_window([ev_rec, ev_fork, ev_lesson]) == 3

    ev_no_key = Event(run_id="r", sequence=1, type=EventType.ACTION_RECORDED, payload={})
    assert _attempts_in_window([ev_no_key]) == 1

    # 6. _history_window non-increasing sequence (branch 441->435) and analyze_trajectory empty (lines 458-459)
    storage = _make_storage(run_id="r_hist")
    try:
        monkeypatch.setattr(
            storage,
            "read_events",
            lambda run_id: [
                Event(run_id="r_hist", sequence=5, type=EventType.RUN_STARTED, payload={}),
                Event(run_id="r_hist", sequence=3, type=EventType.RUN_STARTED, payload={}),
            ],
        )
        assert _history_window(storage, "r_hist") == (0, 5)

        storage_empty = SQLiteStorage(":memory:")
        try:
            storage_empty.create_run(Run(run_id="r_empty", goal="g"))
            assert analyze_trajectory(storage_empty, "r_empty") is None
        finally:
            storage_empty.close()
    finally:
        storage.close()
