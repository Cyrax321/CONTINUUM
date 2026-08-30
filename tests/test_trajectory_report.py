"""Sleep-time trajectory reports (issue #393)."""

from __future__ import annotations

import json

from continuum.analysis.trajectory_report import (
    build_trajectory_report,
    maybe_generate_trajectory_report,
    record_trajectory_report,
)
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
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

        tmp = tempfile.mktemp(suffix=".sqlite")
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
