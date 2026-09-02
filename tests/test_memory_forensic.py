"""Origin digest, forensic join and gateway tenant deny (issue #566, parent #304).

Covers:
- claim metadata carries optional origin_digest, ledger round-trips it
- forensic query joins record_key back to originating observations
- gateway denies cross-tenant namespace claims
- adapters mark memory-derived inputs unverified and escalate
"""

from __future__ import annotations

import pytest

from continuum.actions.ledger import ActionLedger, fold_action_events, forensic_join_across_runs
from continuum.events import EventType
from continuum.gate import derive_memory_key, is_memory_key
from continuum.gateway import load_gateway_config, match_route
from continuum.models import Run
from continuum.security.hashing import stable_hash
from continuum.security.provenance import PlanBranch
from continuum.security.trust_gate import record_memory_observation, resolve_branch
from continuum.storage import SQLiteStorage


def test_origin_digest_round_trips_via_ledger(tmp_path) -> None:
    path = str(tmp_path / "mem.db")
    digest = "a" * 64
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        rendered = "mem:pgvector_main:acme:rec-42"
        outcome = ledger.claim(
            "mem_write", {}, key=rendered, scoped_to_run=False, origin_digest=digest
        )
        assert outcome.fresh is True
        assert outcome.action.origin_digest == digest
        # Payload top-level also carries it
        ev = list(store.read_events("run_1"))[-1]
        assert ev.payload.get("origin_digest") == digest
        assert ev.payload.get("rendered_key") == rendered
        # Round-trip via get and fold
        fetched = ledger.get(outcome.key)
        assert fetched is not None
        assert fetched.origin_digest == digest
        folded = fold_action_events(store.read_events("run_1"))
        assert folded[outcome.key].origin_digest == digest


