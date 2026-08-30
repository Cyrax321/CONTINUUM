"""Route restore and merge through the same precondition gate (#408).

One parametrised suite covers fork, restore and merge so the three edits
enforce and refuse symmetrically. Falsifiable: restore skipping an unsettled
claim refuses exactly like fork, and reconcile makes the same restore pass.
Per-edit-type semantics for depended_results (restore reactivates history) are
asserted separately and documented in gate.py.
"""

from __future__ import annotations

import pytest

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.fork import ForkPreconditionError, approve_fork
from continuum.recovery.gate import EditPreconditionError, check_preconditions
from continuum.recovery.merge import approve_merge
from continuum.recovery.restore import approve_restore
from continuum.storage import SQLiteStorage


def _make_storage(run_id: str = "run_1") -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return storage


def _anchor_zero(storage: SQLiteStorage, run_id: str = "run_1") -> int:
    return 0


def _edit_callables():
    """Map edit_type to a callable that attempts the edit and returns the run.

    Each callable has signature (storage, run_id, reason, carry_forward) and
    internally chooses the anchor the same way the real approval functions do
    for a fresh run with no checkpoint, so the parametrised tests drive the
    same span for every edit type.
    """

    def _fork(storage: SQLiteStorage, run_id: str, reason: str, carry_forward=None):
        return approve_fork(storage, run_id, reason=reason, carry_forward=carry_forward)

    def _restore(storage: SQLiteStorage, run_id: str, reason: str, carry_forward=None):
        # Use anchor 0 (before any history) so the span is (0, head] for all edits.
        # This makes the parametrised refusal symmetric: an uncertain slot in
        # (0, head] blocks every edit type the same way.
        return approve_restore(
            storage, run_id, reason=reason, anchor_sequence=0, carry_forward=carry_forward
        )

    def _merge(storage: SQLiteStorage, run_id: str, reason: str, carry_forward=None):
        return approve_merge(
            storage, run_id, reason=reason, anchor_sequence=0, carry_forward=carry_forward
        )

    return {
        "fork": _fork,
        "restore": _restore,
        "merge": _merge,
    }


EDIT_TYPES = ["fork", "restore", "merge"]
EDIT_CALLS = _edit_callables()


@pytest.mark.parametrize("edit_type", EDIT_TYPES)
def test_uncertain_slot_refused_symmetrically(edit_type: str) -> None:
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
        claimed_seq = storage.last_sequence("run_1")

        # All three edits over (0, head] must refuse the open slot
        with pytest.raises((ForkPreconditionError, EditPreconditionError)) as exc:
            EDIT_CALLS[edit_type](storage, "run_1", reason=f"try {edit_type}")

        err = exc.value
        assert err.rationale["uncertain_slots"]
        slot = err.rationale["uncertain_slots"][0]
        assert slot["sequence"] == claimed_seq
        assert slot["action_id"] == outcome.action.action_id
        assert str(claimed_seq) in str(err) or str(claimed_seq) in str(err.rationale)
        assert outcome.action.action_id in str(err)
        # Identical refusal shape: required keys present for every edit type
        for key in (
            "unsettled_authorizations",
            "depended_results",
            "uncertain_slots",
            "carry_forward",
        ):
            assert key in err.rationale
        assert err.rationale["edit_type"] == edit_type
        # Anchor naming is present for both fork and restore/merge
        assert "anchor_sequence" in err.rationale
        assert "divergence_sequence" in err.rationale

        # carry_forward honoured: same edit passes when the slot is carried
        carried = EDIT_CALLS[edit_type](
            storage, "run_1", reason="carry slot", carry_forward=[outcome.action.action_id]
        )
        assert carried is not None
        events = storage.read_events("run_1")
        # lineage stamping is identical in shape: find the lineage event for this edit type
        lineage_types = {EventType.RUN_FORKED, EventType.RUN_RESTORED, EventType.RUN_MERGED}
        lineage = [e for e in events if e.type in lineage_types]
        assert lineage
        last = lineage[-1]
        assert "preconditions" in last.payload
        assert "precondition_summary" in last.payload
        assert "derivation" in last.payload
        assert last.payload["carry_forward"] == [outcome.action.action_id]
        assert last.payload["edit_type"] == edit_type

        # reconcile path: completing the slot clears the precondition
        storage2 = _make_storage()
        try:
            ledger2 = ActionLedger(storage2, "run_1")
            outcome2 = ledger2.claim("slack.notify", {"channel": "#ops"}, key="k1")
            ledger2.complete(outcome2.key, external_id="done")
            # No uncertain slot, no depended reference, so every edit type is benign
            for et in EDIT_TYPES:
                check_preconditions(storage2, "run_1", 0, edit_type=et)  # should not raise
            carried2 = EDIT_CALLS[edit_type](storage2, "run_1", reason="after settle")
            assert carried2 is not None
        finally:
            storage2.close()
    finally:
        storage.close()


