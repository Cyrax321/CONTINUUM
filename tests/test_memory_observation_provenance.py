"""Tests for observation provenance linkage and memory forensics (issue #1416).

Covers:
- Action model accepts and synchronizes origin_observation_digest and origin_digest
- ActionLedger.claim accepts origin_observation_digest and stores it in event payload
- get_memory_provenance walks backward to initiating observation event
- get_memory_provenance enumerates sibling records sharing the same origin digest
- get_memory_provenance survives event-log compaction
"""

from __future__ import annotations

import pytest

from continuum.actions.ledger import ActionLedger, LedgerError, get_memory_provenance
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run
from continuum.security.hashing import stable_hash
from continuum.storage import SQLiteStorage


def test_action_model_origin_observation_digest_validation() -> None:
    """Action model accepts and synchronizes origin_observation_digest with origin_digest."""
    digest = "c" * 64

    # Supplying origin_observation_digest sets both fields
    act1 = Action(
        run_id="run_1",
        action_type="mem_write",
        origin_observation_digest=digest,
    )
    assert act1.origin_observation_digest == digest
    assert act1.origin_digest == digest

    # Supplying origin_digest sets both fields
    act2 = Action(
        run_id="run_1",
        action_type="mem_write",
        origin_digest=digest,
    )
    assert act2.origin_observation_digest == digest
    assert act2.origin_digest == digest

    # Mismatched digests raise ValueError
    with pytest.raises(ValueError, match="disagree"):
        Action(
            run_id="run_1",
            action_type="mem_write",
            origin_digest=digest,
            origin_observation_digest="d" * 64,
        )

    # Invalid hex string raises ValueError
    with pytest.raises(ValueError, match="64 lowercase hex"):
        Action(
            run_id="run_1",
            action_type="mem_write",
            origin_observation_digest="not-a-valid-hex",
        )


def test_claim_with_origin_observation_digest() -> None:
    """ActionLedger.claim accepts origin_observation_digest and records it on ledger rows."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "test"})

    ledger = ActionLedger(storage, "run_1")
    digest = "a" * 64

    outcome = ledger.claim(
        "mem_write",
        {"data": "content"},
        key="mem:tenant:k1",
        origin_observation_digest=digest,
    )
    assert outcome.fresh is True
    assert outcome.action.origin_observation_digest == digest
    assert outcome.action.origin_digest == digest

    # Stored event payload carries the digest
    events = list(storage.read_events("run_1"))
    action_events = [e for e in events if e.type == EventType.ACTION_RECORDED]
    assert len(action_events) == 1
    assert action_events[0].payload.get("origin_observation_digest") == digest
    assert action_events[0].payload.get("origin_digest") == digest

    # Disagreeing parameters raise LedgerError
    with pytest.raises(LedgerError, match="disagree"):
        ledger.claim(
            "mem_write",
            {"data": "other"},
            key="mem:tenant:k2",
            origin_digest=digest,
            origin_observation_digest="b" * 64,
        )


def test_get_memory_provenance_walks_to_initiating_observation() -> None:
    """get_memory_provenance finds the originating observation event in the hash chain."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "test"})

    # Record perception observation
    obs_payload = {"source": "web_search", "snippet": "target finding"}
    digest = stable_hash(obs_payload)
    storage.append_event("run_1", EventType.PERCEPTION_OBSERVED, obs_payload)

    # Claim memory write citing that observation digest
    ledger = ActionLedger(storage, "run_1")
    key = "mem:tenant_a:record_100"
    ledger.claim(
        "mem_write",
        {"fact": "derived"},
        key=key,
        origin_observation_digest=digest,
    )

    # Forensic lookup via module function and ledger method
    prov = get_memory_provenance(storage, "record_100")
    assert prov is not None
    assert prov["record_key"] == "record_100"
    assert prov["rendered_key"] == key
    assert prov["origin_digest"] == digest
    assert prov["origin_observation_digest"] == digest
    assert prov["observation_event"] is not None
    assert prov["observation_event"].payload == obs_payload
    assert prov["sibling_records"] == []

    # Method on ActionLedger returns equivalent result
    prov_method = ledger.get_memory_provenance("record_100")
    assert prov_method is not None
    assert prov_method["origin_digest"] == digest
    assert prov_method["observation_event"] is not None


