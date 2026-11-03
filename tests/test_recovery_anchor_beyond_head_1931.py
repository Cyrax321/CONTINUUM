from __future__ import annotations

import pytest

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.recovery.gate import MergePreconditionError
from continuum.recovery.merge import approve_merge
from continuum.recovery.restore import approve_restore
from continuum.storage import SQLiteStorage


def test_anchor_beyond_head_refused_reproduction_1931() -> None:
    """Issue #1931 reproduction: an anchor beyond head must be refused rather than silently approved."""
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="r", goal="g"))
    store.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    # An unsettled human approval sitting in the tail: the gate exists to catch this.
    store.append_event(
        "r",
        EventType.APPROVAL_GRANTED,
        {"approval_id": "AP-1", "subject": "deploy"},
        source=Origin.HUMAN,
    )

    # 1. A legitimate anchor at 0: span is (0, 2] -> gate refuses due to unsettled approval.
    with pytest.raises(MergePreconditionError):
        approve_merge(store, "r", anchor_sequence=0, reason="legit")

    # 2. An anchor beyond head: previously shrank span to empty and silently approved.
    # Now it must be rejected with ValueError naming the head.
    with pytest.raises(
        ValueError,
        match=r"anchor sequence 10000 is beyond run 'r' head \d+; an edit must sit inside the run's history",
    ):
        approve_merge(store, "r", anchor_sequence=10_000, reason="bypass")

    with pytest.raises(
        ValueError,
        match=r"anchor sequence 10000 is beyond run 'r' head \d+; an edit must sit inside the run's history",
    ):
        approve_restore(store, "r", anchor_sequence=10_000, reason="bypass")


def test_source_anchor_beyond_head_refused() -> None:
    """approve_merge must refuse a source_anchor_sequence beyond the source run's head."""
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="target", goal="g1"))
    store.append_event("target", EventType.RUN_STARTED, {"goal": "g1"}, source=Origin.DETERMINISTIC)

    store.create_run(Run(run_id="source", goal="g2"))
    store.append_event("source", EventType.RUN_STARTED, {"goal": "g2"}, source=Origin.DETERMINISTIC)

    with pytest.raises(ValueError, match="is beyond run 'source' head"):
        approve_merge(
            store,
            "target",
            anchor_sequence=0,
            source_run_id="source",
            source_anchor_sequence=100,
            reason="cross-merge",
        )


def test_anchor_equal_head_permitted() -> None:
    """anchor == head is a valid checkpoint covering head and should not raise ValueError."""
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="r1", goal="g"))
    store.append_event("r1", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    head = store.last_sequence("r1")

    # Should not raise ValueError about anchor beyond head
    run = approve_restore(store, "r1", anchor_sequence=head, reason="at-head")
    assert run.run_id == "r1"