@pytest.mark.parametrize("edit_type", EDIT_TYPES)
def test_unsettled_authorization_refused_symmetrically(edit_type: str) -> None:
    storage = _make_storage()
    try:
        granted = storage.append_event(
            "run_1",
            EventType.APPROVAL_GRANTED,
            {"approval_id": "ap-1", "subject": "ship it"},
        )
        with pytest.raises((ForkPreconditionError, EditPreconditionError)) as exc:
            EDIT_CALLS[edit_type](storage, "run_1", reason="try branch")
        err = exc.value
        assert err.rationale["unsettled_authorizations"][0]["approval_id"] == "ap-1"
        assert err.rationale["unsettled_authorizations"][0]["sequence"] == granted.sequence
        assert "ap-1" in str(err)

        # carry by approval_id passes and is auditable
        carried = EDIT_CALLS[edit_type](
            storage, "run_1", reason="carry auth", carry_forward=["ap-1"]
        )
        assert carried is not None
        events = storage.read_events("run_1")
        lineage = [
            e
            for e in events
            if e.type in {EventType.RUN_FORKED, EventType.RUN_RESTORED, EventType.RUN_MERGED}
        ][-1]
        assert "ap-1" in lineage.payload["carry_forward"]
        assert (
            lineage.payload["preconditions"]["unsettled_authorizations"][0]["approval_id"] == "ap-1"
        )

        # revoke clears the precondition for every edit type
        storage2 = _make_storage()
        try:
            storage2.append_event(
                "run_1", EventType.APPROVAL_GRANTED, {"approval_id": "ap-2", "subject": "s"}
            )
            storage2.append_event("run_1", EventType.APPROVAL_REVOKED, {"approval_id": "ap-2"})
            for et in EDIT_TYPES:
                check_preconditions(storage2, "run_1", 0, edit_type=et)
            carried2 = EDIT_CALLS[edit_type](storage2, "run_1", reason="clean after revoke")
            assert carried2 is not None
        finally:
            storage2.close()
    finally:
        storage.close()


@pytest.mark.parametrize("edit_type", EDIT_TYPES)
def test_benign_edit_passes_with_lineage_payload(edit_type: str) -> None:
    storage = _make_storage()
    try:
        # No outstanding authorizations, depended results or uncertain slots
        result = EDIT_CALLS[edit_type](storage, "run_1", reason="benign")
        assert result is not None
        events = storage.read_events("run_1")
        lineage = [
            e
            for e in events
            if e.type in {EventType.RUN_FORKED, EventType.RUN_RESTORED, EventType.RUN_MERGED}
        ][-1]
        payload = lineage.payload
        assert payload["edit_type"] == edit_type
        for key in ("preconditions", "precondition_summary", "derivation", "derivation_summary"):
            assert key in payload
            assert payload[key]["unsettled_authorizations"] == []
            assert payload[key]["depended_results"] == []
            assert payload[key]["uncertain_slots"] == []
        assert payload["carry_forward"] == []
    finally:
        storage.close()


@pytest.mark.parametrize("edit_type", EDIT_TYPES)
def test_gate_is_deterministic_and_pure(edit_type: str) -> None:
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
        # For fork and merge this depended result is stranded (completion and
        # dependent both inside span). For restore, survivors are empty (anchor 0
        # has only RUN_STARTED), so restore would not consider it stranded under
        # the per-edit-type rule. To keep determinism test symmetric we use a
        # scenario that blocks every edit type: an uncertain slot.
        # Replace with uncertain slot for determinism
        storage2 = _make_storage(run_id="run_2")
        try:
            ledger2 = ActionLedger(storage2, "run_2")
            ledger2.claim("slack.notify", {"channel": "#ops"}, key="k1")
            with pytest.raises((ForkPreconditionError, EditPreconditionError)) as e1:
                check_preconditions(storage2, "run_2", 0, edit_type=edit_type)
            with pytest.raises((ForkPreconditionError, EditPreconditionError)) as e2:
                check_preconditions(storage2, "run_2", 0, edit_type=edit_type)
            assert e1.value.rationale == e2.value.rationale
            assert e1.value.unaccounted == e2.value.unaccounted
        finally:
            storage2.close()
    finally:
        storage.close()


