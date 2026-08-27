"""Fork precondition gate (#407): refuse unsafe edits, stamp lineage (#389).

These tests would have failed on the pre-gate fork implementation, which
proceeded regardless of what the event log said must be preserved. They pin
the three acceptance shapes from #407:

* stranded-result fork refused with rationale naming sequence numbers;
* unsettled authorization (and uncertain slot) refused until reconcile or
  explicit carry_forward, which is honoured and auditable;
* benign fork passes with derivation summary stamped onto the lineage event.
"""

from __future__ import annotations

import pytest

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.fork import ForkPreconditionError, approve_fork
from continuum.storage import SQLiteStorage


def _make_storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    return storage


def test_stranded_result_fork_refused_with_rationale() -> None:
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger.complete(outcome.key, external_id="42", result={"issue": 42})
        completed_at = storage.last_sequence("run_1")
        storage.append_event(
            "run_1",
            EventType.WORK_ADDED,
            {"task_id": "w1", "description": "notify", "prerequisite": [outcome.key]},
        )

        with pytest.raises(ForkPreconditionError) as exc:
            approve_fork(storage, "run_1", reason="branch risky")

        err = exc.value
        # machine-readable rationale names sequence numbers and action ids
        assert str(completed_at) in str(err) or str(completed_at) in str(err.rationale)
        assert outcome.action.action_id in str(err)
        assert len(err.rationale["depended_results"]) == 1
        assert err.rationale["depended_results"][0]["sequence"] == completed_at
        assert err.rationale["depended_results"][0]["action_id"] == outcome.action.action_id
        # derivation is present and unaccounted subset is non-empty
        assert len(err.derivation.depended_results) >= 1
        assert len(err.unaccounted.depended_results) == 1

        # carry-forward honoured and auditable: same edit passes when asserted
        child = approve_fork(
            storage,
            "run_1",
            reason="branch with carry",
            carry_forward=[outcome.action.action_id],
            child_run_id="run_1_fork_carry",
        )
        assert child.run_id == "run_1_fork_carry"
        events = storage.read_events("run_1")
        fork_events = [e for e in events if e.type is EventType.RUN_FORKED]
        assert fork_events[-1].payload["carry_forward"] == [outcome.action.action_id]
        assert "preconditions" in fork_events[-1].payload
    finally:
        storage.close()


def test_stranded_result_also_honours_key_carry_forward() -> None:
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k2")
        ledger.complete(outcome.key, external_id="99")
        storage.append_event(
            "run_1",
            EventType.WORK_ADDED,
            {"task_id": "w2", "prerequisite": [outcome.key]},
        )
        with pytest.raises(ForkPreconditionError):
            approve_fork(storage, "run_1", reason="should block")

        # carrying the ledger key (not just action_id) also satisfies the gate
        child = approve_fork(
            storage,
            "run_1",
            reason="carry key",
            carry_forward=[outcome.key],
        )
        assert child.parent_run_id == "run_1"
    finally:
        storage.close()


def test_unsettled_authorization_refused_until_carry_forward_or_revoked() -> None:
    storage = _make_storage()
    try:
        granted = storage.append_event(
            "run_1",
            EventType.APPROVAL_GRANTED,
            {"approval_id": "ap-1", "subject": "ship it"},
        )
        with pytest.raises(ForkPreconditionError) as exc:
            approve_fork(storage, "run_1", reason="try branch")

        err = exc.value
        assert str(granted.sequence) in str(err) or str(granted.sequence) in str(err.rationale)
        assert "ap-1" in str(err)
        assert err.rationale["unsettled_authorizations"][0]["approval_id"] == "ap-1"
        assert err.rationale["unsettled_authorizations"][0]["sequence"] == granted.sequence

        # explicit carry_forward passes and is auditable
        approve_fork(
            storage,
            "run_1",
            reason="branch carrying auth",
            carry_forward=["ap-1"],
            child_run_id="run_1_fork_auth",
        )
        events = storage.read_events("run_1")
        fork = [e for e in events if e.type is EventType.RUN_FORKED][-1]
        assert "ap-1" in fork.payload["carry_forward"]
        assert fork.payload["preconditions"]["unsettled_authorizations"][0]["approval_id"] == "ap-1"

        # revoke path: new fork on a fresh parent where the grant was revoked
        storage2 = _make_storage()
        try:
            storage2.append_event(
                "run_1",
                EventType.APPROVAL_GRANTED,
                {"approval_id": "ap-2", "subject": "s"},
            )
            storage2.append_event(
                "run_1",
                EventType.APPROVAL_REVOKED,
                {"approval_id": "ap-2"},
            )
            child2 = approve_fork(storage2, "run_1", reason="clean after revoke")
            assert child2.run_id.endswith("_fork1")
        finally:
            storage2.close()

    finally:
        storage.close()


