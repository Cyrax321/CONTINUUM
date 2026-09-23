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


def test_subagent_span_frozen_model() -> None:
    """SubagentSpan is frozen and immutable."""
    from pydantic import ValidationError

    span = SubagentSpan(
        parent_run_id="run_parent",
        subagent_run_id="run_child",
        task_description="Analyze data",
    )
    try:
        span.status = "completed"  # type: ignore[misc]
        raise AssertionError("Expected frozen model to raise on mutation")
    except ValidationError:
        pass  # Expected - frozen model raises ValidationError


def test_subagent_failure_no_summary(tmp_path) -> None:
    """complete_subagent failure without result_summary."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        adapter.complete_subagent("run_parent", "run_child", success=False)

        events = store.read_events("run_parent")
        failed = [e for e in events if e.type == EventType.SUBAGENT_FAILED]
        assert len(failed) == 1
        assert "result_summary" not in failed[0].payload


def test_provenance_graph_mixed_events(tmp_path) -> None:
    """Subagent spans coexist with regular provenance nodes."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        store.append_event(
            "run_parent",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": "ev1", "claim": "test evidence"},
        )
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        store.append_event(
            "run_parent",
            EventType.DECISION_CREATED,
            {"decision_id": "d1", "decision": "test decision", "caused_by": []},
        )

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)

        assert len(graph.nodes) == 2  # evidence + decision
        assert len(graph.subagent_spans) == 1
        assert graph.subagent_spans[0]["subagent_run_id"] == "run_child"


def test_subagent_events_not_in_nodes(tmp_path) -> None:
    """Subagent events don't become provenance graph nodes."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")
        adapter.complete_subagent("run_parent", "run_child", success=True)

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)

        for node in graph.nodes.values():
            assert node.type != EventType.SUBAGENT_SPAWNED
            assert node.type != EventType.SUBAGENT_COMPLETED


def test_base_adapter_noop_methods() -> None:
    """Base adapter no-op methods don't crash."""
    from continuum.adapters.base import AgentAdapter

    assert hasattr(AgentAdapter, "spawn_subagent")
    assert hasattr(AgentAdapter, "complete_subagent")


def test_provenance_dot_with_subagent_spans(tmp_path) -> None:
    """Provenance DOT output works when subagent spans exist."""
    from continuum.provenance.graph import to_dot

    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Analyze data")

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)
        dot = to_dot(graph)
        assert "digraph" in dot


def test_subagent_span_with_special_characters(tmp_path) -> None:
    """SubagentSpan handles special characters in task description."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        task = "Task with 'quotes' and \"double quotes\" and\nnewlines"
        adapter.spawn_subagent("run_parent", "run_child", task)

        events = store.read_events("run_parent")
        spawn_events = [e for e in events if e.type == EventType.SUBAGENT_SPAWNED]
        assert len(spawn_events) == 1
        assert spawn_events[0].payload["task_description"] == task


def test_spawn_then_fail(tmp_path) -> None:
    """A subagent can be spawned and then marked as failed."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.spawn_subagent("run_parent", "run_child", "Task")
        adapter.complete_subagent(
            "run_parent", "run_child", success=False, result_summary="Out of memory"
        )

        events = store.read_events("run_parent")
        spawned = [e for e in events if e.type == EventType.SUBAGENT_SPAWNED]
        failed = [e for e in events if e.type == EventType.SUBAGENT_FAILED]
        completed = [e for e in events if e.type == EventType.SUBAGENT_COMPLETED]

        assert len(spawned) == 1
        assert len(failed) == 1
        assert len(completed) == 0


def test_multiple_parent_runs(tmp_path) -> None:
    """Different parent runs can spawn subagents independently."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="parent_1", goal="task 1"))
        store.create_run_started(Run(run_id="parent_2", goal="task 2"))
        adapter = GenericAgentAdapter(store)

        adapter.spawn_subagent("parent_1", "child_a", "Task A")
        adapter.spawn_subagent("parent_2", "child_b", "Task B")

        events_1 = store.read_events("parent_1")
        events_2 = store.read_events("parent_2")

        spans_1 = [e for e in events_1 if e.type == EventType.SUBAGENT_SPAWNED]
        spans_2 = [e for e in events_2 if e.type == EventType.SUBAGENT_SPAWNED]

        assert len(spans_1) == 1
        assert len(spans_2) == 1
        assert spans_1[0].payload["subagent_run_id"] == "child_a"
        assert spans_2[0].payload["subagent_run_id"] == "child_b"


def test_graph_with_no_subagent_spans(tmp_path) -> None:
    """Provenance graph with no subagent spans returns empty list."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        store.append_event(
            "run_parent",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": "ev1", "claim": "test"},
        )

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)
        assert graph.subagent_spans == []
        assert graph.to_dict()["subagent_spans"] == []
