"""Provenance DAG projector for claim-level causal graph (issue #552).

Builds a read-only DAG from events where DECISION_CREATED and ACTION_RECORDED
carry ``caused_by: list[event_id]`` edges. Nodes are hash-chained events of
types EVIDENCE_ADDED, FINDING_ADDED, DECISION_CREATED, ACTION_RECORDED.
Edges are caller-declared, never inferred. The graph is a pure fold over
events, so it survives compaction when the caller supplies archived rows.

Each node carries per-node Origin (DETERMINISTIC, EXTERNAL_AGENT, HUMAN) and
a best-effort StateStatus (VALID unless the event payload itself says otherwise).
Staleness propagation (issue #553) will later mark downstream nodes via BFS.
"""

from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any

from continuum.events import EventType
from continuum.models import Origin, StateStatus

__all__ = [
    "ProvenanceNode",
    "ProvenanceGraph",
    "build_provenance_graph",
    "downstream_of",
    "to_dot",
]


#: Event types that become graph nodes. Others are ignored for the DAG view.
_NODE_TYPES = frozenset(
    {
        EventType.EVIDENCE_ADDED,
        EventType.FINDING_ADDED,
        EventType.DECISION_CREATED,
        EventType.ACTION_RECORDED,
    }
)


@dataclass(frozen=True, slots=True)
class ProvenanceNode:
    """One node in the provenance DAG."""

    event_id: str
    sequence: int
    type: EventType
    origin: Origin
    status: StateStatus = StateStatus.VALID
    invalidated_reason: str | None = None
    caused_by: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Human label for CLI rendering."""
        pid = (
            self.payload.get("evidence_id")
            or self.payload.get("finding_id")
            or self.payload.get("decision_id")
            or self.payload.get("action_id")
            or self.event_id[:8]
        )
        return f"{self.type.value}:{pid}"


@dataclass(slots=True)
class ProvenanceGraph:
    """Directed acyclic graph of provenance events."""

    nodes: dict[str, ProvenanceNode] = field(default_factory=dict)
    edges: dict[str, list[str]] = field(default_factory=dict)
    reverse_edges: dict[str, list[str]] = field(default_factory=dict)
    subagent_spans: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)

    def add_node(self, node: ProvenanceNode) -> None:
        """Insert a node, initializing its edge lists."""
        self.nodes[node.event_id] = node
        self.edges.setdefault(node.event_id, [])
        self.reverse_edges.setdefault(node.event_id, [])

    def add_edge(self, parent_id: str, child_id: str) -> None:
        """Link parent to child; unknown ids are ignored."""
        if parent_id not in self.nodes or child_id not in self.nodes:
            return
        if child_id not in self.edges.get(parent_id, []):
            self.edges.setdefault(parent_id, []).append(child_id)
        if parent_id not in self.reverse_edges.get(child_id, []):
            self.reverse_edges.setdefault(child_id, []).append(parent_id)

    def downstream(self, start_id: str) -> list[str]:
        """BFS downstream from start_id, including direct and transitive children."""
        if start_id not in self.nodes:
            return []
        visited: set[str] = set()
        queue: deque[str] = deque([start_id])
        result: list[str] = []
        while queue:
            current = queue.popleft()
            for child in self.edges.get(current, []):
                if child not in visited:
                    visited.add(child)
                    result.append(child)
                    queue.append(child)
        return result

    def to_dict(self) -> dict[str, Any]:
        """Serialise for --json output."""
        return {
            "nodes": [
                {
                    "event_id": n.event_id,
                    "sequence": n.sequence,
                    "type": n.type.value,
                    "origin": n.origin.value,
                    "status": n.status.value,
                    "invalidated_reason": n.invalidated_reason,
                    "caused_by": list(n.caused_by),
                    "payload": dict(n.payload),
                    "parents": list(self.reverse_edges.get(n.event_id, [])),
                    "children": list(self.edges.get(n.event_id, [])),
                }
                for n in sorted(self.nodes.values(), key=lambda x: x.sequence)
            ],
            "edges": [
                {"from": parent, "to": child}
                for parent, children in self.edges.items()
                for child in children
            ],
            "subagent_spans": list(self.subagent_spans),
            "compactions": list(self.compactions),
        }


def _apply_invalidation(
    graph: ProvenanceGraph,
    id_map: dict[str, str],
    target_id: object,
    payload: dict[str, Any],
) -> None:
    """Update node status and reason when an invalidation event matches."""
    if not isinstance(target_id, str):
        return
    node_id = id_map.get(target_id)
    if node_id is None or node_id not in graph.nodes:
        return
    status = StateStatus.INVALID
    raw_status = payload.get("status")
    if raw_status:
        with contextlib.suppress(ValueError, TypeError):
            status = StateStatus(raw_status)
    reason = payload.get("reason")
    if reason is None:
        reason = payload.get("invalidated_reason")
    graph.nodes[node_id] = replace(
        graph.nodes[node_id],
        status=status,
        invalidated_reason=str(reason) if reason is not None else None,
    )


def build_provenance_graph(events: Any) -> ProvenanceGraph:
    """Fold events into a DAG.

    Only events of type in _NODE_TYPES become nodes. Edges are taken from
    payload ``caused_by`` when present and referencing a known node. Unknown
    ``caused_by`` ids are ignored (they were refused at write time, but old
    events may have been written before validation).

    The fold is deterministic and hash-stable: same events yield same graph.
    """
    graph = ProvenanceGraph()
    # Tracking maps for payload identifiers to node event_ids
    decision_map: dict[str, str] = {}
    finding_map: dict[str, str] = {}

    # First pass: nodes and invalidation folding
    for ev in sorted(events, key=lambda e: e.sequence):
        pld: dict[str, Any] = ev.payload if isinstance(ev.payload, dict) else {}

        if ev.type == EventType.DECISION_INVALIDATED:
            _apply_invalidation(
                graph, decision_map, pld.get("decision_id") or pld.get("event_id"), pld
            )
            continue

        if ev.type == EventType.FINDING_INVALIDATED:
            _apply_invalidation(
                graph, finding_map, pld.get("finding_id") or pld.get("event_id"), pld
            )
            continue

        if ev.type not in _NODE_TYPES:
            continue

        caused_by = pld.get("caused_by")
        if not isinstance(caused_by, list):
            caused_by = []
        caused_by_t = tuple(str(x) for x in caused_by if isinstance(x, str) and x)

        initial_status = StateStatus.VALID
        raw_status = pld.get("status")
        if raw_status:
            with contextlib.suppress(ValueError, TypeError):
                initial_status = StateStatus(raw_status)

        initial_reason = None
        raw_reason = pld.get("invalidated_reason") or (
            pld.get("reason") if initial_status is StateStatus.INVALID else None
        )
        if raw_reason is not None:
            initial_reason = str(raw_reason)

        node = ProvenanceNode(
            event_id=ev.event_id,
            sequence=ev.sequence,
            type=ev.type,
            origin=ev.source,
            status=initial_status,
            invalidated_reason=initial_reason,
            caused_by=caused_by_t,
            payload=dict(pld),
        )
        graph.add_node(node)

        if ev.type == EventType.DECISION_CREATED:
            dec_id = pld.get("decision_id")
            if isinstance(dec_id, str) and dec_id:
                decision_map[dec_id] = ev.event_id
            decision_map[ev.event_id] = ev.event_id
        elif ev.type == EventType.FINDING_ADDED:
            find_id = pld.get("finding_id")
            if isinstance(find_id, str) and find_id:
                finding_map[find_id] = ev.event_id
            finding_map[ev.event_id] = ev.event_id
    # Second pass: edges
    for node in list(graph.nodes.values()):
        for parent_id in node.caused_by:
            if parent_id in graph.nodes:
                graph.add_edge(parent_id, node.event_id)
    # Third pass: collect subagent spans for delegation tracing
    for ev in sorted(events, key=lambda e: e.sequence):
        if ev.type == EventType.SUBAGENT_SPAWNED:
            payload = dict(ev.payload) if isinstance(ev.payload, dict) else {}
            graph.subagent_spans.append(
                {
                    "event_id": ev.event_id,
                    "parent_run_id": ev.run_id,
                    "subagent_run_id": payload.get("subagent_run_id", ""),
                    "task_description": payload.get("task_description", ""),
                    "status": "active",
                }
            )
        elif ev.type in (EventType.SUBAGENT_COMPLETED, EventType.SUBAGENT_FAILED):
            payload = dict(ev.payload) if isinstance(ev.payload, dict) else {}
            subagent_run_id = payload.get("subagent_run_id", "")
            for span in graph.subagent_spans:
                if span.get("subagent_run_id") == subagent_run_id:
                    span["status"] = (
                        "completed" if ev.type == EventType.SUBAGENT_COMPLETED else "failed"
                    )
                    span["result_summary"] = payload.get("result_summary")
                    break
        elif ev.type is EventType.PRECOMPACT_HOOK:
            payload = dict(ev.payload) if isinstance(ev.payload, dict) else {}
            graph.compactions.append(
                {
                    "event_id": ev.event_id,
                    "sequence": int(ev.sequence),
                    "retained_events": payload.get("retained_events"),
                    "compacted_events": payload.get("compacted_events"),
                    "summary": payload.get("summary"),
                }
            )
    return graph


def downstream_of(graph: ProvenanceGraph, evidence_id: str) -> list[ProvenanceNode]:
    """Return downstream nodes for a given evidence payload id or event id.

    ``evidence_id`` may be an event_id or a payload evidence_id (e.g. "ev1").
    The function resolves both: first tries event_id, then scans payloads for
    matching evidence_id / finding_id / decision_id.
    """
    # direct event id match
    if evidence_id in graph.nodes:
        downstream_ids = graph.downstream(evidence_id)
        return [graph.nodes[did] for did in downstream_ids]
    # payload id match
    start_ids: list[str] = []
    for node in graph.nodes.values():
        pid = (
            node.payload.get("evidence_id")
            or node.payload.get("finding_id")
            or node.payload.get("decision_id")
        )
        if pid == evidence_id or node.payload.get("evidence_id") == evidence_id:
            start_ids.append(node.event_id)
    result: list[ProvenanceNode] = []
    seen: set[str] = set()
    for sid in start_ids:
        for did in graph.downstream(sid):
            if did not in seen:
                seen.add(did)
                result.append(graph.nodes[did])
    return result


_ORIGIN_COLOR: dict[Origin, str] = {
    Origin.DETERMINISTIC: "lightblue",
    Origin.HUMAN: "lightgreen",
    Origin.EXTERNAL_AGENT: "orange",
    Origin.LLM: "gold",
    Origin.IMPORTED: "lightgrey",
}


def to_dot(graph: ProvenanceGraph) -> str:
    """Emit Graphviz DOT with per-node Origin color (issue #554)."""
    lines = ["digraph provenance {"]
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box, style=filled];")
    for node in sorted(graph.nodes.values(), key=lambda n: n.sequence):
        color = _ORIGIN_COLOR.get(node.origin, "white")
        label = f"{node.type.value}\\n{node.event_id[:8]}\\n{node.origin.value}"
        # Escape quotes in label
        label = label.replace('"', '\\"')
        lines.append(f'  "{node.event_id}" [label="{label}", fillcolor="{color}"];')
    for parent, children in graph.edges.items():
        for child in children:
            lines.append(f'  "{parent}" -> "{child}";')
    lines.append("}")
    return "\n".join(lines)
