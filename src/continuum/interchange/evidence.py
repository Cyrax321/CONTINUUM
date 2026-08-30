"""Content-addressed evidence export for external consumers.

The exporter maps a run's durable evidence to four neutral primitives:

* Transitions -- event-appended state movements (every event)
* Observations -- environment validations and diffs
* Relations -- dependency edges between components
* State Checkpoints -- checkpoint records

Each primitive is content-addressed (stable_hash of its content), carries
sequence, origin/provenance, and the signature inputs needed to re-verify
against the hash-chained log. The export is pure read, zero new
dependencies, and the receiver can detect truncation or tampering by
re-computing the chain exactly as ``verify()`` does.

Design constraints
------------------
* Native format stays authoritative; this is a read-only view.
* Hashes are the same as the storage layer's ``Event.hash`` and
  ``StateCheckpoint.integrity_hash``, so a receiver compares directly
  to ``verify()`` output.
* No third-party dependencies beyond pydantic and the existing hashing.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from continuum.events import Event
from continuum.security.hashing import stable_hash, to_json
from continuum.storage.base import Storage

__all__ = [
    "EvidencePrimitive",
    "Transition",
    "Observation",
    "Relation",
    "Checkpoint",
    "export_evidence",
    "verify_export",
]

# ---------------------------------------------------------------------------
# Primitive models
# ---------------------------------------------------------------------------

Kind = Literal["transition", "observation", "relation", "checkpoint"]


class EvidencePrimitive(BaseModel):
    """Base for all exported primitives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Kind
    run_id: str
    sequence: int
    content_hash: str
    prev_hash: str | None
    origin: str
    timestamp: str
    payload: dict[str, Any]
    signature_inputs: dict[str, Any]

    def content(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "prev_hash": self.prev_hash,
            "origin": self.origin,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "signature_inputs": self.signature_inputs,
        }

    def digest(self) -> str:
        return stable_hash(self.content())


class Transition(EvidencePrimitive):
    kind: Literal["transition"] = "transition"
    event_id: str
    event_type: str


class Observation(EvidencePrimitive):
    kind: Literal["observation"] = "observation"
    event_id: str
    event_type: str
    observed_at: str


class Relation(EvidencePrimitive):
    kind: Literal["relation"] = "relation"
    event_id: str
    event_type: str
    source_id: str | None = None
    target_id: str | None = None


class Checkpoint(EvidencePrimitive):
    kind: Literal["checkpoint"] = "checkpoint"
    checkpoint_id: str
    version: int
    trigger: str
    integrity_hash: str | None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Observations are environment validations and diff results.
_OBSERVATION_TYPES = frozenset(
    {
        "STATE_VALIDATED",
        "ENVIRONMENT_CHANGED",
        "PERCEPTION_OBSERVED",
        "BRANCH_RESOLVED",
        "TOOL_COMPLETED",
        "TOOL_FAILED",
        "TOOL_CALLED",
        "REASONING_SUMMARY",
    }
)

# Relations are dependency edges.
_RELATION_TYPES = frozenset(
    {
        "DEPENDENCY_DECLARED",
        "FINDING_ADDED",
        "FINDING_INVALIDATED",
        "DECISION_CREATED",
        "DECISION_INVALIDATED",
        "EVIDENCE_ADDED",
        "WORK_ADDED",
        "CONSTRAINT_PINNED",
        "CONSTRAINT_RETRACTED",
    }
)

# Checkpoints are explicit checkpoint records; we also emit a checkpoint
# primitive for each STATE_CHECKPOINTED event, but the canonical checkpoint
# record comes from storage.list_checkpoints.
_CHECKPOINT_TYPES = frozenset({"STATE_CHECKPOINTED"})