def test_get_memory_provenance_identifies_siblings() -> None:
    """get_memory_provenance enumerates sibling records originating from the same observation."""
    storage = SQLiteStorage(":memory:")
    for rid in ("run_1", "run_2"):
        storage.create_run(Run(run_id=rid, goal="test"))
        storage.append_event(rid, EventType.RUN_STARTED, {"goal": "test"})

    obs_payload = {"source": "tool_output", "text": "shared raw data"}
    digest = stable_hash(obs_payload)
    storage.append_event("run_1", EventType.PERCEPTION_OBSERVED, obs_payload)

    ledger1 = ActionLedger(storage, "run_1")
    ledger1.claim("mem_write", key="mem:store:item_alpha", origin_observation_digest=digest)
    ledger1.claim("mem_write", key="mem:store:item_beta", origin_observation_digest=digest)

    # Sibling in another run
    ledger2 = ActionLedger(storage, "run_2")
    ledger2.claim("mem_write", key="mem:store:item_gamma", origin_observation_digest=digest)

    prov = get_memory_provenance(storage, "item_alpha")
    assert prov is not None
    assert prov["rendered_key"] == "mem:store:item_alpha"
    assert prov["origin_digest"] == digest

    # Both other records appear as siblings
    assert "mem:store:item_beta" in prov["sibling_records"]
    assert "mem:store:item_gamma" in prov["sibling_records"]
    assert "mem:store:item_alpha" not in prov["sibling_records"]
    assert len(prov["sibling_actions"]) == 2


def test_get_memory_provenance_survives_compaction() -> None:
    """Observation provenance lookup survives event-log compaction."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="long running task"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "long running task"})

    obs_payload = {"prompt": "poisoned context injection"}
    digest = stable_hash(obs_payload)
    storage.append_event("run_1", EventType.PERCEPTION_OBSERVED, obs_payload)

    ledger = ActionLedger(storage, "run_1")
    key = "mem:global:persisted_key"
    outcome = ledger.claim(
        "mem_write",
        {"value": "poisoned"},
        key=key,
        origin_observation_digest=digest,
    )
    ledger.complete(outcome.key, result={"written": True})

    # Add further events and compact past the observation and action
    storage.append_event("run_1", EventType.TASK_UPDATED, {"progress": 1.0})
    last_seq = storage.last_sequence("run_1")
    storage.compact_run("run_1", through_sequence=last_seq - 1)

    # Verify events were moved to archive
    live_events = list(storage.read_events("run_1"))
    assert not any(e.type == EventType.PERCEPTION_OBSERVED for e in live_events)

    # Forensic lookup still successfully recovers the archived observation and action
    prov = get_memory_provenance(storage, "persisted_key")
    assert prov is not None
    assert prov["origin_digest"] == digest
    assert prov["observation_event"] is not None
    assert prov["observation_event"].payload == obs_payload
    assert prov["action"].status == ActionStatus.COMPLETED


def test_get_memory_provenance_missing_or_no_digest() -> None:
    """Lookup handles nonexistent records and actions without observation digests."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "test"})

    ledger = ActionLedger(storage, "run_1")
    ledger.claim("compute", key="compute:k1")

    # Nonexistent record returns None
    assert get_memory_provenance(storage, "nonexistent") is None

    # Record without digest returns empty provenance
    prov = get_memory_provenance(storage, "compute:k1")
    assert prov is not None
    assert prov["origin_digest"] is None
    assert prov["observation_event"] is None
    assert prov["sibling_records"] == []