def test_uncertain_slot_refused_until_reconcile_or_carry_forward() -> None:
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
        claimed_seq = storage.last_sequence("run_1")

        with pytest.raises(ForkPreconditionError) as exc:
            approve_fork(storage, "run_1", reason="branch over open slot")

        err = exc.value
        assert str(claimed_seq) in str(err) or str(claimed_seq) in str(err.rationale)
        assert outcome.action.action_id in str(err)
        assert len(err.rationale["uncertain_slots"]) == 1
        assert err.rationale["uncertain_slots"][0]["sequence"] == claimed_seq

        # carry_forward honoured
        child = approve_fork(
            storage,
            "run_1",
            reason="carry slot",
            carry_forward=[outcome.action.action_id],
            child_run_id="run_1_fork_slot",
        )
        assert child.run_id == "run_1_fork_slot"
        events = storage.read_events("run_1")
        fork = [e for e in events if e.type is EventType.RUN_FORKED][-1]
        assert outcome.action.action_id in fork.payload["carry_forward"]

        # reconcile path: completing the slot clears the precondition
        storage3 = _make_storage()
        try:
            ledger3 = ActionLedger(storage3, "run_1")
            outcome3 = ledger3.claim("slack.notify", {"channel": "#ops"}, key="k1")
            ledger3.complete(outcome3.key, external_id="done")
            # completed slot is not uncertain, but without a dependent reference
            # there is also no depended_results, so fork is benign
            child3 = approve_fork(storage3, "run_1", reason="after settle")
            assert child3.run_id.endswith("_fork1")
        finally:
            storage3.close()

    finally:
        storage.close()


def test_benign_fork_passes_with_lineage_payload_present() -> None:
    storage = _make_storage()
    try:
        # no outstanding authorizations, depended results or uncertain slots
        child = approve_fork(storage, "run_1", reason="benign branch")

        events = storage.read_events("run_1")
        fork_events = [e for e in events if e.type is EventType.RUN_FORKED]
        assert len(fork_events) == 1
        payload = fork_events[0].payload
        assert payload["child_run_id"] == child.run_id
        assert payload["reason"] == "benign branch"
        # derivation summary is stamped onto the lineage event for audit
        assert "preconditions" in payload
        assert "precondition_summary" in payload
        assert "derivation" in payload
        assert payload["preconditions"]["unsettled_authorizations"] == []
        assert payload["preconditions"]["depended_results"] == []
        assert payload["preconditions"]["uncertain_slots"] == []
        assert payload["carry_forward"] == []
        # also check alias keys carry same data
        assert payload["derivation"] == payload["preconditions"]
        assert payload["precondition_summary"] == payload["preconditions"]
        assert payload["derivation_summary"] == payload["preconditions"]

        # child is independently usable
        assert storage.get_run(child.run_id).parent_run_id == "run_1"
        child_events = storage.read_events(child.run_id)
        assert child_events[0].type is EventType.RUN_STARTED
    finally:
        storage.close()


def test_fork_gate_is_deterministic_and_pure() -> None:
    """Repeated derivation over the same prefix yields the same refusal or allow."""
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("mail.send", {"to": "a@example.com"}, key="k1")
        ledger.complete(outcome.key, external_id="m1")
        storage.append_event(
            "run_1",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [outcome.key]},
        )
        # two consecutive fork attempts without new events must behave identically
        with pytest.raises(ForkPreconditionError) as e1:
            approve_fork(storage, "run_1", reason="first", child_run_id="c1")
        with pytest.raises(ForkPreconditionError) as e2:
            approve_fork(storage, "run_1", reason="second", child_run_id="c2")
        assert e1.value.rationale == e2.value.rationale
        assert e1.value.unaccounted == e2.value.unaccounted
    finally:
        storage.close()


def test_empty_span_never_blocks_even_with_later_events() -> None:
    """When divergence equals head, items before anchor do not block."""
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        ledger.claim("slack.notify", {"channel": "#ops"}, key="k_old")
        from continuum.checkpoint import CheckpointManager

        CheckpointManager(storage).checkpoint("run_1", trigger="test")
        # The uncertain slot is now before the divergence anchor, so it is
        # out of span and must not block the fork.
        child = approve_fork(storage, "run_1", reason="empty span")
        assert child.run_id.endswith("_fork1")

        # A fresh run checkpointed with no outstanding items also passes.
        storage3 = _make_storage()
        try:
            CheckpointManager(storage3).checkpoint("run_1", trigger="t")
            child2 = approve_fork(storage3, "run_1", reason="empty span fresh")
            assert child2.run_id.endswith("_fork1")
        finally:
            storage3.close()

    finally:
        storage.close()
