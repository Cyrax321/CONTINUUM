"""Sleep-time trajectory reports (issue #393)."""

from __future__ import annotations

import json
import os

from continuum.analysis.trajectory_report import (
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
    # The shared derived_label names the weak origin rather than the generic
    # "unverified (derived)" the old hardcoded renderer printed (#1098).
    assert "unverified (derived from external_agent)" in text

    report_machine = report.model_copy(update={"derived_origin": "deterministic"})
    assert "derived from deterministic" in "\n".join(render_trajectory_report(report_machine))
