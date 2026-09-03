"""DOT export and compaction survival for provenance graph (issue #554)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from continuum.events import EventType
from continuum.models import Run
from continuum.provenance.graph import build_provenance_graph, to_dot
from continuum.storage.sqlite import SQLiteStorage


def test_dot_export_contains_origin_colors():
    storage = SQLiteStorage(":memory:")
    run_id = "run_dot"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    dec = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d", "decision_id": "dec1", "caused_by": [ev.event_id]},
    )
    graph = build_provenance_graph(storage.read_events(run_id))
    dot = to_dot(graph)
    assert "digraph provenance" in dot
    assert ev.event_id in dot
    assert dec.event_id in dot
    # Origin colors: deterministic should be lightblue
    assert "lightblue" in dot or "orange" in dot or "lightgreen" in dot
    # Edges
    assert f'"{ev.event_id}" -> "{dec.event_id}"' in dot


def test_graph_survives_compaction_via_archive():
    # Create a run, build graph, compact, reopen, graph still same via read_all_events
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "comp.db"
        storage = SQLiteStorage(str(db))
        run_id = "run_compact"
        storage.create_run_started(Run(run_id=run_id, goal="g"))
        ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
        dec = storage.append_event(
            run_id,
            EventType.DECISION_CREATED,
            {"decision": "d", "decision_id": "dec1", "caused_by": [ev.event_id]},
        )
        storage.append_event(
            run_id,
            EventType.ACTION_RECORDED,
            {"action_type": "send", "action_id": "act1", "caused_by": [dec.event_id]},
        )
        # Need at least one checkpoint for compaction to have something to anchor
        from continuum.state.semantic import project

        state = project(run_id, storage.read_events(run_id))
        storage.put_version(state)
        # Build graph before compact
        graph_before = build_provenance_graph(storage.read_all_events(run_id))
        edges_before = {(p, c) for p, cs in graph_before.edges.items() for c in cs}
        # Compact
        report = storage.compact_run(run_id)
        assert report["archived"] >= 1
        # After compact, live events should be only anchor
        live = storage.read_events(run_id)
        assert any(e.type == EventType.EVENT_LOG_ANCHORED for e in live)
        # But read_all should still have full history
        all_events = storage.read_all_events(run_id)
        graph_after = build_provenance_graph(all_events)
        edges_after = {(p, c) for p, cs in graph_after.edges.items() for c in cs}
        assert edges_before == edges_after
        assert len(graph_after.nodes) == len(graph_before.nodes)
        # Also test that non-CLI graph still works after compact via read_all_events
        storage.close()
        storage2 = SQLiteStorage(str(db))
        graph_cli = build_provenance_graph(storage2.read_all_events(run_id))
        assert len(graph_cli.nodes) == len(graph_before.nodes)
        # Impact via direct API should still work
        from continuum.provenance.graph import downstream_of as _downstream

        ds = _downstream(graph_cli, ev.event_id)
        assert any(n.event_id == dec.event_id for n in ds)
        # DOT export after compact via direct API
        dot_after = to_dot(graph_cli)
        assert "digraph provenance" in dot_after
        assert ev.event_id in dot_after
        storage2.close()