def test_restore_reactivates_history_depended_differs_from_fork() -> None:
    """Per-edit-type semantics: depended set for restore differs from fork.

    A result completed inside the span that is only referenced inside the same
    span strands a fork (child would miss the result) but not a restore
    (both will be discarded and recomputed). Restore only blocks when the
    surviving prefix still references the result.
    """
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger.complete(outcome.key, external_id="42")
        storage.append_event(
            "run_1",
            EventType.WORK_ADDED,
            {"task_id": "w1", "description": "notify", "prerequisite": [outcome.key]},
        )
        # Anchor 0: span (0, head] contains both completion and dependent.
        # Fork must refuse; restore with empty survivors must not.
        with pytest.raises(ForkPreconditionError):
            check_preconditions(storage, "run_1", 0, edit_type="fork")
        # Restore sees no survivor referencing k1 (anchor 0 only has RUN_STARTED),
        # so it should allow the same span.
        # Use gate directly to assert the filtered depended set is empty.
        from continuum.recovery.gate import check_preconditions as gate_check

        # This should not raise for restore
        gate_check(storage, "run_1", 0, edit_type="restore")
        # And a merge (fork semantics) should still refuse
        with pytest.raises(EditPreconditionError):
            gate_check(storage, "run_1", 0, edit_type="merge")

        # Now create a survivor reference: a WORK_ADDED before the completion
        # that already declares the prerequisite. Put it at sequence 2 before
        # the claim/complete, then the completion inside span is still required
        # by surviving history.
        storage2 = _make_storage(run_id="run_2")
        try:
            from continuum.actions.idempotency import idempotency_key

            expected_key = idempotency_key(
                "github.create_issue", {"title": "t"}, scope="run_2", key="k1"
            )
            storage2.append_event(
                "run_2",
                EventType.WORK_ADDED,
                {"task_id": "w0", "description": "early need", "prerequisite": [expected_key]},
            )
            ledger2 = ActionLedger(storage2, "run_2")
            outcome2 = ledger2.claim("github.create_issue", {"title": "t"}, key="k1")
            ledger2.complete(outcome2.key, external_id="42")
            # Anchor at 2 (after the early WORK_ADDED, before the completion)
            # Span (2, head] contains the completion but survivor prefix at 2
            # references the hashed key, so restore must now refuse as well.
            anchor = 2
            with pytest.raises(EditPreconditionError) as exc:
                gate_check(storage2, "run_2", anchor, edit_type="restore")
            assert exc.value.rationale["depended_results"]
            assert exc.value.rationale["depended_results"][0]["key"] == expected_key
            # Fork over the same span also refuses, but for a different reason
            # (dependent inside span). Both refuse, but the depended set came
            # from different signals.
        finally:
            storage2.close()
    finally:
        storage.close()


def test_falsifiable_restore_skipping_unsettled_claim_refuses_like_fork() -> None:
    """Falsifiable from #389/#408: checkpoint mid-run after intercept.

    Checkpoint mid-run, claim an uncertain action, attempt restore that skips
    past the claim: it must refuse naming the action_id, just like fork.
    After reconcile the same restore passes.
    """
    storage = _make_storage()
    try:
        manager = CheckpointManager(storage)
        # Checkpoint at RUN_STARTED (sequence 1)
        manager.checkpoint("run_1", trigger="test")
        anchor = storage.latest_checkpoint("run_1").state.source_sequence
        assert anchor == 1

        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
        claimed_seq = storage.last_sequence("run_1")
        # Fork over (anchor, head] must refuse the uncertain slot
        with pytest.raises(ForkPreconditionError) as fe:
            approve_fork(storage, "run_1", reason="fork over open slot")
        assert outcome.action.action_id in str(fe.value)
        assert fe.value.rationale["uncertain_slots"][0]["sequence"] == claimed_seq

        # Restore to the same anchor must refuse symmetrically, naming the same id
        with pytest.raises(EditPreconditionError) as re:
            approve_restore(
                storage, "run_1", reason="restore over open slot", anchor_sequence=anchor
            )
        assert re.value.edit_type == "restore"
        assert re.value.rationale["uncertain_slots"][0]["action_id"] == outcome.action.action_id
        assert outcome.action.action_id in str(re.value)
        assert re.value.rationale["uncertain_slots"][0]["sequence"] == claimed_seq

        # Merge must also refuse symmetrically
        with pytest.raises(EditPreconditionError) as me:
            approve_merge(storage, "run_1", reason="merge over open slot", anchor_sequence=anchor)
        assert me.value.rationale["uncertain_slots"][0]["action_id"] == outcome.action.action_id

        # Reconcile the slot: complete it, then every edit type must pass
        ledger.complete(outcome.key, external_id="done")
        # Fork now passes (no uncertain, no depended reference)
        child = approve_fork(
            storage, "run_1", reason="after settle", child_run_id="run_1_fork_after"
        )
        assert child.run_id == "run_1_fork_after"
        # Restore now passes
        restored = approve_restore(storage, "run_1", reason="after settle", anchor_sequence=anchor)
        assert restored.run_id == "run_1"
        events = storage.read_events("run_1")
        assert any(e.type is EventType.RUN_RESTORED for e in events)
        # Merge now passes
        merged = approve_merge(storage, "run_1", reason="after settle", anchor_sequence=anchor)
        assert merged.run_id == "run_1"
        assert any(e.type is EventType.RUN_MERGED for e in storage.read_events("run_1"))
    finally:
        storage.close()
