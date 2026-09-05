"""Consumed-input refs for admissibility (issue #295, sub-issue #558).

Completed actions record which checkpoint components and prior actions they
consumed. Old rows without the field must remain admissible (default empty),
new rows must round-trip through ledger and storage, and validation must
accept empty lists but reject malformed commitments.
"""

from __future__ import annotations

import json

import pytest

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Action, ActionStatus, ConsumedInputs, Run
from continuum.storage import SQLiteStorage


def _new_store_with_run(run_id: str = "run_1") -> SQLiteStorage:
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id=run_id, goal="g"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return store


def test_old_row_without_consumed_inputs_is_admissible() -> None:
    """Old ledger rows without consumed_inputs must load as empty and be admissible."""
    # Simulate a legacy action payload missing the field (pre-#558)
    action = Action(run_id="run_1", action_type="a.do")
    payload = action.model_dump(mode="json")
    payload.pop("consumed_inputs", None)
    assert "consumed_inputs" not in payload
    # Loading must succeed and yield empty commitments
    restored = Action.model_validate(payload)
    assert restored.consumed_inputs == ConsumedInputs()
    assert restored.consumed_inputs.checkpoint_seq == 0
    assert restored.consumed_inputs.event_positions == []
    assert restored.consumed_inputs.action_ids == []


def test_old_event_without_field_survives_fold() -> None:
    store = _new_store_with_run("run_old")
    ActionLedger(store, "run_old")
    # Manually craft an ACTION_RECORDED event whose action dict lacks consumed_inputs
    action = Action(run_id="run_old", action_type="a.do", status=ActionStatus.COMPLETED)
    raw_payload = action.model_dump(mode="json")
    raw_payload.pop("consumed_inputs", None)
    store.append_event(
        "run_old",
        EventType.ACTION_RECORDED,
        {"key": "legacy-key", "action": raw_payload},
    )
    replayed = ActionLedger(store, "run_old")
    found = replayed.get("legacy-key")
    assert found is not None
    assert found.consumed_inputs == ConsumedInputs()
    store.close()


def test_new_row_round_trips_via_ledger() -> None:
    store = _new_store_with_run("run_new")
    ledger = ActionLedger(store, "run_new")
    outcome = ledger.claim("a.do", {"x": 1})
    ci = {"checkpoint_seq": 2, "event_positions": [1, 2, 3], "action_ids": ["action_abc"]}
    completed = ledger.complete(outcome.key, external_id="ext-1", consumed_inputs=ci)
    assert completed.consumed_inputs.checkpoint_seq == 2
    assert completed.consumed_inputs.event_positions == [1, 2, 3]
    assert completed.consumed_inputs.action_ids == ["action_abc"]

    # Round-trip via get
    fetched = ledger.get(outcome.key)
    assert fetched is not None
    assert fetched.consumed_inputs == ConsumedInputs(
        checkpoint_seq=2, event_positions=[1, 2, 3], action_ids=["action_abc"]
    )

    # Round-trip via fresh ledger replaying the same storage
    fresh = ActionLedger(store, "run_new")
    refetched = fresh.get(outcome.key)
    assert refetched is not None
    assert refetched.consumed_inputs == fetched.consumed_inputs
    store.close()


def test_consumed_inputs_stored_in_action_index_projection() -> None:
    store = _new_store_with_run("run_idx")
    ledger = ActionLedger(store, "run_idx")
    outcome = ledger.claim("a.do", {}, key="k-idx")
    ci = ConsumedInputs(checkpoint_seq=5, event_positions=[5], action_ids=["action_xyz"])
    ledger.complete(outcome.key, consumed_inputs=ci)

    # The projection's action_json must contain the commitment
    row = store._connection.execute(
        "SELECT action_json FROM action_index WHERE key = ?", (str(outcome.key),)
    ).fetchone()
    assert row is not None
    action_json = json.loads(row["action_json"])
    assert "consumed_inputs" in action_json
    assert action_json["consumed_inputs"]["checkpoint_seq"] == 5
    assert action_json["consumed_inputs"]["event_positions"] == [5]
    assert action_json["consumed_inputs"]["action_ids"] == ["action_xyz"]

    # Rebuild must preserve it
    store.rebuild_action_index()
    row2 = store._connection.execute(
        "SELECT action_json FROM action_index WHERE key = ?", (str(outcome.key),)
    ).fetchone()
    assert json.loads(row2["action_json"])["consumed_inputs"] == action_json["consumed_inputs"]
    store.close()


