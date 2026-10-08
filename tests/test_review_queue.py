"""Tests for the deferred review queue and priority batching (issue #1410).

Covers ReviewItem properties, ReviewQueue enqueuing, priority sorting,
separation of immediate blockers from parked background items, individual
and bulk low-risk approval, durable event recording, and replay.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.review_queue import ReviewQueue
from continuum.storage.sqlite import SQLiteStorage


@pytest.fixture
def storage(tmp_path: Path) -> SQLiteStorage:
    db = tmp_path / "test.db"
    store = SQLiteStorage(f"sqlite:///{db}")
    store.create_run(Run(run_id="run_1", goal="test deferred review queue"))
    return store


@pytest.fixture
def queue(storage: SQLiteStorage) -> ReviewQueue:
    policy = {
        "hourly_prompt_cap": 10,
        "batch_window_seconds": 3600,
        "blast_radius_threshold": 0.8,
        "risk_weights": {
            "mem_delete": 0.9,
            "mem_write": 0.4,
            "read_query": 0.1,
            "default": 0.0,
        },
    }
    return ReviewQueue(storage, policy=policy)


def test_enqueue_low_risk_action_is_parked(queue: ReviewQueue) -> None:
    item = queue.enqueue("run_1", "read_query", {"query": "SELECT 1"})
    assert item.action_type == "read_query"
    assert item.risk_score == 0.1
    assert item.immediate is False
    assert item.parked is True
    assert item.is_parked is True
    assert item.is_immediate is False
    assert item.is_pending is True
    assert item.status == "pending"
    assert item.batch_id == "batch_run_1"

    # Verifies durable event in storage
    events = queue.storage.read_all_events("run_1")
    req_events = [e for e in events if e.type is EventType.APPROVAL_REQUESTED]
    assert len(req_events) == 1
    assert req_events[0].payload["review_id"] == item.review_id
    assert req_events[0].payload["immediate"] is False
    assert req_events[0].payload["parked"] is True


def test_enqueue_high_risk_action_is_immediate_blocker(queue: ReviewQueue) -> None:
    item = queue.enqueue("run_1", "mem_delete", {"key": "user_data"})
    assert item.action_type == "mem_delete"
    assert item.risk_score == 0.9
    assert item.immediate is True
    assert item.parked is False
    assert item.is_immediate is True
    assert item.is_parked is False
    assert item.is_pending is True


def test_priority_sorting_in_list_pending(queue: ReviewQueue) -> None:
    # 1. Parked low risk (0.1), depth 0
    item_low = queue.enqueue("run_1", "read_query", dependency_depth=0)
    time.sleep(0.01)
    # 2. Parked medium risk (0.4), depth 0
    item_med1 = queue.enqueue("run_1", "mem_write", dependency_depth=0)
    time.sleep(0.01)
    # 3. Parked medium risk (0.4), depth 2 (deeper dependency)
    item_med2 = queue.enqueue("run_1", "mem_write", dependency_depth=2)
    time.sleep(0.01)
    # 4. Immediate blocker (0.9), depth 0
    item_imm = queue.enqueue("run_1", "mem_delete", dependency_depth=0)

    pending = queue.list_pending("run_1")
    assert len(pending) == 4

    # Ranking:
    # 1st: Immediate blocker (item_imm)
    # 2nd: Parked medium risk with depth 2 (item_med2)
    # 3rd: Parked medium risk with depth 0 (item_med1)
    # 4th: Parked low risk (item_low)
    assert pending[0].review_id == item_imm.review_id
    assert pending[1].review_id == item_med2.review_id
    assert pending[2].review_id == item_med1.review_id
    assert pending[3].review_id == item_low.review_id


def test_list_parked_and_list_immediate_filters(queue: ReviewQueue) -> None:
    queue.enqueue("run_1", "read_query")
    queue.enqueue("run_1", "mem_write")
    queue.enqueue("run_1", "mem_delete")

    parked = queue.list_parked("run_1")
    immediate = queue.list_immediate("run_1")

    assert len(parked) == 2
    assert all(it.parked for it in parked)
    assert len(immediate) == 1
    assert immediate[0].action_type == "mem_delete"
    assert immediate[0].immediate is True


def test_approve_individual_item(queue: ReviewQueue) -> None:
    item = queue.enqueue("run_1", "mem_delete")
    assert item.is_pending is True

    approved = queue.approve("run_1", item.review_id, reviewer="alice")
    assert approved.status == "approved"
    assert approved.approved_by == "alice"
    assert approved.approved_at is not None

    # Should no longer be in pending
    pending = queue.list_pending("run_1")
    assert len(pending) == 0

    # Durable event recorded
    events = queue.storage.read_all_events("run_1")
    granted_events = [e for e in events if e.type is EventType.APPROVAL_GRANTED]
    assert len(granted_events) == 1
    assert granted_events[0].payload["review_id"] == item.review_id
    assert granted_events[0].payload["granted_by"] == "alice"


def test_approve_unknown_item_raises(queue: ReviewQueue) -> None:
    with pytest.raises(KeyError, match="no review item with id 'ghost'"):
        queue.approve("run_1", "ghost")


def test_approve_low_risk_bulk(queue: ReviewQueue) -> None:
    queue.enqueue("run_1", "read_query")  # risk 0.1
    queue.enqueue("run_1", "mem_write")  # risk 0.4
    queue.enqueue("run_1", "mem_delete")  # risk 0.9 (immediate)

    # Bulk approve low risk with default threshold (0.8)
    approved = queue.approve_low_risk("run_1", reviewer="bob")
    assert len(approved) == 2
    approved_types = {it.action_type for it in approved}
    assert approved_types == {"read_query", "mem_write"}

    # Immediate item remains pending
    remaining = queue.list_pending("run_1")
    assert len(remaining) == 1
    assert remaining[0].action_type == "mem_delete"


def test_approve_low_risk_with_custom_threshold(queue: ReviewQueue) -> None:
    queue.enqueue("run_1", "read_query")  # risk 0.1
    queue.enqueue("run_1", "mem_write")  # risk 0.4

    # Threshold 0.2 only approves read_query
    approved = queue.approve_low_risk("run_1", max_risk=0.2, reviewer="carol")
    assert len(approved) == 1
    assert approved[0].action_type == "read_query"

    remaining = queue.list_pending("run_1")
    assert len(remaining) == 1
    assert remaining[0].action_type == "mem_write"


def test_state_reconstruction_and_replay(storage: SQLiteStorage) -> None:
    q1 = ReviewQueue(storage)
    it1 = q1.enqueue("run_1", "mem_write")
    it2 = q1.enqueue("run_1", "mem_delete")
    q1.approve("run_1", it1.review_id, reviewer="dave")

    # Second instance reconstructs exact state from log
    q2 = ReviewQueue(storage)
    pending = q2.list_pending("run_1")
    assert len(pending) == 1
    assert pending[0].review_id == it2.review_id

    item1_reloaded = q2.get_item("run_1", it1.review_id)
    assert item1_reloaded is not None
    assert item1_reloaded.status == "approved"
    assert item1_reloaded.approved_by == "dave"


def test_approval_revocation_marks_item_revoked(queue: ReviewQueue) -> None:
    item = queue.enqueue("run_1", "read_query")
    # Record revocation event
    queue.storage.append_event(
        "run_1",
        EventType.APPROVAL_REVOKED,
        {"approval_id": item.review_id, "review_id": item.review_id},
    )

    reloaded = queue.get_item("run_1", item.review_id)
    assert reloaded is not None
    assert reloaded.status == "revoked"
    assert reloaded.is_pending is False
    assert len(queue.list_pending("run_1")) == 0


def test_approve_revoked_item_refused(queue: ReviewQueue) -> None:
    # Approving a revoked item would flip it back to granted and silently undo
    # the revocation, so it must be refused rather than recorded.
    item = queue.enqueue("run_1", "read_query")
    queue.storage.append_event(
        "run_1",
        EventType.APPROVAL_REVOKED,
        {"approval_id": item.review_id, "review_id": item.review_id},
    )

    with pytest.raises(ValueError, match="already revoked"):
        queue.approve("run_1", item.review_id, reviewer="eve")

    # No grant event was appended, so replay still reads the item as revoked
    granted = [
        e for e in queue.storage.read_all_events("run_1") if e.type is EventType.APPROVAL_GRANTED
    ]
    assert granted == []
    reloaded = queue.get_item("run_1", item.review_id)
    assert reloaded is not None
    assert reloaded.status == "revoked"


def test_approve_already_approved_item_refused(queue: ReviewQueue) -> None:
    item = queue.enqueue("run_1", "read_query")
    queue.approve("run_1", item.review_id, reviewer="alice")

    with pytest.raises(ValueError, match="already approved"):
        queue.approve("run_1", item.review_id, reviewer="alice")

    granted = [
        e for e in queue.storage.read_all_events("run_1") if e.type is EventType.APPROVAL_GRANTED
    ]
    assert len(granted) == 1


def test_approve_low_risk_custom_threshold_never_approves_immediate_blocker(
    queue: ReviewQueue,
) -> None:
    # A custom threshold at or above the blast radius threshold must not turn
    # bulk low-risk approval into bulk blocker approval.
    queue.enqueue("run_1", "read_query")  # risk 0.1
    queue.enqueue("run_1", "mem_delete")  # risk 0.9 (immediate)

    approved = queue.approve_low_risk("run_1", max_risk=1.0, reviewer="bob")

    approved_types = {it.action_type for it in approved}
    assert approved_types == {"read_query"}

    # The immediate blocker survives and is still pending
    remaining = queue.list_pending("run_1")
    assert len(remaining) == 1
    assert remaining[0].action_type == "mem_delete"
    assert remaining[0].immediate is True
