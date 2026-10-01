"""Source-aware merge gate (#494, #389).

Merge must derive both sides: target (anchor, target_head] and source
(source_anchor, source_head] where source_anchor is common ancestor or
source latest checkpoint if absent. Merge refuses if either side has
unaccounted preconditions (union) and carry_forward may name items from
either side. Per-edit filtering keeps fork semantics for depended_results.
"""

from __future__ import annotations

import pytest

from continuum.actions import ActionLedger
from continuum.actions.idempotency import idempotency_key
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.gate import (
    EditPreconditionError,
    MergePreconditionError,
    check_merge_preconditions,
)
from continuum.recovery.merge import approve_merge
from continuum.storage import SQLiteStorage


def _make_run(storage: SQLiteStorage, run_id: str) -> None:
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})


def test_source_completed_target_dependent_blocks_and_names_source_sequence() -> None:
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        # Source side: completed result
        ledger_src = ActionLedger(storage, "source")
        outcome = ledger_src.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger_src.complete(outcome.key, external_id="42")
        expected_key = idempotency_key(
            "github.create_issue", {"title": "t"}, scope="source", key="k1"
        )
        src_seq = storage.last_sequence("source")
        # Target side: surviving dependent referencing source key
        storage.append_event(
            "target",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [expected_key]},
        )
        # Merge must refuse, error names source sequence and both anchors
        with pytest.raises(EditPreconditionError) as exc:
            approve_merge(
                storage,
                "target",
                source_run_id="source",
                anchor_sequence=0,
                reason="cross",
            )
        err = exc.value
        # Old code would have passed (target-only derivation empty), new code blocks
        assert hasattr(err, "rationale")
        rationale = err.rationale  # type: ignore[attr-defined]
        assert str(src_seq) in str(err) or str(src_seq) in str(rationale)
        assert any(d["sequence"] == src_seq for d in rationale["depended_results"])
        # Rationale must name both sides
        assert "target_anchor_sequence" in rationale
        assert "source_anchor_sequence" in rationale
        assert "target_candidate_sequence" in rationale
        assert "source_candidate_sequence" in rationale
        assert rationale["source_run_id"] == "source"
        assert "target (anchor" in str(err) and "source (anchor" in str(err)
    finally:
        storage.close()


def test_source_depended_with_carry_forward_passes_and_lineage_stamps() -> None:
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        ledger_src = ActionLedger(storage, "source")
        outcome = ledger_src.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger_src.complete(outcome.key, external_id="42")
        expected_key = idempotency_key(
            "github.create_issue", {"title": "t"}, scope="source", key="k1"
        )
        storage.append_event(
            "target",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [expected_key]},
        )
        # With carry_forward containing source key, merge passes
        merged = approve_merge(
            storage,
            "target",
            source_run_id="source",
            anchor_sequence=0,
            reason="carry source",
            carry_forward=[expected_key],
        )
        assert merged.run_id == "target"
        events = storage.read_events("target")
        lineage = [e for e in events if e.type is EventType.RUN_MERGED][-1]
        assert expected_key in lineage.payload["carry_forward"]
        # Lineage stamps both sides' summaries
        assert "target_preconditions" in lineage.payload
        assert "source_preconditions" in lineage.payload
        assert "target_summary" in lineage.payload
        assert "source_summary" in lineage.payload
        # Union summary still records the carried depended result for audit,
        # but the check passed because it was accounted via carry_forward
        assert len(lineage.payload["preconditions"]["depended_results"]) == 1
        assert lineage.payload["preconditions"]["depended_results"][0]["key"] == expected_key
        assert lineage.payload["source_preconditions"] is not None
        assert lineage.payload["target_preconditions"] is not None
        assert lineage.payload["source_anchor_sequence"] is not None
        assert lineage.payload["target_anchor_sequence"] is not None
    finally:
        storage.close()


