"""Provenance DAG projector tests (issue #552)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from continuum.events import EventType
from continuum.models import Origin, Run, StateStatus
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


def _node(event_id: str, etype: EventType, payload: dict[str, object]) -> ProvenanceNode:
    """Build a bare node without going through storage (label tests)."""
    return ProvenanceNode(
        event_id=event_id,
        sequence=1,
        type=etype,
        origin=Origin.DETERMINISTIC,
        status=StateStatus.VALID,
        payload=payload,
    )


def test_node_label_prefers_each_payload_id_in_order():
    """``label`` falls back evidence -> finding -> decision -> action -> event prefix."""
    assert _node("ev-1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"}).label == (
        "EVIDENCE_ADDED:ev1"
    )
    assert _node("f-1", EventType.FINDING_ADDED, {"finding_id": "f1"}).label == ("FINDING_ADDED:f1")
    assert _node("d-1", EventType.DECISION_CREATED, {"decision_id": "dec1"}).label == (
        "DECISION_CREATED:dec1"
    )
    assert _node("a-1", EventType.ACTION_RECORDED, {"action_id": "act1"}).label == (
        "ACTION_RECORDED:act1"
    )


def test_node_label_falls_back_to_the_event_id_prefix():
    """With no known payload id, the label degrades to the first 8 hex chars."""
    node = _node("abcdef0123456789", EventType.EVIDENCE_ADDED, {})
    assert node.label == "EVIDENCE_ADDED:abcdef01"


def test_node_label_cascade_prefers_the_earlier_key():
    """The cascade is ordered: evidence_id wins over finding_id, and so on."""
    assert _node(
        "e-1", EventType.DECISION_CREATED, {"evidence_id": "ev9", "finding_id": "f9"}
    ).label == ("DECISION_CREATED:ev9")
    assert _node(
        "e-1", EventType.DECISION_CREATED, {"decision_id": "dec9", "action_id": "act9"}
    ).label == ("DECISION_CREATED:dec9")


def test_add_edge_and_downstream_ignore_unknown_ids():
    """``add_edge`` silently drops unknown ids; ``downstream`` returns [] for them."""
    graph = ProvenanceGraph()
    graph.add_node(_node("n1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"}))
    graph.add_node(_node("n2", EventType.FINDING_ADDED, {"finding_id": "f1"}))

    # Both ids unknown: no error, no edge recorded.
    graph.add_edge("unknown_parent", "unknown_child")
    assert graph.edges == {"n1": [], "n2": []}
    assert graph.reverse_edges == {"n1": [], "n2": []}

    # Half-known ids are dropped too, in both directions (branch coverage of the
    # ``or`` guard: unknown parent, and unknown child).
    graph.add_edge("unknown_parent", "n2")
    graph.add_edge("n1", "unknown_child")
    assert graph.edges == {"n1": [], "n2": []}
    assert graph.reverse_edges == {"n1": [], "n2": []}

    # A known pair still links, proving the guards did not over-suppress.
    graph.add_edge("n1", "n2")
    assert graph.edges["n1"] == ["n2"]
    assert graph.reverse_edges["n2"] == ["n1"]

    # Re-adding the same edge is idempotent: no duplicate entries either way.
    graph.add_edge("n1", "n2")
    assert graph.edges["n1"] == ["n2"]
    assert graph.reverse_edges["n2"] == ["n1"]

    # downstream of an unknown id returns [] rather than raising.
    assert graph.downstream("unknown_id") == []


def test_subagent_spawn_and_complete_updates_the_span():
    """SUBAGENT_SPAWNED opens a span; SUBAGENT_COMPLETED closes it with a summary."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_subagent_ok"
    storage.create_run(Run(run_id=run_id, goal="delegate"))
    storage.append_event(
        run_id,
        EventType.SUBAGENT_SPAWNED,
        {"subagent_run_id": "sub_run_1", "task_description": "summarize the ledger"},
    )
    storage.append_event(
        run_id,
        EventType.SUBAGENT_COMPLETED,
        {"subagent_run_id": "sub_run_1", "result_summary": "summarized 12 events"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.subagent_spans) == 1
    span = graph.subagent_spans[0]
    assert span["parent_run_id"] == run_id
    assert span["subagent_run_id"] == "sub_run_1"
    assert span["task_description"] == "summarize the ledger"
    assert span["status"] == "completed"
    assert span["result_summary"] == "summarized 12 events"


def test_subagent_spawn_and_fail_marks_the_span_failed():
    """SUBAGENT_FAILED flips the matching span to ``failed`` and keeps the summary."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_subagent_fail"
    storage.create_run(Run(run_id=run_id, goal="delegate"))
    storage.append_event(
        run_id,
        EventType.SUBAGENT_SPAWNED,
        {"subagent_run_id": "sub_run_2", "task_description": "rebase the plan"},
    )
    storage.append_event(
        run_id,
        EventType.SUBAGENT_FAILED,
        {"subagent_run_id": "sub_run_2", "result_summary": "context budget exhausted"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.subagent_spans) == 1
    span = graph.subagent_spans[0]
    assert span["status"] == "failed"
    assert span["result_summary"] == "context budget exhausted"


def test_subagent_completion_without_a_matching_span_is_ignored():
    """A completion for an unspawned subagent must not invent or edit a span."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_subagent_orphan"
    storage.create_run(Run(run_id=run_id, goal="delegate"))
    storage.append_event(
        run_id,
        EventType.SUBAGENT_SPAWNED,
        {"subagent_run_id": "sub_run_active", "task_description": "still running"},
    )
    storage.append_event(
        run_id,
        EventType.SUBAGENT_COMPLETED,
        {"subagent_run_id": "never_spawned", "result_summary": "late to the party"},
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    # The stray completion did not match, so the active span is untouched.
    assert len(graph.subagent_spans) == 1
    span = graph.subagent_spans[0]
    assert span["subagent_run_id"] == "sub_run_active"
    assert span["status"] == "active"
    assert "result_summary" not in span


def test_precompact_hook_records_compaction_metadata():
    """A PRECOMPACT_HOOK event is tracked with its retention accounting."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_compact"
    storage.create_run(Run(run_id=run_id, goal="long horizon"))
    storage.append_event(
        run_id,
        EventType.PRECOMPACT_HOOK,
        {
            "retained_events": 20,
            "compacted_events": 130,
            "summary": "kept the last 20 events and the plan",
        },
    )

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.compactions) == 1
    entry = graph.compactions[0]
    assert entry["sequence"] == 1
    assert entry["retained_events"] == 20
    assert entry["compacted_events"] == 130
    assert entry["summary"] == "kept the last 20 events and the plan"
    assert entry["event_id"]


def test_precompact_hook_defaults_are_none_when_payload_omits_them():
    """Missing accounting keys surface as None rather than raising."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_compact_sparse"
    storage.create_run(Run(run_id=run_id, goal="long horizon"))
    storage.append_event(run_id, EventType.PRECOMPACT_HOOK, {})

    graph = build_provenance_graph(storage.read_events(run_id))

    assert len(graph.compactions) == 1
    entry = graph.compactions[0]
    assert entry["retained_events"] is None
    assert entry["compacted_events"] is None
    assert entry["summary"] is None
