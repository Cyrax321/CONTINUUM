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


# --- the trigger: is_quiet_window (issue #1235) ------------------------------ #


def _event(type: EventType, payload: dict[str, object] | None = None, sequence: int = 1) -> Event:
    """Build a bare, unsealed event for a pure predicate test (no storage)."""
    return Event(run_id="run_1", sequence=sequence, type=type, payload=payload or {})


def test_is_quiet_window_empty_window_is_quiet() -> None:
    """An empty window counts as quiet: nothing happened, so nothing to report on."""
    assert is_quiet_window([]) is True


def test_is_quiet_window_records_only_facts_is_quiet() -> None:
    """Recorded facts that signal neither progress nor a decision keep a window quiet.

    Failed actions, tool calls and tool completions are audit trail: they say
    work was attempted, never that it landed. Only the three progress/decision
    types can break quietness, so a window of pure facts is the quiet case the
    rest of the module reports on.
    """
    events = [
        _event(EventType.TOOL_CALLED, {"tool_name": "edit"}, sequence=1),
        _event(EventType.TOOL_COMPLETED, {"path": "/tmp/x", "sha256": "abc"}, sequence=2),
        _event(EventType.TOOL_FAILED, {"tool_name": "edit"}, sequence=3),
        _event(EventType.EVIDENCE_ADDED, {"kind": "log"}, sequence=4),
        _event(EventType.LIVENESS_SILENCE_DETECTED, {"since": 1}, sequence=5),
        _event(EventType.EVENT_LOG_ANCHORED, {"anchor_sequence": 5}, sequence=6),
    ]
    assert is_quiet_window(events) is True


def test_is_quiet_window_unresolved_and_failed_actions_are_quiet() -> None:
    """A window full of stalled work is exactly the window a trajectory report covers."""
    events = [
        _event(EventType.ACTION_RECORDED, {"key": "k1", "status": "failed"}, sequence=1),
        _event(EventType.ACTION_RECONCILED, {"key": "k1", "status": "failed"}, sequence=2),
        _event(EventType.ACTION_COMPENSATED, {"key": "k2"}, sequence=3),
    ]
    assert is_quiet_window(events) is True


def test_is_quiet_window_work_completed_breaks_quiet() -> None:
    """A completed unit of work is the signal that the window was productive."""
    assert is_quiet_window([_event(EventType.WORK_COMPLETED, {"count": 1})]) is False
    assert is_quiet_window([_event(EventType.WORK_COMPLETED, {"count": 5})]) is False
    # Zero completed units is not progress, and neither is a batch that failed.
    assert is_quiet_window([_event(EventType.WORK_COMPLETED, {"count": 0})]) is True
    assert is_quiet_window([_event(EventType.WORK_COMPLETED, {"count": 3, "failed": True})]) is True
    # A payload that cannot be parsed as a count is treated as one unit of work.
    assert is_quiet_window([_event(EventType.WORK_COMPLETED, {"count": "many"})]) is False
    assert is_quiet_window([_event(EventType.WORK_COMPLETED, {})]) is False


def test_is_quiet_window_task_updated_breaks_quiet() -> None:
    """A task that moved forward is progress; one that did not is not."""
    assert is_quiet_window([_event(EventType.TASK_UPDATED, {"completed": 1})]) is False
    assert is_quiet_window([_event(EventType.TASK_UPDATED, {"completed": 0})]) is True
    # A completion field present but unparseable is not safe to read as zero progress.
    assert is_quiet_window([_event(EventType.TASK_UPDATED, {"completed": "later"})]) is False
    # Absent completion means the update carried no progress claim at all.
    assert is_quiet_window([_event(EventType.TASK_UPDATED, {"title": "t"})]) is True


def test_is_quiet_window_decision_created_breaks_quiet() -> None:
    """A decision is deliberation, so the window was not idle regardless of payload."""
    assert is_quiet_window([_event(EventType.DECISION_CREATED, {})]) is False
    assert (
        is_quiet_window(
            [
                _event(EventType.TOOL_FAILED, {"tool_name": "edit"}, sequence=1),
                _event(EventType.DECISION_CREATED, {"summary": "retry"}, sequence=2),
            ]
        )
        is False
    )


