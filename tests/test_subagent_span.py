"""Subagent spanning: trace delegation chains across runs."""

from __future__ import annotations

from continuum.adapters.generic import GenericAgentAdapter
from continuum.events import EventType
from continuum.models import Run, SubagentSpan
from continuum.provenance.graph import build_provenance_graph
from continuum.storage.sqlite import SQLiteStorage


def test_subagent_span_model() -> None:
    """SubagentSpan model can be constructed with required fields."""
    span = SubagentSpan(
        parent_run_id="run_parent",
        subagent_run_id="run_child",
        task_description="Analyze data",
    )
    assert span.parent_run_id == "run_parent"
    assert span.subagent_run_id == "run_child"
    assert span.task_description == "Analyze data"
    assert span.status == "active"
    assert span.completed_at is None
    assert span.result_summary is None


def test_subagent_span_with_completion() -> None:
    """SubagentSpan supports completion fields."""
    span = SubagentSpan(
        parent_run_id="run_parent",
        subagent_run_id="run_child",
        task_description="Analyze data",
        status="completed",
        result_summary="Found 42 results",
    )
    assert span.status == "completed"
    assert span.result_summary == "Found 42 results"


def test_spawn_subagent_records_event(tmp_path) -> None:
    """GenericAgentAdapter.spawn_subagent records a SUBAGENT_SPAWNED event."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")

        events = store.read_events("run_parent")
        spawn_events = [e for e in events if e.type == EventType.SUBAGENT_SPAWNED]
        assert len(spawn_events) == 1
        assert spawn_events[0].payload["subagent_run_id"] == "run_child"
        assert spawn_events[0].payload["task_description"] == "Analyze data"


def test_complete_subagent_success(tmp_path) -> None:
    """GenericAgentAdapter.complete_subagent records SUBAGENT_COMPLETED on success."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        adapter.complete_subagent(
            "run_parent", "run_child", success=True, result_summary="Done"
        )

        events = store.read_events("run_parent")
        completed = [e for e in events if e.type == EventType.SUBAGENT_COMPLETED]
        assert len(completed) == 1
        assert completed[0].payload["subagent_run_id"] == "run_child"
        assert completed[0].payload["result_summary"] == "Done"


def test_complete_subagent_failure(tmp_path) -> None:
    """GenericAgentAdapter.complete_subagent records SUBAGENT_FAILED on failure."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        adapter.complete_subagent(
            "run_parent", "run_child", success=False, result_summary="Timeout"
        )

        events = store.read_events("run_parent")
        failed = [e for e in events if e.type == EventType.SUBAGENT_FAILED]
        assert len(failed) == 1
        assert failed[0].payload["subagent_run_id"] == "run_child"
        assert failed[0].payload["result_summary"] == "Timeout"


def test_provenance_graph_tracks_subagent_spans(tmp_path) -> None:
    """build_provenance_graph collects subagent spans from events."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        adapter.complete_subagent(
            "run_parent", "run_child", success=True, result_summary="Done"
        )

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)
        assert len(graph.subagent_spans) == 1
        assert graph.subagent_spans[0]["subagent_run_id"] == "run_child"
        assert graph.subagent_spans[0]["parent_run_id"] == "run_parent"
        assert graph.subagent_spans[0]["task_description"] == "Analyze data"
        assert graph.subagent_spans[0]["status"] == "completed"
        assert graph.subagent_spans[0]["result_summary"] == "Done"


def test_provenance_json_includes_subagent_spans(tmp_path) -> None:
    """Provenance JSON output includes subagent_spans field."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)
        payload = graph.to_dict()
        assert "subagent_spans" in payload
        assert len(payload["subagent_spans"]) == 1


def test_multiple_subagent_spans(tmp_path) -> None:
    """Multiple subagents can be spawned from the same parent run."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "child_1", "Task A")
        adapter.spawn_subagent("run_parent", "child_2", "Task B")
        adapter.spawn_subagent("run_parent", "child_3", "Task C")

        events = store.read_events("run_parent")
        spawn_events = [e for e in events if e.type == EventType.SUBAGENT_SPAWNED]
        assert len(spawn_events) == 3

        graph = build_provenance_graph(events)
        assert len(graph.subagent_spans) == 3


def test_subagent_span_no_result_summary(tmp_path) -> None:
    """complete_subagent works without a result_summary."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        adapter.complete_subagent("run_parent", "run_child")

        events = store.read_events("run_parent")
        completed = [e for e in events if e.type == EventType.SUBAGENT_COMPLETED]
        assert len(completed) == 1
        assert "result_summary" not in completed[0].payload
