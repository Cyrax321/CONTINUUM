"""Provenance DAG projector tests (issue #552)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from continuum.events import EventType
from continuum.models import Run, StateStatus
from continuum.provenance.graph import build_provenance_graph, downstream_of
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


def test_invalidated_decision_and_finding_mark_nodes_invalid_with_reason(tmp_path: Path):
    db = str(tmp_path / "invalidation.db")
    storage = SQLiteStorage(db)
    run_id = "run_inv_1"
    storage.create_run(Run(run_id=run_id, goal="test invalidation"))
    dec_event = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision_id": "d1", "decision": "adopt P-42"},
    )
    storage.append_event(
        run_id,
        EventType.DECISION_INVALIDATED,
        {"decision_id": "d1", "reason": "superseded by P-43"},
    )
    find_event = storage.append_event(
        run_id,
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "detected leak"},
    )
    storage.append_event(
        run_id,
        EventType.FINDING_INVALIDATED,
        {"finding_id": "f1", "invalidated_reason": "benign buffer"},
    )
    events = list(storage.read_all_events(run_id))
    storage.close()

    graph = build_provenance_graph(events)
    assert dec_event.event_id in graph.nodes
    dec_node = graph.nodes[dec_event.event_id]
    assert dec_node.status == StateStatus.INVALID
    assert dec_node.invalidated_reason == "superseded by P-43"

    assert find_event.event_id in graph.nodes
    find_node = graph.nodes[find_event.event_id]
    assert find_node.status == StateStatus.INVALID
    assert find_node.invalidated_reason == "benign buffer"

    dict_data = graph.to_dict()
    nodes_by_id = {n["event_id"]: n for n in dict_data["nodes"]}
    assert nodes_by_id[dec_event.event_id]["status"] == "invalid"
    assert nodes_by_id[dec_event.event_id]["invalidated_reason"] == "superseded by P-43"
    assert nodes_by_id[find_event.event_id]["status"] == "invalid"
    assert nodes_by_id[find_event.event_id]["invalidated_reason"] == "benign buffer"

    # Verify CLI --json output reflects the invalid status and reason
    code, out, _ = _run("provenance", run_id, "--json", db=db)
    assert code == 0
    import json

    cli_payload = json.loads(out)
    cli_nodes = {n["event_id"]: n for n in cli_payload["nodes"]}
    assert cli_nodes[dec_event.event_id]["status"] == "invalid"
    assert cli_nodes[dec_event.event_id]["invalidated_reason"] == "superseded by P-43"
    assert cli_nodes[find_event.event_id]["status"] == "invalid"
    assert cli_nodes[find_event.event_id]["invalidated_reason"] == "benign buffer"


def test_synthetic_event_id_invalidation_and_payload_initial_status():
    from continuum.events import Event

    events = [
        Event(
            run_id="r_syn",
            sequence=1,
            type=EventType.DECISION_CREATED,
            event_id="d_syn",
            payload={"decision_id": "d_syn", "decision": "synthetic decision"},
        ),
        Event(
            run_id="r_syn",
            sequence=2,
            type=EventType.DECISION_INVALIDATED,
            event_id="d_inv",
            payload={"decision_id": "d_syn", "reason": "superseded"},
        ),
        Event(
            run_id="r_syn",
            sequence=3,
            type=EventType.FINDING_ADDED,
            event_id="f_syn",
            payload={
                "finding_id": "f_syn",
                "claim": "already invalid finding",
                "status": "invalid",
                "invalidated_reason": "rejected on entry",
            },
        ),
        Event(
            run_id="r_syn",
            sequence=4,
            type=EventType.FINDING_ADDED,
            event_id="f_syn2",
            payload={"finding_id": "f_syn2", "claim": "finding to override"},
        ),
        Event(
            run_id="r_syn",
            sequence=5,
            type=EventType.FINDING_INVALIDATED,
            event_id="f_inv2",
            payload={
                "finding_id": "f_syn2",
                "status": "requires_review",
                "invalidated_reason": "flagged for review",
            },
        ),
        Event(
            run_id="r_syn",
            sequence=6,
            type=EventType.DECISION_INVALIDATED,
            event_id="d_inv_unknown",
            payload={"decision_id": "unknown_decision", "reason": "noop"},
        ),
        Event(
            run_id="r_syn",
            sequence=7,
            type=EventType.FINDING_INVALIDATED,
            event_id="f_inv_no_id",
            payload={"reason": "missing id"},
        ),
    ]

    graph = build_provenance_graph(events)
    assert graph.nodes["d_syn"].status == StateStatus.INVALID
    assert graph.nodes["d_syn"].invalidated_reason == "superseded"
    assert graph.nodes["f_syn"].status == StateStatus.INVALID
    assert graph.nodes["f_syn"].invalidated_reason == "rejected on entry"
    assert graph.nodes["f_syn2"].status == StateStatus.REQUIRES_REVIEW
    assert graph.nodes["f_syn2"].invalidated_reason == "flagged for review"