def test_quiet_window_boundaries_are_start_exclusive_end_inclusive() -> None:
    """The window selector reads (start, end], so a busy event at start cannot suppress a report.

    The trigger sees only what the window selector hands it, which makes the
    boundary part of the trigger's contract: the event at ``window_start``
    belonged to the previous window, and the event at ``window_end`` is the
    anchor this report is named for.
    """
    storage = _make_storage()
    try:
        run_id = "run_1"
        storage.append_event(
            run_id,
            EventType.WORK_COMPLETED,
            {"count": 1, "task_id": "busy"},
            source=Origin.DETERMINISTIC,
        )
        storage.append_event(
            run_id,
            EventType.TOOL_FAILED,
            {"tool_name": "edit"},
            source=Origin.DETERMINISTIC,
        )
        busy_seq = 2
        quiet_seq = 3

        # The busy event inside the window suppresses generation.
        assert (
            maybe_generate_trajectory_report(storage, run_id, window_start=1, window_end=quiet_seq)
            is None
        )
        # window_end is inclusive, so a window ending on the busy event is busy.
        assert (
            maybe_generate_trajectory_report(storage, run_id, window_start=1, window_end=busy_seq)
            is None
        )
        # The busy event sits exactly on window_start and is excluded, so the
        # quiet event alone makes this window reportable. Run last: recording
        # this report makes any later lookup of the same window_end dedupe to it.
        report = maybe_generate_trajectory_report(
            storage, run_id, window_start=busy_seq, window_end=quiet_seq
        )
        assert report is not None
        assert report.window_start == busy_seq
        assert report.window_end == quiet_seq
    finally:
        storage.close()


# --- the renderer: render_trajectory_report (issue #1235) --------------------- #


def _stall_and_scar_report(storage: SQLiteStorage, run_id: str) -> TrajectoryReport:
    """Build a report over a window with two stalled actions and one unresolved scar."""
    from continuum.actions import ActionLedger

    for i in range(2):
        _add_failed_action(storage, run_id, "test.stall", f"stall_{i}")
    ledger = ActionLedger(storage, run_id)
    ledger.claim("test.scar", {"y": 1}, key="scar_0")
    end = storage.last_sequence(run_id)
    return build_trajectory_report(storage, run_id, 0, end)


def test_render_trajectory_report_names_top_stall_and_scar_rate() -> None:
    """The rendered lines name the report id, the window, the top stall and the scar rate."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        report = _stall_and_scar_report(storage, run_id)
        assert report.stall_sites
        assert report.scar_rate > 0.0

        lines = render_trajectory_report(report)
        text = "\n".join(lines)

        assert lines[0].startswith("trajectory report ")
        assert report.report_id in lines[0]
        assert f"window {report.window_start}->{report.window_end}" in lines[0]
        assert f"attempts {report.attempts}, scar_rate {report.scar_rate:.2f}" in text
        assert "test.stall" in text
        assert "top failures: test.stall" in text
    finally:
        storage.close()


def test_render_trajectory_report_without_lessons_reports_honest_zero() -> None:
    """A report with no lessons renders what is true rather than an empty string.

    The briefing layer concatenates these lines, so a lesson-less window must
    still yield a header and an explicit zero scar rate instead of vanishing.
    """
    storage = _make_storage()
    try:
        run_id = "run_1"
        report = build_trajectory_report(storage, run_id, 0, storage.last_sequence(run_id))
        assert report.stall_sites == []
        assert report.top_failure_action_types == []
        assert report.scar_rate == 0.0

        lines = render_trajectory_report(report)

        assert lines, "a lesson-less report must still render something"
        text = "\n".join(lines)
        assert report.report_id in text
        assert "scar_rate 0.00" in text
        assert "stall_sites" not in text
        assert "top failures" not in text
    finally:
        storage.close()


def test_render_trajectory_report_flags_self_certified_derivation() -> None:
    """A report derived from agent-asserted events is labelled unverified, not trusted."""
    storage = _make_storage()
    try:
        run_id = "run_1"
        report = _stall_and_scar_report(storage, run_id)
        agent = report.model_copy(update={"derived_origin": Origin.EXTERNAL_AGENT.value})
        llm = report.model_copy(update={"derived_origin": Origin.LLM.value})
        local = report.model_copy(update={"derived_origin": Origin.DETERMINISTIC.value})

        assert "unverified (derived)" in "\n".join(render_trajectory_report(agent))
        assert "unverified (derived)" in "\n".join(render_trajectory_report(llm))
        local_text = "\n".join(render_trajectory_report(local))
        assert "unverified (derived)" not in local_text
        assert f"derived from {Origin.DETERMINISTIC.value}" in local_text
    finally:
        storage.close()