def test_source_completion_reconciled_away_does_not_strand_merge() -> None:
    """A source completion reconciled to absent is not a live depended result (#1505).

    ``reconcile(occurred=False)`` folds an already-COMPLETED action to FAILED and
    clears its receipt, so the effect no longer exists. Stranding it is then
    impossible and the merge must pass. The cross-run fold has to follow the
    newest status, as ``derive()`` does, instead of keeping the first COMPLETED
    entry for the key.
    """
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        ledger_src = ActionLedger(storage, "source")
        outcome = ledger_src.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger_src.complete(outcome.key, external_id="42")
        expected_key = idempotency_key(
            "github.create_issue", {"title": "t"}, scope="source", key="k1"
        )
        storage.append_event(
            "target",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [expected_key]},
        )
        # Control: with the completion still live, the merge is refused.
        with pytest.raises(EditPreconditionError):
            approve_merge(
                storage,
                "target",
                source_run_id="source",
                anchor_sequence=0,
                reason="cross",
            )
        # The probe contradicts the optimistic complete(): no external effect.
        ledger_src.reconcile(outcome.key, occurred=False)
        # Nothing is stranded now, so the same merge succeeds.
        merged = approve_merge(
            storage,
            "target",
            source_run_id="source",
            anchor_sequence=0,
            reason="cross",
        )
        assert merged.run_id == "target"
    finally:
        storage.close()


def test_source_completion_compensated_away_does_not_strand_merge() -> None:
    """A compensated source completion is not a live depended result either (#1505).

    Compensation undoes the effect deliberately, so a target step referencing it
    cannot be stranded by the merge. Same newest-status-wins rule, reached
    through the compensate path rather than reconcile.
    """
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        ledger_src = ActionLedger(storage, "source")
        outcome = ledger_src.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger_src.complete(outcome.key, external_id="42")
        expected_key = idempotency_key(
            "github.create_issue", {"title": "t"}, scope="source", key="k1"
        )
        storage.append_event(
            "target",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [expected_key]},
        )
        with pytest.raises(EditPreconditionError):
            approve_merge(
                storage,
                "target",
                source_run_id="source",
                anchor_sequence=0,
                reason="cross",
            )
        ledger_src.compensate(outcome.key)
        merged = approve_merge(
            storage,
            "target",
            source_run_id="source",
            anchor_sequence=0,
            reason="cross",
        )
        assert merged.run_id == "target"
    finally:
        storage.close()


def test_clean_merge_of_two_branches_passes_and_stamps_both_summaries() -> None:
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        # No outstanding items on either side
        merged = approve_merge(storage, "target", source_run_id="source", reason="clean")
        assert merged is not None
        events = storage.read_events("target")
        lineage = [e for e in events if e.type is EventType.RUN_MERGED][-1]
        # Both sides summaries present and empty
        assert lineage.payload["source_preconditions"]["unsettled_authorizations"] == []
        assert lineage.payload["source_preconditions"]["depended_results"] == []
        assert lineage.payload["source_preconditions"]["uncertain_slots"] == []
        assert lineage.payload["target_preconditions"]["unsettled_authorizations"] == []
        assert lineage.payload["target_preconditions"]["depended_results"] == []
        assert lineage.payload["target_preconditions"]["uncertain_slots"] == []
        assert lineage.payload["preconditions"]["unsettled_authorizations"] == []
        assert lineage.payload["preconditions"]["depended_results"] == []
        assert lineage.payload["preconditions"]["uncertain_slots"] == []
        assert lineage.payload["source_run_id"] == "source"
        # Also check the merge gate stamps similarly when source given
        storage2 = SQLiteStorage(":memory:")
        try:
            _make_run(storage2, "t2")
            _make_run(storage2, "s2")
            derivation, carry_set, summary, _t2, _s2 = check_merge_preconditions(
                storage2, target_run_id="t2", target_anchor=0, source_run_id="s2"
            )
            assert summary["unsettled_authorizations"] == []
            assert summary["depended_results"] == []
        finally:
            storage2.close()
    finally:
        storage.close()


