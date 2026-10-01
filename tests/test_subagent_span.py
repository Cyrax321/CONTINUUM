"""Subagent spanning and context compaction.

Subagent spanning traces delegation chains when a main agent spawns
subagents. Context compaction records platform context-window compaction
boundaries: a PRECOMPACT_HOOK names how many events were summarised, the
validator marks pre-compaction evidence PARTIAL, and provenance exposes the
compactions for audit.
"""

from __future__ import annotations

from continuum.adapters.generic import GenericAgentAdapter
from continuum.events import EventType
from continuum.models import Run, StateStatus, SubagentSpan
from continuum.provenance.graph import build_provenance_graph
from continuum.state.validator import validate_state
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
        adapter.complete_subagent("run_parent", "run_child", success=True, result_summary="Done")

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
        adapter.complete_subagent("run_parent", "run_child", success=True, result_summary="Done")

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


def _evidence_run(store: SQLiteStorage) -> None:
    store.append_event(
        "run_parent", EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "claim": "c1"}
    )
    store.append_event(
        "run_parent", EventType.EVIDENCE_ADDED, {"evidence_id": "ev2", "claim": "c2"}
    )


def test_compact_context_records_event(tmp_path) -> None:
    """GenericAgentAdapter.compact_context records a PRECOMPACT_HOOK event."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.compact_context(
            "run_parent", retained_events=2, compacted_events=1, summary="summarised t1"
        )

        events = store.read_events("run_parent")
        hooks = [e for e in events if e.type == EventType.PRECOMPACT_HOOK]
        assert len(hooks) == 1
        assert hooks[0].payload["retained_events"] == 2
        assert hooks[0].payload["compacted_events"] == 1
        assert hooks[0].payload["summary"] == "summarised t1"


def test_compact_context_without_summary(tmp_path) -> None:
    """compact_context omits the summary key when none is supplied."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        adapter = GenericAgentAdapter(store)
        adapter.compact_context("run_parent", retained_events=1, compacted_events=1)

        events = store.read_events("run_parent")
        hooks = [e for e in events if e.type == EventType.PRECOMPACT_HOOK]
        assert len(hooks) == 1
        assert "summary" not in hooks[0].payload


def test_validator_marks_precompaction_evidence_partial(tmp_path) -> None:
    """Evidence folded before the latest compaction validates as PARTIAL."""
    from continuum.state.semantic import project

    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        _evidence_run(store)
        adapter = GenericAgentAdapter(store)
        adapter.compact_context("run_parent", retained_events=1, compacted_events=2)

        events = list(store.read_events("run_parent"))
        outcome = validate_state(project("run_parent", events), events=events)

        assert not outcome.safe
        partial = [e for e in outcome.report.statuses if e.status is StateStatus.PARTIAL]
        assert {(e.component.value, e.component_id) for e in partial} == {
            ("evidence", "ev1"),
            ("evidence", "ev2"),
        }
        by_id = {e.component_id: e for e in partial}
        assert "context compacted at sequence 4" in by_id["ev1"].detail
        assert "2 event(s) summarised" in by_id["ev1"].detail
        assert outcome.state.evidence[0].status is StateStatus.PARTIAL


def test_validator_skips_compaction_with_bad_counts(tmp_path) -> None:
    """Zero or malformed compaction counts leave evidence verified."""
    from continuum.state.semantic import project

    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        _evidence_run(store)
        store.append_event(
            "run_parent",
            EventType.PRECOMPACT_HOOK,
            {"retained_events": 0, "compacted_events": 0},
        )

        events = list(store.read_events("run_parent"))
        outcome = validate_state(project("run_parent", events), events=events)

        assert outcome.safe
        assert not [e for e in outcome.report.statuses if e.status is StateStatus.PARTIAL]


def test_no_compaction_leaves_evidence_valid(tmp_path) -> None:
    """Without a PRECOMPACT_HOOK, evidence validates as before."""
    from continuum.state.semantic import project

    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        _evidence_run(store)

        events = list(store.read_events("run_parent"))
        outcome = validate_state(project("run_parent", events), events=events)

        assert outcome.safe
        assert all(e.status is StateStatus.VALID for e in outcome.state.evidence)


def test_provenance_graph_exposes_compactions(tmp_path) -> None:
    """build_provenance_graph collects PRECOMPACT_HOOK boundaries."""
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_parent", goal="parent task"))
        _evidence_run(store)
        adapter = GenericAgentAdapter(store)
        adapter.compact_context("run_parent", retained_events=1, compacted_events=2)

        events = store.read_events("run_parent")
        graph = build_provenance_graph(events)
        assert len(graph.compactions) == 1
        assert graph.compactions[0]["sequence"] == 4
        assert graph.compactions[0]["retained_events"] == 1
        assert graph.compactions[0]["compacted_events"] == 2
        assert graph.to_dict()["compactions"] == graph.compactions


def test_partial_status_maps_and_blocks(tmp_path) -> None:
    """PARTIAL maps to UNKNOWN canonically and blocks resume."""
    from continuum.provenance_map import (
        CanonicalProvenance,
        canonical_state_status,
    )

    assert canonical_state_status(StateStatus.PARTIAL) is (CanonicalProvenance.UNKNOWN)