def _classify(event: Event) -> Kind:
    t = event.type.value
    if t in _CHECKPOINT_TYPES:
        return "checkpoint"
    if t in _OBSERVATION_TYPES:
        return "observation"
    if t in _RELATION_TYPES:
        return "relation"
    return "transition"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_evidence(storage: Storage, run_id: str) -> list[dict[str, Any]]:
    """Export a run's evidence as JSON-serialisable primitives.

    Reads ``events`` plus ``archived_events`` (so compacted runs are fully
    covered) and ``checkpoints``. Each primitive carries ``content_hash``
    (stable_hash of its content), ``prev_hash`` (previous primitive's hash
    for chain verification), ``origin`` and signature inputs.

    The caller may write the result as JSON lines::

        for prim in export_evidence(storage, run_id):
            print(json.dumps(prim, sort_keys=True))

    Truncation or tampering is detectable by the receiver::

        prev = None
        for i, prim in enumerate(exported, start=1):
            assert prim["sequence"] == i
            assert prim["prev_hash"] == prev
            assert prim["content_hash"] == stable_hash(prim["signature_inputs"])
            # also recompute event digest for transitions/observations/relations
            prev = prim["content_hash"]

    Pure read, no writes, zero new dependencies.
    """
    # Validate run exists early, fail closed.
    storage.get_run(run_id)

    # Gather full history: archived + live, sorted by sequence.
    archived = list(storage.read_archived_events(run_id))
    live = list(storage.read_events(run_id))
    events = sorted([*archived, *live], key=lambda e: e.sequence)

    checkpoints = list(storage.list_checkpoints(run_id))
    # Map checkpoint_id -> checkpoint for quick lookup (not strictly needed
    # but useful for enrichment).
    _cp_by_id = {c.checkpoint_id: c for c in checkpoints}

    primitives: list[dict[str, Any]] = []
    prev_hash: str | None = None
    seq = 0

    for ev in events:
        seq += 1
        kind = _classify(ev)
        # Signature inputs are the event's content dict (the same inputs
        # that produce Event.hash). Use canonical JSON round-trip so the
        # timestamp is stored as ISO with T, matching stable_hash's
        # canonicalization and surviving JSON dump/load without re-encoding
        # via default=str (which would use a space separator and break the
        # hash). This keeps the exported hash identical to verify() and
        # lets a receiver recompute stable_hash(signature_inputs) after
        # loading the JSON lines.
        sig_inputs = json.loads(to_json(ev.content()))
        # Content hash for the exported primitive is the event's own hash
        # (so it matches verify() directly) and also stable_hash of the
        # primitive's content for chain verification. We store both:
        # content_hash is the export chain hash, event_hash is the raw event
        # hash for direct compare.
        primitive: dict[str, Any]
        base = {
            "run_id": ev.run_id,
            "sequence": seq,
            "content_hash": ev.hash,
            "prev_hash": prev_hash,
            "origin": ev.source.value,
            "timestamp": ev.timestamp.isoformat(),
            "payload": dict(ev.payload),
            "signature_inputs": sig_inputs,
            "event_id": ev.event_id,
            "event_type": ev.type.value,
        }
        if kind == "transition":
            primitive = {"kind": "transition", **base}
        elif kind == "observation":
            primitive = {"kind": "observation", "observed_at": ev.timestamp.isoformat(), **base}
        elif kind == "relation":
            # Try to extract source/target ids for dependency edges.
            source_id = (
                ev.payload.get("resource")
                or ev.payload.get("decision_id")
                or ev.payload.get("finding_id")
            )
            target_id = ev.payload.get("evidence") or ev.payload.get("depends_on")
            if isinstance(target_id, list) and target_id:
                target_id = target_id[0]
            primitive = {
                "kind": "relation",
                "source_id": str(source_id) if source_id else None,
                "target_id": str(target_id) if target_id else None,
                **base,
            }
        else:  # checkpoint
            # Enrich with checkpoint record if available.
            cp = None
            cid = ev.payload.get("checkpoint_id")
            if isinstance(cid, str):
                cp = _cp_by_id.get(cid)
            primitive = {
                "kind": "checkpoint",
                "checkpoint_id": cid if isinstance(cid, str) else ev.event_id,
                "version": ev.payload.get("version", 0),
                "trigger": ev.payload.get("trigger", "unknown"),
                "integrity_hash": cp.integrity_hash if cp else None,
                **base,
            }
        primitives.append(primitive)
        prev_hash = ev.hash

    # Emit checkpoint records that are not already represented as events?
    # In current storage, each checkpoint is also an event of type
    # STATE_CHECKPOINTED, so we have already covered them. To avoid
    # duplication, we only emit checkpoint records that have no matching
    # event. This keeps the export covering every event exactly once for
    # the truncation check, while still surfacing the checkpoint's
    # integrity_hash for external verification.
    emitted_cids = {p["checkpoint_id"] for p in primitives if p["kind"] == "checkpoint"}
    for cp in checkpoints:
        if cp.checkpoint_id in emitted_cids:
            continue
        seq += 1
        sig_inputs = json.loads(to_json(cp.content()))
        primitive = {
            "kind": "checkpoint",
            "run_id": cp.run_id,
            "sequence": seq,
            "content_hash": cp.integrity_hash,
            "prev_hash": prev_hash,
            "origin": "deterministic",
            "timestamp": cp.created_at.isoformat(),
            "payload": {
                "checkpoint_id": cp.checkpoint_id,
                "version": cp.version,
                "trigger": cp.trigger,
            },
            "signature_inputs": sig_inputs,
            "checkpoint_id": cp.checkpoint_id,
            "version": cp.version,
            "trigger": cp.trigger,
            "integrity_hash": cp.integrity_hash,
            "event_id": cp.checkpoint_id,
            "event_type": "STATE_CHECKPOINTED",
        }
        primitives.append(primitive)
        prev_hash = cp.integrity_hash

    return primitives


def verify_export(primitives: list[dict[str, Any]]) -> bool:
    """Verify an exported stream exactly as a receiver would.

    Returns True if the chain is intact, sequences are contiguous starting at
    1, each content_hash matches the recomputed digest of signature_inputs
    (for events) or is present for checkpoints, and prev_hash links are
    correct. Used in tests to prove truncation is detectable.
    """
    prev: str | None = None
    for i, prim in enumerate(primitives, start=1):
        if prim.get("sequence") != i:
            return False
        if prim.get("prev_hash") != prev:
            return False
        # Recompute event hash where possible.
        sig = prim.get("signature_inputs")
        if sig is not None:
            # For event-backed primitives, signature_inputs is the event content.
            # Recompute and compare to content_hash.
            try:
                recomputed = stable_hash(sig)
            except Exception:
                return False
            if prim.get("content_hash") != recomputed:
                # For checkpoint primitives that were not event-backed, the
                # content_hash is the checkpoint's integrity_hash, not the
                # stable_hash of signature_inputs. In that case, we accept
                # the stored hash as long as prev links hold; the checkpoint's
                # own verify() would be used. We only enforce the event case
                # where event_id is present and type is not checkpoint-only.
                # To keep it simple, we require the hash to match the stored
                # event hash which we already have; if it doesn't, it's tampered.
                return False
        prev = prim.get("content_hash")
    return True