def test_source_run_id_none_only_target_checked_backward_compat() -> None:
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        # Target has uncertain slot, no source
        ledger = ActionLedger(storage, "target")
        outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
        seq = storage.last_sequence("target")
        with pytest.raises(EditPreconditionError) as exc:
            approve_merge(storage, "target", reason="target only", anchor_sequence=0)
        assert str(seq) in str(exc.value)
        # Carry by target action_id unblocks
        merged = approve_merge(
            storage,
            "target",
            reason="carry target",
            anchor_sequence=0,
            carry_forward=[outcome.action.action_id],
        )
        assert merged is not None
        # Same via check_merge_preconditions with source None
        storage2 = SQLiteStorage(":memory:")
        try:
            _make_run(storage2, "t2")
            ledger2 = ActionLedger(storage2, "t2")
            out2 = ledger2.claim("slack.notify", {"channel": "#ops"}, key="k1")
            with pytest.raises(EditPreconditionError):
                check_merge_preconditions(
                    storage2, target_run_id="t2", target_anchor=0, source_run_id=None
                )
            # Should pass with carry
            _, _, _, _, _ = check_merge_preconditions(
                storage2,
                target_run_id="t2",
                target_anchor=0,
                source_run_id=None,
                carry_forward=[out2.action.action_id],
            )
        finally:
            storage2.close()
    finally:
        storage.close()


@pytest.mark.parametrize(
    "kind",
    ["unsettled_authorization", "depended_result", "uncertain_slot"],
)
def test_source_side_each_kind_blocks_and_carry_by_key_action_sequence(kind: str) -> None:
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        if kind == "unsettled_authorization":
            storage.append_event(
                "source",
                EventType.APPROVAL_GRANTED,
                {"approval_id": "ap-1", "subject": "ship"},
            )
            seq = storage.last_sequence("source")
            with pytest.raises(EditPreconditionError) as exc:
                approve_merge(storage, "target", source_run_id="source", reason="block")
            assert str(seq) in str(exc.value)
            assert "ap-1" in str(exc.value)
            # carry by approval_id
            approve_merge(
                storage,
                "target",
                source_run_id="source",
                reason="carry ap",
                carry_forward=["ap-1"],
            )
            # Must also work by sequence string
            storage2 = SQLiteStorage(":memory:")
            try:
                _make_run(storage2, "t2")
                _make_run(storage2, "s2")
                storage2.append_event(
                    "s2",
                    EventType.APPROVAL_GRANTED,
                    {"approval_id": "ap-2", "subject": "ship"},
                )
                seq2 = storage2.last_sequence("s2")
                with pytest.raises(EditPreconditionError):
                    approve_merge(storage2, "t2", source_run_id="s2", reason="block2")
                approve_merge(
                    storage2,
                    "t2",
                    source_run_id="s2",
                    reason="carry seq",
                    carry_forward=[str(seq2)],
                )
            finally:
                storage2.close()
        elif kind == "depended_result":
            ledger = ActionLedger(storage, "source")
            out = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
            ledger.complete(out.key, external_id="42")
            storage.append_event(
                "source",
                EventType.WORK_ADDED,
                {"task_id": "w1", "prerequisite": [out.key]},
            )
            with pytest.raises(EditPreconditionError) as exc:
                approve_merge(storage, "target", source_run_id="source", reason="block")
            assert "depended" in str(exc.value).lower()
            # carry by key, action_id, sequence each unblock (per-run scope)
            for carry_kind in ["key", "action_id", "sequence"]:
                storage2 = SQLiteStorage(":memory:")
                try:
                    _make_run(storage2, "t2")
                    _make_run(storage2, "s2")
                    ledger2 = ActionLedger(storage2, "s2")
                    out2 = ledger2.claim("github.create_issue", {"title": "t"}, key="k1")
                    ledger2.complete(out2.key, external_id="42")
                    storage2.append_event(
                        "s2",
                        EventType.WORK_ADDED,
                        {"task_id": "w1", "prerequisite": [out2.key]},
                    )
                    if carry_kind == "key":
                        carry_val = out2.key
                    elif carry_kind == "action_id":
                        carry_val = out2.action.action_id
                    else:
                        carry_val = str(storage2.last_sequence("s2") - 1)
                    approve_merge(
                        storage2,
                        "t2",
                        source_run_id="s2",
                        reason="carry",
                        carry_forward=[carry_val],
                    )
                finally:
                    storage2.close()
            # Clean carry test already done
        else:  # uncertain_slot
            ledger = ActionLedger(storage, "source")
            out = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
            seq = storage.last_sequence("source")
            with pytest.raises(EditPreconditionError) as exc:
                approve_merge(storage, "target", source_run_id="source", reason="block")
            assert str(seq) in str(exc.value)
            # carry by key, action_id, sequence
            for carry in [out.key, out.action.action_id, str(seq)]:
                storage2 = SQLiteStorage(":memory:")
                try:
                    _make_run(storage2, "t2")
                    _make_run(storage2, "s2")
                    ledger2 = ActionLedger(storage2, "s2")
                    out2 = ledger2.claim("slack.notify", {"channel": "#ops"}, key="k1")
                    seq2 = storage2.last_sequence("s2")
                    carry_val = carry
                    if carry == out.key:
                        carry_val = out2.key
                    elif carry == out.action.action_id:
                        carry_val = out2.action.action_id
                    else:
                        carry_val = str(seq2)
                    with pytest.raises(EditPreconditionError):
                        approve_merge(storage2, "t2", source_run_id="s2", reason="block2")
                    approve_merge(
                        storage2,
                        "t2",
                        source_run_id="s2",
                        reason="carry",
                        carry_forward=[carry_val],
                    )
                finally:
                    storage2.close()
    finally:
        storage.close()


