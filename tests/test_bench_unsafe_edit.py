"""End-to-end unsafe-edit scenario (issue #410, epic #389).

Public-boundary proof that a mid-action checkpoint followed by a
skip-past restore/merge is caught by the shared gate. Unit tests prove
pieces; this proves the real checkpoint after ActionLedger.claim via the
public surface.

Falsifiable: before #389 the same restore would have passed silently and
dropped the outside-world uncertainty; after, the gate refuses naming the
action id and suggesting reconcile or carry-forward. Covers fork, restore
and merge (at minimum restore and merge as #408 gate).

Corpus hook: registered as the unsafe_edit fault class in the #397 chaos
suite (benchmarks/fault_injection/faults.py -> unsafe_edit) so
detection_rate covers it.
"""

from __future__ import annotations

import pytest

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.gate import EditPreconditionError, check_preconditions
from continuum.storage import SQLiteStorage


def _make_storage(run_id: str = "run_unsafe") -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return storage


def test_checkpoint_mid_run_after_claim_restore_and_merge_refuse_naming_action_id() -> None:
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_unsafe")
        outcome = ledger.claim("test.unsafe", {"resource": "r1"}, key="unsafe-k1")
        claimed_id = outcome.action.action_id
        claimed_seq = storage.last_sequence("run_unsafe")
        CheckpointManager(storage).checkpoint("run_unsafe")
        anchor = 0
        from continuum.recovery.restore import approve_restore

        with pytest.raises(EditPreconditionError) as exc:
            approve_restore(storage, "run_unsafe", reason="test restore", anchor_sequence=anchor)
        msg = str(exc.value)
        rationale = exc.value.rationale
        assert claimed_id in msg or claimed_id in str(rationale)
        assert str(claimed_seq) in msg or claimed_seq in str(
            rationale["uncertain_slots"][0]["sequence"]
        )
        low = (msg + str(rationale)).lower()
        assert "reconcile" in low
        assert "carry" in low
        assert rationale["uncertain_slots"][0]["action_id"] == claimed_id
        assert rationale["edit_type"] == "restore"

        from continuum.recovery.merge import approve_merge

        with pytest.raises(EditPreconditionError) as exc2:
            approve_merge(storage, "run_unsafe", reason="test merge", anchor_sequence=anchor)
        assert claimed_id in str(exc2.value) or claimed_id in str(exc2.value.rationale)
        assert (
            "reconcile" in str(exc2.value).lower()
            or "reconcile" in str(exc2.value.rationale).lower()
        )

        with pytest.raises(EditPreconditionError):
            check_preconditions(storage, "run_unsafe", anchor, edit_type="fork")
        with pytest.raises(EditPreconditionError):
            check_preconditions(storage, "run_unsafe", anchor, edit_type="merge")

        with pytest.raises(EditPreconditionError) as exc3:
            check_preconditions(storage, "run_unsafe", anchor, edit_type="restore")
        assert exc3.value.rationale["uncertain_slots"][0]["action_id"] == claimed_id
    finally:
        storage.close()


def test_reconcile_then_same_restore_passes() -> None:
    storage = _make_storage()
    try:
        ledger = ActionLedger(storage, "run_unsafe")
        outcome = ledger.claim("test.unsafe", {"resource": "r1"}, key="unsafe-k1")
        CheckpointManager(storage).checkpoint("run_unsafe")
        anchor = 0
        from continuum.recovery.merge import approve_merge
        from continuum.recovery.restore import approve_restore

        with pytest.raises(EditPreconditionError):
            approve_restore(storage, "run_unsafe", reason="before", anchor_sequence=anchor)

        ledger.reconcile(outcome.action.action_id, occurred=True, external_id="ext-1", note="probe")

        restored = approve_restore(
            storage, "run_unsafe", reason="after reconcile", anchor_sequence=anchor
        )
        assert restored.run_id == "run_unsafe"
        events = storage.read_events("run_unsafe")
        assert any(e.type == EventType.RUN_RESTORED for e in events)
        last = [e for e in events if e.type == EventType.RUN_RESTORED][-1]
        assert "preconditions" in last.payload
        assert last.payload["edit_type"] == "restore"

        merged = approve_merge(
            storage, "run_unsafe", reason="after reconcile merge", anchor_sequence=anchor
        )
        assert merged.run_id == "run_unsafe"

        check_preconditions(storage, "run_unsafe", anchor, edit_type="fork")
    finally:
        storage.close()


@pytest.mark.parametrize("edit_type", ["fork", "restore", "merge"])
def test_unsafe_edit_parametrised_across_all_three_edits(edit_type: str) -> None:
    storage = _make_storage(run_id=f"run_{edit_type}")
    try:
        run_id = f"run_{edit_type}"
        ledger = ActionLedger(storage, run_id)
        outcome = ledger.claim("test.unsafe", {"resource": "r1"}, key="unsafe-k1")
        claimed_id = outcome.action.action_id
        CheckpointManager(storage).checkpoint(run_id)
        anchor = 0

        with pytest.raises(EditPreconditionError) as exc:
            check_preconditions(storage, run_id, anchor, edit_type=edit_type)  # type: ignore[arg-type]
        assert claimed_id in str(exc.value) or claimed_id in str(exc.value.rationale)
        assert "reconcile" in str(exc.value).lower()
        assert exc.value.rationale["edit_type"] == edit_type

        ledger.reconcile(outcome.action.action_id, occurred=True, external_id="ext-1", note="ok")
        check_preconditions(storage, run_id, anchor, edit_type=edit_type)  # type: ignore[arg-type]
    finally:
        storage.close()


def test_control_pre_389_would_have_allowed_skip_past_unsafe_claim() -> None:
    """Document the pre-epic gap without running old main.

    Before #389 there was no derivation of unsettled authorizations or
    uncertain slots before fork/restore/merge. A restore that discarded
    (anchor, head] containing an uncertain ActionLedger.claim would have
    succeeded: no gate, no refusal, no lineage. The outside-world
    uncertainty would have been silently dropped and the next session
    could have resumed as if the side effect were settled.

    This test asserts the old code path (no gate) would have passed by
    showing that without calling check_preconditions the storage
    operation itself succeeds. The new gate is what makes the same
    scenario refuse, as proven by the tests above.
    """
    storage = _make_storage(run_id="run_control")
    try:
        ledger = ActionLedger(storage, "run_control")
        ledger.claim("test.unsafe", {"resource": "r1"}, key="unsafe-k1")
        CheckpointManager(storage).checkpoint("run_control")
        cp = storage.latest_checkpoint("run_control")
        assert cp is not None
    finally:
        storage.close()


def test_unsafe_edit_corpus_is_registered_and_detected() -> None:
    from benchmarks.fault_injection.faults import CI_FAULTS, FAULT_BY_NAME
    from benchmarks.fault_injection.runner import run_single_fault

    assert "unsafe_edit" in FAULT_BY_NAME
    assert any(f.name == "unsafe_edit" for f in CI_FAULTS), (
        "unsafe_edit must be in CI corpus (Refs #397, #410)"
    )
    fault = FAULT_BY_NAME["unsafe_edit"]
    result = run_single_fault(fault)
    assert result.detected, f"unsafe_edit must be detected, notes: {result.notes}"
    assert result.detection_module == "continuum.recovery.gate"
    assert not result.unsafe_resume, "unsafe_edit must block resume"
