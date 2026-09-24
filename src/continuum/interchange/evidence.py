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
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from continuum.events import Event
from continuum.models import Origin, StateCheckpoint
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
        """Return the canonical fields represented by this primitive."""
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
        """Return the stable hash of this primitive's canonical content."""
        return stable_hash(self.content())

    @classmethod
    def for_event(
        cls,
        ev: Event,
        *,
        sequence: int,
        prev_hash: str | None,
        checkpoints: Mapping[str, StateCheckpoint] | None = None,
    ) -> EvidencePrimitive:
        """Build the primitive ``ev`` classifies to (#1155).

        The four subclasses were exported but never constructed, so the
        exporter hand-rolled ``dict[str, Any]`` values and every subclass
        field could be added, renamed or dropped with nothing breaking --
        the dict keys were spelled separately in the function body. Routing
        construction through the models makes ``extra="forbid"`` a live
        check: a field that drifts now raises at export instead of silently
        shipping a different shape.
        """
        common = _common_fields(ev, sequence=sequence, prev_hash=prev_hash)
        known: Mapping[str, StateCheckpoint] = checkpoints or {}
        kind = _classify(ev)
        if kind == "observation":
            return Observation(observed_at=ev.timestamp.isoformat(), **common)
        if kind == "relation":
            return Relation.from_event(ev, **common)
        if kind == "checkpoint":
            cid = ev.payload.get("checkpoint_id")
            record = known.get(cid) if isinstance(cid, str) else None
            return Checkpoint(
                checkpoint_id=cid if isinstance(cid, str) else ev.event_id,
                version=ev.payload.get("version", 0),
                trigger=ev.payload.get("trigger", "unknown"),
                integrity_hash=record.integrity_hash if record is not None else None,
                **common,
            )
        return Transition(**common)

    @classmethod
    def for_checkpoint(
        cls, cp: StateCheckpoint, *, sequence: int, prev_hash: str | None
    ) -> EvidencePrimitive:
        """Build the primitive for a checkpoint record with no log event.

        Current storage writes a ``STATE_CHECKPOINTED`` event for every
        checkpoint, so this path covers a record the event stream missed
        rather than a whole class of object. With no event to name, the
        checkpoint's own id stands in as ``event_id`` and the kind names the
        event type it would have had.
        """
        return Checkpoint(
            run_id=cp.run_id,
            sequence=sequence,
            content_hash=cp.integrity_hash,
            prev_hash=prev_hash,
            origin=Origin.DETERMINISTIC.value,
            timestamp=cp.created_at.isoformat(),
            payload={
                "checkpoint_id": cp.checkpoint_id,
                "version": cp.version,
                "trigger": cp.trigger,
            },
            signature_inputs=json.loads(to_json(cp.content())),
            checkpoint_id=cp.checkpoint_id,
            version=cp.version,
            trigger=cp.trigger,
            integrity_hash=cp.integrity_hash,
            event_id=cp.checkpoint_id,
            event_type=_CHECKPOINT_EVENT_TYPE,
        )


class Transition(EvidencePrimitive):
    """Represent an event-backed state transition."""

    kind: Literal["transition"] = "transition"
    event_id: str
    event_type: str


class Observation(EvidencePrimitive):
    """Represent an environment or tool observation."""

    kind: Literal["observation"] = "observation"
    event_id: str
    event_type: str
    observed_at: str


class Relation(EvidencePrimitive):
    """Represent a dependency or derivation relation between items."""

    kind: Literal["relation"] = "relation"
    event_id: str
    event_type: str
    source_id: str | None = None
    target_id: str | None = None

    @classmethod
    def from_event(cls, ev: Event, **common: Any) -> Relation:
        """Build a relation primitive, guessing its endpoints from the payload.

        A dependency edge's endpoints are spelled differently per event family
        (``resource`` for a declaration, ``decision_id`` / ``finding_id`` for
        those), so this is the one place primitive construction inspects the
        payload instead of copying it. Keeping it next to the fields it fills
        means a new event family that spells an endpoint differently lands in
        the model rather than in an exporter dict nobody type-checks (#1155).
        """
        source_id = (
            ev.payload.get("resource")
            or ev.payload.get("decision_id")
            or ev.payload.get("finding_id")
        )
        target_id = ev.payload.get("evidence") or ev.payload.get("depends_on")
        if isinstance(target_id, list) and target_id:
            target_id = target_id[0]
        return cls(
            source_id=str(source_id) if source_id else None,
            target_id=str(target_id) if target_id else None,
            **common,
        )


class Checkpoint(EvidencePrimitive):
    """Represent a persisted semantic-state checkpoint."""

    kind: Literal["checkpoint"] = "checkpoint"
    checkpoint_id: str
    version: int
    trigger: str
    integrity_hash: str | None
    # The exporter names the log event a checkpoint primitive came from, and
    # for a record-only checkpoint (no matching event) the checkpoint's own
    # id stands in. Declared now that the model is actually constructed
    # (#1155): the dict it replaces emitted both keys, and ``extra="forbid"``
    # would have rejected them had the model been built at the time.
    event_id: str
    event_type: str


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

#: The event type a checkpoint record would have been logged as. Named so the
#: event-backed and record-backed branches cannot spell it differently (#1155).
_CHECKPOINT_EVENT_TYPE = "STATE_CHECKPOINTED"


def _classify(event: Event) -> Kind:
    t = event.type.value
    if t in _CHECKPOINT_TYPES:
        return "checkpoint"
    if t in _OBSERVATION_TYPES:
        return "observation"
    if t in _RELATION_TYPES:
        return "relation"
    return "transition"