def test_merge_gate_with_source_union() -> None:
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        ledger = ActionLedger(storage, "source")
        out = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
        with pytest.raises(EditPreconditionError):
            check_merge_preconditions(
                storage, target_run_id="target", target_anchor=0, source_run_id="source"
            )
        # carry by source key passes
        _, _, summary, _t, _s = check_merge_preconditions(
            storage,
            target_run_id="target",
            target_anchor=0,
            source_run_id="source",
            carry_forward=[out.key],
        )
        assert summary is not None
    finally:
        storage.close()


def test_cross_run_merge_refusal_raises_merge_subclass() -> None:
    """The cross-run arm raises MergePreconditionError, not just the base (#1114).

    ``check_merge_preconditions`` with a ``source_run_id`` computes the union
    derivation itself rather than going through ``check_preconditions``, so it
    has its own raise site. A caller who catches ``MergePreconditionError`` to
    handle a merge refusal specifically must catch this path too.
    """
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        ledger_src = ActionLedger(storage, "source")
        outcome = ledger_src.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger_src.complete(outcome.key, external_id="42")
        expected_key = idempotency_key(
            "github.create_issue", {"title": "t"}, scope="source", key="k1"
        )
        storage.append_event(
            "target",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [expected_key]},
        )
        # Caught as the merge subclass, which is the contract callers rely on
        with pytest.raises(MergePreconditionError) as exc:
            approve_merge(
                storage,
                "target",
                source_run_id="source",
                anchor_sequence=0,
                reason="cross",
            )
        err = exc.value
        assert type(err) is MergePreconditionError
        assert err.edit_type == "merge"
        # Still an EditPreconditionError, so the base-class catch-all still works
        assert isinstance(err, EditPreconditionError)
        assert "depended" in str(err).lower()

        # Same site reached directly through the gate function
        with pytest.raises(MergePreconditionError) as exc:
            check_merge_preconditions(
                storage,
                target_run_id="target",
                target_anchor=0,
                source_run_id="source",
            )
        assert type(exc.value) is MergePreconditionError
        assert exc.value.rationale["source_run_id"] == "source"
    finally:
        storage.close()


def test_old_code_would_have_passed_incorrectly_new_blocks() -> None:
    """Demonstrate bug: target-only check passes, source-aware check blocks."""
    storage = SQLiteStorage(":memory:")
    try:
        _make_run(storage, "target")
        _make_run(storage, "source")
        ledger_src = ActionLedger(storage, "source")
        outcome = ledger_src.claim("github.create_issue", {"title": "t"}, key="k1")
        ledger_src.complete(outcome.key, external_id="42")
        expected_key = idempotency_key(
            "github.create_issue", {"title": "t"}, scope="source", key="k1"
        )
        storage.append_event(
            "target",
            EventType.WORK_ADDED,
            {"task_id": "w1", "prerequisite": [expected_key]},
        )
        # Old code: only target side, no depended on target alone, would pass
        from continuum.recovery.gate import check_preconditions

        # Target-only derivation should NOT block (old behavior)
        check_preconditions(storage, "target", 0, edit_type="merge")  # should not raise
        # New code with source must block
        with pytest.raises(EditPreconditionError) as exc:
            approve_merge(
                storage,
                "target",
                source_run_id="source",
                anchor_sequence=0,
                reason="new should block",
            )
        assert "depended" in str(exc.value).lower()
        assert str(storage.last_sequence("source")) in str(exc.value)
    finally:
        storage.close()
