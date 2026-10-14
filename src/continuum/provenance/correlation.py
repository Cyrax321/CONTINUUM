"""Provenance correlation between observed tool events and ledger action attempts (#785).

CONTINUUM records host-observed ``TOOL_CALLED``, ``TOOL_COMPLETED``, and
``TOOL_FAILED`` events as well as ledger ``ACTION_RECORDED``, ``ACTION_RECONCILED``,
and ``ACTION_COMPENSATED`` events. Each is individually hash-chained.

This module provides the correlation model and read-only projection helpers that
link tool observations to the ledger action attempts they correspond to via a
shared, bounded correlation identifier (``correlation_id``).

Trust boundaries and non-goals:
- A correlation ID is evidence linkage, not proof an external side effect occurred.
  A correlated completed tool event does not automatically settle an uncertain action,
  bypass reconciliation, or override the most-cautious recovery result.
- Correlation IDs are distinct from idempotency keys: an idempotency key identifies
  a logical operation across retries and deduplicates claims, while a correlation ID
  links a specific attempt or execution span to host-level tool observations.
- Correlation IDs are distinct from causal ``caused_by`` DAG edges: ``caused_by``
  tracks which prior events motivated a decision or state change, while correlation
  links contemporaneous action intent with tool execution observations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from continuum.events import Event, EventType
from continuum.models import validate_correlation_id
from continuum.storage.base import Storage

__all__ = [
    "EvidenceChain",
    "correlate_action_events",
    "correlate_evidence",
    "validate_correlation_id",
]

_ACTION_EVENT_TYPES = (
    EventType.ACTION_RECORDED,
    EventType.ACTION_RECONCILED,
    EventType.ACTION_COMPENSATED,
)

_TOOL_EVENT_TYPES = (
    EventType.TOOL_CALLED,
    EventType.TOOL_COMPLETED,
    EventType.TOOL_FAILED,
)


@dataclass
class EvidenceChain:
    """An ordered action-to-tool evidence link for an action attempt or observation.

    Represents the correlation between ledger action events and host-observed
    tool execution events.
    """

    action_key: str | None = None
    action_id: str | None = None
    action_type: str | None = None
    correlation_id: str | None = None
    action_status: str | None = None
    status: str = "matched"
    action_events: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert evidence chain to a JSON-serializable dictionary."""
        return {
            "action_key": self.action_key,
            "action_id": self.action_id,
            "action_type": self.action_type,
            "correlation_id": self.correlation_id,
            "action_status": self.action_status,
            "status": self.status,
            "action_events": list(self.action_events),
            "tool_events": list(self.tool_events),
            "observations": list(self.observations),
        }


def _extract_correlation_id(event: Event) -> str | None:
    """Extract and validate correlation_id from an event payload if present."""
    if not isinstance(event.payload, dict):
        return None
    raw_cid = event.payload.get("correlation_id")
    if raw_cid is None:
        act = event.payload.get("action")
        if isinstance(act, dict):
            raw_cid = act.get("correlation_id")
    if raw_cid is None:
        return None
    try:
        return validate_correlation_id(raw_cid)
    except ValueError:
        return None


def _observation_details(event: Event) -> dict[str, Any]:
    """Extract observation details from a tool event."""
    payload = dict(event.payload) if isinstance(event.payload, dict) else {}
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.type.value if hasattr(event.type, "value") else str(event.type),
        "tool": payload.get("tool") or payload.get("tool_name") or "unknown",
        "path": payload.get("path"),
        "sha256": payload.get("sha256"),
        "bytes": payload.get("bytes"),
        "status": payload.get("status")
        or ("ok" if event.type is EventType.TOOL_COMPLETED else "failed"),
    }