def test_origin_digest_rejects_bad_hex(tmp_path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        with pytest.raises(Exception, match="origin_digest"):
            ledger.claim(
                "mem_write",
                {},
                key="mem:pgvector_main:acme:rec-42",
                scoped_to_run=False,
                origin_digest="bad",
            )


def test_forensic_join_record_key_to_observation(tmp_path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        # Create an observation that will be the origin
        obs_payload = {"content_hash": "obs_hash_1", "raw_claim": "poisoned content"}
        digest = stable_hash(obs_payload)
        # Record observation via direct event
        store.append_event("run_1", EventType.PERCEPTION_OBSERVED, obs_payload)
        # Claim a memory write that cites that observation
        ledger = ActionLedger(store, "run_1")
        rendered = "mem:pgvector_main:acme:rec-99"
        outcome = ledger.claim(
            "mem_write", {}, key=rendered, scoped_to_run=False, origin_digest=digest
        )
        # Also claim a sibling write from same origin
        rendered2 = "mem:pgvector_main:acme:rec-100"
        outcome2 = ledger.claim(
            "mem_write", {}, key=rendered2, scoped_to_run=False, origin_digest=digest
        )
        assert outcome.fresh and outcome2.fresh
        # Forensic lookup by record_key should find both and join to observation
        hits = ledger.forensic_lookup("rec-99")
        assert len(hits) == 1
        assert hits[0]["origin_digest"] == digest
        assert hits[0]["rendered_key"] == rendered
        # Observation event should be joined (we stored PERCEPTION_OBSERVED with same payload hash)
        # Our forensic maps digest via stable_hash of payload, so it should find it
        assert hits[0]["observation_event"] is not None
        assert hits[0]["observation_event"].payload == obs_payload
        # Cross-run join should also find them
        hits_all = forensic_join_across_runs(store, "rec-")
        # Should find at least the two we created (maybe more)
        found = {h["rendered_key"] for h in hits_all}
        assert rendered in found
        assert rendered2 in found


def test_forensic_join_across_runs_tenant_scoped(tmp_path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        for rid in ("run_1", "run_2"):
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
        ledger1 = ActionLedger(store, "run_1")
        digest = "b" * 64
        ledger1.claim(
            "mem_write",
            {},
            key="mem:pgvector_main:acme:rec-42",
            scoped_to_run=False,
            origin_digest=digest,
        )
        ledger2 = ActionLedger(store, "run_2")
        # Different tenant, same record_key, should be separate hits
        ledger2.claim(
            "mem_write",
            {},
            key="mem:pgvector_main:globex:rec-42",
            scoped_to_run=False,
            origin_digest=digest,
        )
        hits = forensic_join_across_runs(store, "rec-42")
        # Both tenants should appear
        tenants = set()
        for h in hits:
            rk = h["rendered_key"]
            if "acme" in rk:
                tenants.add("acme")
            if "globex" in rk:
                tenants.add("globex")
        assert "acme" in tenants
        assert "globex" in tenants


def test_gateway_tenant_deny(tmp_path) -> None:
    # Memory route with mem template
    routes = load_gateway_config(tmp_path / "nope.json")  # empty
    # Manually construct route
    from continuum.gateway import Route

    route = Route(
        host="pgvector.internal",
        methods=("POST",),
        prefix="/upsert",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:{record_key}",
    )
    routes = [route]
    # Create a claimed action for acme
    path = str(tmp_path / "gw.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        rendered_acme = "mem:pgvector_main:acme:rec-42"
        outcome = ledger.claim("mem_write", {}, key=rendered_acme, scoped_to_run=False)
        assert outcome.fresh
        # Complete it so gateway sees STARTED? Actually gateway allows only STARTED, not COMPLETED.
        # For this test we want live claim, so keep STARTED.
        actions = fold_action_events(store.read_events("run_1"))
        # Bound acme, request acme -> should be allowed (if claim exists)
        body_acme = {"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"}
        decision = match_route(
            routes,
            host="pgvector.internal",
            method="POST",
            body=body_acme,
            actions_by_key=actions,
            run_id="run_1",
            bound_tenant="acme",
            storage=store,
        )
        assert decision.allow is True
        # Bound acme, request globex -> deny tenant mismatch before ledger check
        body_globex = {"store_id": "pgvector_main", "tenant": "globex", "record_key": "rec-42"}
        decision2 = match_route(
            routes,
            host="pgvector.internal",
            method="POST",
            body=body_globex,
            actions_by_key=actions,
            run_id="run_1",
            bound_tenant="acme",
            storage=store,
        )
        assert decision2.allow is False
        assert "tenant mismatch" in decision2.reason
        # No bound Tenant -> cross-tenant would be allowed if claim existed, but globex has no claim
        # So it should be denied as unclaimed, not tenant mismatch
        decision3 = match_route(
            routes,
            host="pgvector.internal",
            method="POST",
            body=body_globex,
            actions_by_key=actions,
            run_id="run_1",
            bound_tenant=None,
            storage=store,
        )
        assert decision3.allow is False
        assert "has no ledger claim" in decision3.reason


def test_gateway_tenant_deny_before_ledger(tmp_path) -> None:
    from continuum.gateway import Route

    route = Route(
        host="pgvector.internal",
        methods=("POST",),
        prefix="/upsert",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:{record_key}",
    )
    path = str(tmp_path / "gw.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        actions = fold_action_events(store.read_events("run_1"))
        # No claim at all, but tenant mismatch should still deny with tenant mismatch
        body = {"store_id": "pgvector_main", "tenant": "globex", "record_key": "rec-99"}
        decision = match_route(
            [route],
            host="pgvector.internal",
            method="POST",
            body=body,
            actions_by_key=actions,
            run_id="run_1",
            bound_tenant="acme",
        )
        assert decision.allow is False
        assert "tenant mismatch" in decision.reason


def test_memory_observation_unverified_escalates(tmp_path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        # Memory retrieval is unverified by default
        content_hash = stable_hash({"record": "rec-42", "content": "suspicious"})
        ev = record_memory_observation(store, "run_1", content_hash, "retrieved rec-42")
        assert ev.type == EventType.PERCEPTION_OBSERVED
        assert ev.payload["trust_level"] == "unverified"
        assert ev.payload["source"] == "environment_observed"
        # High-risk branch gated on that observation should require review
        branch = PlanBranch(
            branch_id="b1",
            risk_tier="high",
            action_intent="submit_payment",
            depends_on_observation=True,
        )
        # Fetch the observation provenance back
        from continuum.security.provenance import ObservationProvenance

        obs = ObservationProvenance.model_validate(ev.payload)
        gate = resolve_branch(branch, obs, storage=store, run_id="run_1")
        assert gate.requires_review is True
        # Low-risk branch should not
        branch_low = PlanBranch(
            branch_id="b2", risk_tier="low", action_intent="read_text", depends_on_observation=True
        )
        gate2 = resolve_branch(branch_low, obs)
        assert gate2.requires_review is False


def test_derive_memory_key_and_is_memory_helpers() -> None:
    assert is_memory_key("mem:pgvector_main:acme:rec-1") is True
    rendered = derive_memory_key(
        {"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"}
    )
    assert rendered == "mem:pgvector_main:acme:rec-42"