def test_empty_consumed_inputs_is_valid() -> None:
    empty = ConsumedInputs()
    assert empty.checkpoint_seq == 0
    assert empty.event_positions == []
    assert empty.action_ids == []

    # Action with default empty must validate
    a = Action(run_id="r", action_type="t", consumed_inputs=ConsumedInputs())
    assert a.consumed_inputs == empty

    # Also via dict form with empty lists
    a2 = Action.model_validate(
        {
            "run_id": "r",
            "action_type": "t",
            "consumed_inputs": {"checkpoint_seq": 0, "event_positions": [], "action_ids": []},
        }
    )
    assert a2.consumed_inputs == empty


def test_complete_accepts_both_mapping_and_model() -> None:
    store = _new_store_with_run("run_both")
    ledger = ActionLedger(store, "run_both")
    outcome = ledger.claim("a.do", {}, key="k-both")
    # Mapping form
    ledger.complete(
        outcome.key, consumed_inputs={"checkpoint_seq": 1, "event_positions": [], "action_ids": []}
    )
    fetched = ledger.get(outcome.key)
    assert fetched is not None
    assert fetched.consumed_inputs.checkpoint_seq == 1

    # Model form (update on already completed)
    ledger.complete(
        outcome.key,
        consumed_inputs=ConsumedInputs(checkpoint_seq=9, event_positions=[9], action_ids=["a1"]),
    )
    refetched = ledger.get(outcome.key)
    assert refetched is not None
    assert refetched.consumed_inputs.checkpoint_seq == 9
    store.close()


def test_validation_rejects_negative_checkpoint_seq() -> None:
    with pytest.raises(Exception, match="greater than or equal to 0"):
        ConsumedInputs(checkpoint_seq=-1)


def test_validation_rejects_negative_event_position() -> None:
    with pytest.raises(Exception, match="must be >= 0"):
        ConsumedInputs(event_positions=[-1])


def test_validation_rejects_too_many_action_ids() -> None:
    with pytest.raises(Exception, match="at most 32"):
        ConsumedInputs(action_ids=[f"action_{i}" for i in range(33)])


def test_validation_rejects_too_many_event_positions() -> None:
    with pytest.raises(Exception, match="at most 128"):
        ConsumedInputs(event_positions=list(range(129)))


def test_complete_validates_invalid_consumed_inputs() -> None:
    store = _new_store_with_run("run_val")
    ledger = ActionLedger(store, "run_val")
    outcome = ledger.claim("a.do", {}, key="k-val")
    with pytest.raises(Exception):  # noqa: B017
        ledger.complete(
            outcome.key,
            consumed_inputs={"checkpoint_seq": -5, "event_positions": [], "action_ids": []},
        )
    # Also via action_ids too long
    with pytest.raises(Exception):  # noqa: B017
        ledger.complete(
            outcome.key,
            consumed_inputs={"checkpoint_seq": 0, "event_positions": [], "action_ids": [""]},
        )
    store.close()


def test_reconcile_preserves_consumed_inputs() -> None:
    store = _new_store_with_run("run_rec")
    ledger = ActionLedger(store, "run_rec")
    outcome = ledger.claim("a.do", {}, key="k-rec")
    ledger.fail(outcome.key, "boom", certain=False)
    ci = ConsumedInputs(checkpoint_seq=3, event_positions=[3], action_ids=["action_prev"])
    reconciled = ledger.reconcile(outcome.key, occurred=True, consumed_inputs=ci)
    assert reconciled.consumed_inputs == ci
    assert ledger.get(outcome.key).consumed_inputs == ci
    store.close()


def test_complete_without_consumed_inputs_preserves_existing() -> None:
    store = _new_store_with_run("run_preserve")
    ledger = ActionLedger(store, "run_preserve")
    outcome = ledger.claim("a.do", {}, key="k-preserve")
    ci = {"checkpoint_seq": 4, "event_positions": [4], "action_ids": ["a4"]}
    ledger.complete(outcome.key, consumed_inputs=ci)
    # Repeat complete without supplying consumed_inputs must keep prior
    ledger.complete(outcome.key, external_id="ext-keep")
    fetched = ledger.get(outcome.key)
    assert fetched is not None
    assert fetched.consumed_inputs.checkpoint_seq == 4
    assert fetched.external_id == "ext-keep"
    store.close()
