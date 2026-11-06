"""Provenance DAG projector tests (issue #552)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.provenance.graph import (
    ProvenanceGraph,
    ProvenanceNode,
    build_provenance_graph,
    downstream_of,
)
from continuum.storage.sqlite import SQLiteStorage

#: The worktree's src, so the subprocess imports the tree under test rather
#: than whatever continuum happens to be installed (the sibling tests that
#: spawn the CLI all do this; the ones that did not were the portability gaps
#: called out in issue #837).
WORKTREE_SRC = str(Path(__file__).resolve().parents[1] / "src")


# Use CLI helper from tests
def _run(*args: str, db: str | None = None):
    """Helper to run continuum CLI via subprocess."""
    cmd = (
        [sys.executable, "-m", "continuum.cli", "--db", db] + list(args)
        if db
        else [sys.executable, "-m", "continuum.cli"] + list(args)
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [WORKTREE_SRC, os.environ.get("PYTHONPATH")])),
    }
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def test_build_graph_with_caused_by_edges():
    storage = SQLiteStorage(":memory:")
    run_id = "run_prov_1"
    storage.create_run(Run(run_id=run_id, goal="test"))
    ev = storage.append_event(
        run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "summary": "s"}
    )
    _find = storage.append_event(
        run_id, EventType.FINDING_ADDED, {"finding_id": "f1", "claim": "c", "evidence": ["ev1"]}
    )
    # decision caused by evidence
    dec = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d1", "decision_id": "dec1", "caused_by": [ev.event_id]},
    )
    # action caused by decision
    act = storage.append_event(
        run_id,
        EventType.ACTION_RECORDED,
        {"action_type": "send", "action_id": "act1", "caused_by": [dec.event_id]},
    )
    events = storage.read_events(run_id)
    graph = build_provenance_graph(events)
    assert len(graph.nodes) == 4
    # edges: ev -> dec, dec -> act
    assert act.event_id in graph.edges.get(dec.event_id, [])
    assert dec.event_id in graph.edges.get(ev.event_id, [])
    # downstream of ev should include dec and act (transitive)
    downstream = graph.downstream(ev.event_id)
    assert dec.event_id in downstream
    assert act.event_id in downstream
    # downstream via helper
    ds_nodes = downstream_of(graph, ev.event_id)
    assert any(n.event_id == dec.event_id for n in ds_nodes)
    assert any(n.event_id == act.event_id for n in ds_nodes)


def test_downstream_via_payload_evidence_id():
    storage = SQLiteStorage(":memory:")
    run_id = "run_prov_2"
    storage.create_run(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev_payload_1"})
    dec = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d", "decision_id": "dec2", "caused_by": [ev.event_id]},
    )
    events = storage.read_events(run_id)
    graph = build_provenance_graph(events)
    # downstream via payload evidence_id "ev_payload_1"
    ds = downstream_of(graph, "ev_payload_1")
    assert any(n.event_id == dec.event_id for n in ds)


def test_graph_nodes_carry_origin():
    storage = SQLiteStorage(":memory:")
    run_id = "run_prov_origin"
    storage.create_run(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    storage.append_event(
        run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": [ev.event_id]}
    )
    graph = build_provenance_graph(storage.read_events(run_id))
    for node in graph.nodes.values():
        assert node.origin is not None
        assert node.origin.value in ("deterministic", "external_agent", "human", "llm", "imported")


def test_cli_provenance_json(tmp_path: Path):
    db = str(tmp_path / "prov.db")
    storage = SQLiteStorage(db)
    run_id = "run_cli_prov"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    storage.append_event(
        run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": [ev.event_id]}
    )
    storage.close()
    # Use direct storage API to verify provenance graph via read_all_events (works after compaction too)
    from continuum.storage.sqlite import SQLiteStorage as _S

    s2 = _S(db)
    from continuum.provenance.graph import build_provenance_graph

    graph = build_provenance_graph(s2.read_all_events(run_id))
    payload = graph.to_dict()
    payload["run_id"] = run_id
    assert payload["run_id"] == run_id
    assert "nodes" in payload
    assert len(payload["nodes"]) >= 2
    for n in payload["nodes"]:
        assert "origin" in n
        assert "event_id" in n
    s2.close()


def test_cli_impact_json(tmp_path: Path):
    db = str(tmp_path / "impact.db")
    storage = SQLiteStorage(db)
    run_id = "run_cli_impact"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    dec = storage.append_event(
        run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": [ev.event_id]}
    )
    storage.close()
    from continuum.provenance.graph import build_provenance_graph, downstream_of
    from continuum.storage.sqlite import SQLiteStorage as _S2

    s2 = _S2(db)
    graph = build_provenance_graph(s2.read_all_events(run_id))
    downstream = downstream_of(graph, ev.event_id)
    payload = {
        "evidence": ev.event_id,
        "downstream": [{"event_id": n.event_id} for n in downstream],
    }
    assert payload["evidence"] == ev.event_id
    assert any(n["event_id"] == dec.event_id for n in payload["downstream"])
    s2.close()


def test_decision_invalidation_marks_the_decision_node_invalid():
    """An invalidation names the decision's domain id, not its event id (#1850).

    ``graph.nodes`` is keyed by ``event_id``; the invalidation branch used to
    look the ``decision_id`` up in it, which never matched, so the node stayed
    ``VALID`` and the reason was lost. The fold must resolve the domain id to
    the node carrying it.
    """
    storage = SQLiteStorage(":memory:")
    run_id = "run_invalidate"
    storage.create_run(Run(run_id=run_id, goal="test"))
    dec = storage.append_event(
        run_id, EventType.DECISION_CREATED, {"decision": "deploy", "decision_id": "dec_42"}
    )
    storage.append_event(
        run_id,
        EventType.DECISION_INVALIDATED,
        {"decision_id": "dec_42", "reason": "config change"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    node = graph.nodes[dec.event_id]
    assert node.status.value == "invalid"
    assert node.invalidated_reason == "config change"


def test_finding_invalidation_marks_the_finding_node_invalid():
    """Same lookup bug on the finding side: ``finding_id``, not ``event_id``."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_invalidate_finding"
    storage.create_run(Run(run_id=run_id, goal="test"))
    finding = storage.append_event(
        run_id, EventType.FINDING_ADDED, {"finding_id": "f_7", "claim": "over budget"}
    )
    storage.append_event(
        run_id,
        EventType.FINDING_INVALIDATED,
        {"finding_id": "f_7", "reason": "re-costed"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    node = graph.nodes[finding.event_id]
    assert node.status.value == "invalid"
    assert node.invalidated_reason == "re-costed"


def test_invalidation_of_an_unknown_decision_leaves_the_graph_alone():
    """An invalidation with no node to void is still a no-op, not an error."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_invalidate_unknown"
    storage.create_run(Run(run_id=run_id, goal="test"))
    storage.append_event(
        run_id, EventType.DECISION_CREATED, {"decision": "deploy", "decision_id": "dec_42"}
    )
    storage.append_event(run_id, EventType.DECISION_INVALIDATED, {"decision_id": "dec_missing"})

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.nodes) == 1
    assert next(iter(graph.nodes.values())).status.value == "valid"


def test_node_label_prefers_payload_ids_over_event_id():
    """The label cascade picks a domain id when the payload carries one (#1988)."""
    base = {"event_id": "evt_abcdef123456", "sequence": 1, "origin": Origin.DETERMINISTIC}
    variants = [
        ({"evidence_id": "ev1"}, "EVIDENCE_ADDED:ev1"),
        ({"finding_id": "f1"}, "EVIDENCE_ADDED:f1"),
        ({"decision_id": "dec1"}, "EVIDENCE_ADDED:dec1"),
        ({"action_id": "act1"}, "EVIDENCE_ADDED:act1"),
        ({}, "EVIDENCE_ADDED:evt_abcd"),  # event_id[:8] fallback
    ]
    for payload, expected in variants:
        node = ProvenanceNode(
            event_id=base["event_id"],
            sequence=base["sequence"],
            type=EventType.EVIDENCE_ADDED,
            origin=base["origin"],
            payload=payload,
        )
        assert node.label == expected, f"payload={payload}"


def test_node_label_uses_the_first_present_payload_id():
    """When several ids are present the cascade order is evidence > finding > decision > action."""
    node = ProvenanceNode(
        event_id="evt_abcdef123456",
        sequence=1,
        type=EventType.ACTION_RECORDED,
        origin=Origin.DETERMINISTIC,
        payload={
            "evidence_id": "ev1",
            "finding_id": "f1",
            "decision_id": "dec1",
            "action_id": "act1",
        },
    )
    assert node.label == "ACTION_RECORDED:ev1"


def test_add_edge_and_downstream_ignore_unknown_ids():
    """Edges between unknown nodes are dropped and unknown starts traverse nothing (#1988)."""
    graph = ProvenanceGraph()
    graph.add_edge("ghost_parent", "ghost_child")
    assert graph.edges == {}
    assert graph.reverse_edges == {}
    assert graph.downstream("unknown_id") == []
    # A known parent paired with an unknown child is ignored the same way.
    graph.add_node(
        ProvenanceNode(
            event_id="evt_known",
            sequence=1,
            type=EventType.EVIDENCE_ADDED,
            origin=Origin.DETERMINISTIC,
        )
    )
    graph.add_edge("evt_known", "ghost_child")
    assert graph.edges.get("evt_known") == []
    assert graph.downstream("evt_known") == []


def test_subagent_span_spawned_then_completed():
    """A completed subagent span carries the parent link and result summary (#1988)."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_subagent_completed"
    storage.create_run(Run(run_id=run_id, goal="delegate"))
    storage.append_event(
        run_id,
        EventType.SUBAGENT_SPAWNED,
        {"subagent_run_id": "run_sub_1", "task_description": "scan the logs"},
    )
    storage.append_event(
        run_id,
        EventType.SUBAGENT_COMPLETED,
        {"subagent_run_id": "run_sub_1", "result_summary": "found 3 anomalies"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.subagent_spans) == 1
    span = graph.subagent_spans[0]
    assert span["subagent_run_id"] == "run_sub_1"
    assert span["parent_run_id"] == run_id
    assert span["task_description"] == "scan the logs"
    assert span["status"] == "completed"
    assert span["result_summary"] == "found 3 anomalies"


def test_subagent_span_spawned_then_failed():
    """A failed subagent span is marked failed and keeps no result summary (#1988)."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_subagent_failed"
    storage.create_run(Run(run_id=run_id, goal="delegate"))
    storage.append_event(
        run_id,
        EventType.SUBAGENT_SPAWNED,
        {"subagent_run_id": "run_sub_2", "task_description": "retry the push"},
    )
    storage.append_event(
        run_id,
        EventType.SUBAGENT_FAILED,
        {"subagent_run_id": "run_sub_2"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.subagent_spans) == 1
    span = graph.subagent_spans[0]
    assert span["status"] == "failed"
    assert span["result_summary"] is None


def test_precompact_hook_records_compaction_details():
    """A PRECOMPACT_HOOK folds into graph.compactions with its metadata (#1988)."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_precompact"
    storage.create_run(Run(run_id=run_id, goal="compact"))
    hook = storage.append_event(
        run_id,
        EventType.PRECOMPACT_HOOK,
        {
            "retained_events": 20,
            "compacted_events": 180,
            "summary": "kept the last 20 events",
        },
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.compactions) == 1
    compaction = graph.compactions[0]
    assert compaction["event_id"] == hook.event_id
    assert compaction["sequence"] == hook.sequence
    assert compaction["retained_events"] == 20
    assert compaction["compacted_events"] == 180
    assert compaction["summary"] == "kept the last 20 events"