def correlate_evidence(
    events: Iterable[Event],
    *,
    action_key: str | None = None,
    correlation_id: str | None = None,
) -> list[EvidenceChain]:
    """Correlate action events with host-observed tool events.

    Scans the given event stream in sequence order, matches actions and tool
    observations sharing the same correlation identifier, detects unmatched or
    ambiguous observations, and returns ordered evidence chains.
    """
    sorted_events = sorted(events, key=lambda e: e.sequence)

    # 1. Group action attempts by action_key and track correlation_ids
    # Map: action_key -> {
    #   "action_id": ..., "action_type": ..., "status": ..., "correlation_id": ...,
    #   "events": [...], "first_seq": ...
    # }
    actions_by_key: dict[str, dict[str, Any]] = {}
    actions_without_key: list[dict[str, Any]] = []

    # Map: correlation_id -> list of action_keys
    actions_by_cid: dict[str, list[str]] = {}

    # Tool events grouped by correlation_id: cid -> list of Events
    tools_by_cid: dict[str, list[Event]] = {}
    tools_without_cid: list[Event] = []

    for event in sorted_events:
        cid = _extract_correlation_id(event)

        if event.type in _ACTION_EVENT_TYPES:
            payload = dict(event.payload) if isinstance(event.payload, dict) else {}
            key = str(payload.get("key") or "")
            action_id = str(payload.get("action_id") or "")
            action_type = str(payload.get("action_type") or "")
            status = str(payload.get("status") or "")

            ev_summary = {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "type": event.type.value if hasattr(event.type, "value") else str(event.type),
                "status": status,
                "correlation_id": cid,
            }

            if not key:
                actions_without_key.append(
                    {
                        "action_key": None,
                        "action_id": action_id or None,
                        "action_type": action_type or None,
                        "correlation_id": cid,
                        "action_status": status or None,
                        "events": [ev_summary],
                        "first_seq": event.sequence,
                    }
                )
                continue

            if key not in actions_by_key:
                actions_by_key[key] = {
                    "action_key": key,
                    "action_id": action_id or None,
                    "action_type": action_type or None,
                    "correlation_id": cid,
                    "action_status": status,
                    "events": [ev_summary],
                    "first_seq": event.sequence,
                }
                if cid:
                    actions_by_cid.setdefault(cid, []).append(key)
            else:
                entry = actions_by_key[key]
                entry["events"].append(ev_summary)
                entry["action_status"] = status
                if cid and not entry["correlation_id"]:
                    entry["correlation_id"] = cid
                    actions_by_cid.setdefault(cid, []).append(key)
                elif cid and entry["correlation_id"] and entry["correlation_id"] != cid:
                    # Conflicting correlation IDs within the same action key
                    entry["conflicting_cids"] = True

        elif event.type in _TOOL_EVENT_TYPES:
            if cid:
                tools_by_cid.setdefault(cid, []).append(event)
            else:
                tools_without_cid.append(event)

    chains: list[EvidenceChain] = []
    seen_cids: set[str] = set()

    # Build chains for actions with keys
    for key, act_info in actions_by_key.items():
        cid = act_info.get("correlation_id")
        act_events = act_info.get("events", [])
        tool_evs: list[Event] = []
        observations: list[dict[str, Any]] = []

        if cid:
            seen_cids.add(cid)
            tool_evs = tools_by_cid.get(cid, [])
            observations = [_observation_details(te) for te in tool_evs]

            # Check ambiguity: multiple distinct action keys sharing the same correlation_id
            keys_for_cid = actions_by_cid.get(cid, [])
            if len(keys_for_cid) > 1 or act_info.get("conflicting_cids"):
                chain_status = "ambiguous"
            elif tool_evs:
                chain_status = "matched"
            else:
                chain_status = "unmatched_action"
        else:
            chain_status = "missing_correlation"

        tool_ev_summaries = [
            {
                "sequence": te.sequence,
                "event_id": te.event_id,
                "type": te.type.value if hasattr(te.type, "value") else str(te.type),
                "tool": (dict(te.payload).get("tool") or dict(te.payload).get("tool_name"))
                if isinstance(te.payload, dict)
                else "unknown",
            }
            for te in tool_evs
        ]

        chains.append(
            EvidenceChain(
                action_key=key,
                action_id=act_info.get("action_id"),
                action_type=act_info.get("action_type"),
                correlation_id=cid,
                action_status=act_info.get("action_status"),
                status=chain_status,
                action_events=act_events,
                tool_events=tool_ev_summaries,
                observations=observations,
            )
        )

    # Process tool observations whose correlation_id did not match any action
    for cid, t_events in tools_by_cid.items():
        if cid in seen_cids:
            continue
        tool_ev_summaries = [
            {
                "sequence": te.sequence,
                "event_id": te.event_id,
                "type": te.type.value if hasattr(te.type, "value") else str(te.type),
                "tool": (dict(te.payload).get("tool") or dict(te.payload).get("tool_name"))
                if isinstance(te.payload, dict)
                else "unknown",
            }
            for te in t_events
        ]
        obs_list = [_observation_details(te) for te in t_events]
        chains.append(
            EvidenceChain(
                action_key=None,
                action_id=None,
                action_type=None,
                correlation_id=cid,
                action_status=None,
                status="unmatched_observation",
                action_events=[],
                tool_events=tool_ev_summaries,
                observations=obs_list,
            )
        )

    # Filter by action_key or correlation_id if requested
    if action_key is not None:
        chains = [c for c in chains if c.action_key == action_key]
    if correlation_id is not None:
        chains = [c for c in chains if c.correlation_id == correlation_id]

    return chains


def correlate_action_events(
    storage: Storage,
    run_id: str,
    *,
    action_key: str | None = None,
    correlation_id: str | None = None,
) -> list[EvidenceChain]:
    """Read the full history for run_id and return correlated evidence chains.

    Reads all events including archived events from compaction so correlation
    survives across compaction boundaries.
    """
    events = storage.read_all_events(run_id)
    return correlate_evidence(
        events,
        action_key=action_key,
        correlation_id=correlation_id,
    )