def _common_fields(ev: Event, *, sequence: int, prev_hash: str | None) -> dict[str, Any]:
    """Fields every event-backed primitive shares.

    ``content_hash`` is the event's own hash, not ``digest()``: it is what a
    receiver compares directly against ``verify()`` output, and the chain links
    are built from it. ``digest()`` content-addresses the primitive itself and
    is a separate guarantee.

    ``signature_inputs`` is the event's content dict, canonical-JSON round
    tripped so the timestamp lands as ISO-with-T: that matches
    ``stable_hash``'s canonicalisation and survives a JSON dump/load without
    ``default=str`` re-encoding (which would use a space separator and break
    the hash). This is what makes the exported hash identical to ``verify()``
    and lets a receiver recompute ``stable_hash(signature_inputs)`` after
    loading the JSON lines.
    """
    return {
        "run_id": ev.run_id,
        "sequence": sequence,
        "content_hash": ev.hash,
        "prev_hash": prev_hash,
        "origin": ev.source.value,
        "timestamp": ev.timestamp.isoformat(),
        "payload": dict(ev.payload),
        "signature_inputs": json.loads(to_json(ev.content())),
        "event_id": ev.event_id,
        "event_type": ev.type.value,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_evidence(storage: Storage, run_id: str) -> list[EvidencePrimitive]:
    """Export a run's evidence as typed, JSON-serialisable primitives.

    Reads ``events`` plus ``archived_events`` (so compacted runs are fully
    covered) and ``checkpoints``. Each primitive carries ``content_hash``
    (the event's own hash, so it matches ``verify()`` directly), ``prev_hash``
    (previous primitive's hash for chain verification), ``origin`` and
    signature inputs.

    The result is the module's own models, not untyped dicts (#1155): the
    four subclasses were declared and exported but never constructed, so a
    field could drift out of the model and nothing would notice. Serialize
    with ``model_dump()``::

        for prim in export_evidence(storage, run_id):
            print(json.dumps(prim.model_dump(mode="json"), sort_keys=True))

    Truncation or tampering is detectable by the receiver::

        prev = None
        for i, prim in enumerate(exported, start=1):
            assert prim.sequence == i
            assert prim.prev_hash == prev
            assert prim.content_hash == stable_hash(prim.signature_inputs)
            # also recompute event digest for transitions/observations/relations
            prev = prim.content_hash

    Pure read, no writes, zero new dependencies.
    """
    # Validate run exists early, fail closed.
    storage.get_run(run_id)

    # Gather full history: archived + live, sorted by sequence.
    archived = list(storage.read_archived_events(run_id))
    live = list(storage.read_events(run_id))
    events = sorted([*archived, *live], key=lambda e: e.sequence)

    # Map checkpoint_id -> checkpoint for enrichment of event-backed
    # checkpoint primitives with the stored integrity_hash.
    checkpoints = {c.checkpoint_id: c for c in storage.list_checkpoints(run_id)}

    primitives: list[EvidencePrimitive] = []
    prev_hash: str | None = None
    seq = 0

    for ev in events:
        seq += 1
        primitives.append(
            EvidencePrimitive.for_event(
                ev, sequence=seq, prev_hash=prev_hash, checkpoints=checkpoints
            )
        )
        prev_hash = ev.hash

    # In current storage, each checkpoint is also an event of type
    # STATE_CHECKPOINTED, so the loop above has already covered it. To avoid
    # duplication, only records with no matching event are emitted here. This
    # keeps the export covering every event exactly once for the truncation
    # check, while still surfacing the checkpoint's integrity_hash for
    # external verification.
    emitted_cids = {p.checkpoint_id for p in primitives if isinstance(p, Checkpoint)}
    for cp in checkpoints.values():
        if cp.checkpoint_id in emitted_cids:
            continue
        seq += 1
        primitives.append(EvidencePrimitive.for_checkpoint(cp, sequence=seq, prev_hash=prev_hash))
        prev_hash = cp.integrity_hash

    return primitives


def verify_export(primitives: Sequence[EvidencePrimitive | Mapping[str, Any]]) -> bool:
    """Verify an exported stream exactly as a receiver would.

    Accepts the models or the mappings a JSON-lines consumer produces after
    ``json.loads``, so a receiver reading a file and a caller holding the
    export in memory run the same check.

    Returns True if the chain is intact, sequences are contiguous starting at
    1, each content_hash matches the recomputed digest of signature_inputs
    (for events) or is present for checkpoints, and prev_hash links are
    correct. Used in tests to prove truncation is detectable.
    """
    prev: str | None = None
    for i, prim in enumerate(primitives, start=1):
        p = prim.model_dump() if isinstance(prim, EvidencePrimitive) else dict(prim)
        if p.get("sequence") != i:
            return False
        if p.get("prev_hash") != prev:
            return False
        # Recompute event hash where possible.
        sig = p.get("signature_inputs")
        if sig is not None:
            # For event-backed primitives, signature_inputs is the event content.
            # Recompute and compare to content_hash.
            try:
                recomputed = stable_hash(sig)
            except Exception:
                return False
            if p.get("content_hash") != recomputed:
                # For checkpoint primitives that were not event-backed, the
                # content_hash is the checkpoint's integrity_hash, not the
                # stable_hash of signature_inputs. In that case, we accept
                # the stored hash as long as prev links hold; the checkpoint's
                # own verify() would be used. We only enforce the event case
                # where event_id is present and type is not checkpoint-only.
                # To keep it simple, we require the hash to match the stored
                # event hash which we already have; if it doesn't, it's tampered.
                return False
        prev = p.get("content_hash")
    return True
