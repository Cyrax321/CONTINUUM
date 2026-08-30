"""Tests for pure precondition derivation over event prefixes (issue #406).

Covers the three derivable sets (unsettled authorizations, depended results,
uncertain slots), the span semantics ((anchor, candidate], prefix visibility
stops at the candidate), the empty-range edge, the purity contract (storage is
only ever read) and the determinism property: identical prefixes derive equal
results, at every anchor and candidate pair.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from continuum.actions import ActionLedger
from continuum.events import Event, EventLog, EventType
from continuum.models import Run
from continuum.recovery.preconditions import (
    DependedResult,
    DerivationResult,
    EditPoint,
    UncertainSlot,
    UnsettledAuthorization,
    derive,
)
from continuum.storage import SQLiteStorage
from continuum.storage.base import RunNotFound

RUN_ID = "run_1"


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=RUN_ID, goal="g"))
    storage.append_event(RUN_ID, EventType.RUN_STARTED, {"goal": "g"})
    yield storage
    storage.close()


def grant(storage: SQLiteStorage, approval_id: str, subject: str = "deploy") -> Event:
    return storage.append_event(
        RUN_ID,
        EventType.APPROVAL_GRANTED,
        {"approval_id": approval_id, "subject": subject},
    )


def revoke(storage: SQLiteStorage, approval_id: str) -> Event:
    return storage.append_event(RUN_ID, EventType.APPROVAL_REVOKED, {"approval_id": approval_id})


def full_span(storage: SQLiteStorage, *, anchor: int = 1) -> EditPoint:
    """The span covering everything after ``anchor - 1`` up to the log head."""
    return EditPoint(
        run_id=RUN_ID, anchor_sequence=anchor - 1, candidate_sequence=storage.last_sequence(RUN_ID)
    )


# --- edit point validation -------------------------------------------------- #


def test_candidate_before_anchor_is_refused() -> None:
    with pytest.raises(ValidationError):
        EditPoint(run_id=RUN_ID, anchor_sequence=5, candidate_sequence=4)


def test_unknown_run_is_refused(store: SQLiteStorage) -> None:
    point = EditPoint(run_id="ghost", anchor_sequence=0, candidate_sequence=10)
    with pytest.raises(RunNotFound):
        derive(store, point)


# --- unsettled authorizations ------------------------------------------------


def test_grant_in_range_without_revocation_is_reported(store: SQLiteStorage) -> None:
    event = grant(store, "ap-1", subject="ship it")
    result = derive(store, full_span(store))
    assert result.unsettled_authorizations == frozenset(
        {UnsettledAuthorization(approval_id="ap-1", subject="ship it", sequence=event.sequence)}
    )


def test_revoked_authorization_is_not_reported(store: SQLiteStorage) -> None:
    grant(store, "ap-1")
    revoke(store, "ap-1")
    result = derive(store, full_span(store))
    assert result.unsettled_authorizations == frozenset()


def test_revocation_after_the_candidate_does_not_settle(store: SQLiteStorage) -> None:
    granted = grant(store, "ap-1")
    revoke(store, "ap-1")
    point = EditPoint(run_id=RUN_ID, anchor_sequence=0, candidate_sequence=granted.sequence)
    result = derive(store, point)
    assert {a.approval_id for a in result.unsettled_authorizations} == {"ap-1"}


def test_grant_at_or_before_the_anchor_is_not_reported(store: SQLiteStorage) -> None:
    early = grant(store, "ap-early")
    grant(store, "ap-late")
    point = EditPoint(
        run_id=RUN_ID,
        anchor_sequence=early.sequence,
        candidate_sequence=store.last_sequence(RUN_ID),
    )
    result = derive(store, point)
    assert {a.approval_id for a in result.unsettled_authorizations} == {"ap-late"}


def test_regrant_after_revocation_carries_the_new_sequence(store: SQLiteStorage) -> None:
    grant(store, "ap-1")
    revoke(store, "ap-1")
    again = grant(store, "ap-1", subject="second time")
    result = derive(store, full_span(store))
    assert result.unsettled_authorizations == frozenset(
        {UnsettledAuthorization(approval_id="ap-1", subject="second time", sequence=again.sequence)}
    )


# --- depended results ---------------------------------------------------------


def test_completed_action_referenced_by_a_later_step_is_reported(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
    ledger.complete(outcome.key, external_id="42", result={"issue": 42})
    completed_at = store.last_sequence(RUN_ID)
    store.append_event(
        RUN_ID,
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "notify requester", "prerequisite": [outcome.key]},
    )
    result = derive(store, full_span(store))
    assert result.depended_results == frozenset(
        {
            DependedResult(
                key=outcome.key,
                action_id=outcome.action.action_id,
                action_type="github.create_issue",
                sequence=completed_at,
            )
        }
    )


def test_action_claimed_before_the_anchor_but_completed_inside_the_span_is_reported(
    store: SQLiteStorage,
) -> None:
    """Membership is judged at the completion (#416 review thread, gitar-bot).

    The claim predates the anchor and survives the edit; the completion does
    not. A surviving step referencing the result would wait on an outcome the
    edit discarded, so the result belongs in the report even though the slot
    was opened before the anchor.
    """
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("mail.send", {"to": "a@example.com"}, key="k1")
    anchor = store.last_sequence(RUN_ID)
    ledger.complete(outcome.key, external_id="m1")
    completed_at = store.last_sequence(RUN_ID)
    store.append_event(
        RUN_ID,
        EventType.WORK_ADDED,
        {"task_id": "w2", "description": "tell the requester", "prerequisite": [outcome.key]},
    )
    result = derive(
        store,
        EditPoint(
            run_id=RUN_ID,
            anchor_sequence=anchor,
            candidate_sequence=store.last_sequence(RUN_ID),
        ),
    )
    assert result.depended_results == frozenset(
        {
            DependedResult(
                key=outcome.key,
                action_id=outcome.action.action_id,
                action_type="mail.send",
                sequence=completed_at,
            )
        }
    )


def test_action_id_reference_counts_as_a_dependency(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("mail.send", {"to": "a@example.com"}, key="k1")
    ledger.complete(outcome.key, external_id="m1")
    completed_at = store.last_sequence(RUN_ID)
    store.append_event(
        RUN_ID,
        EventType.DECISION_CREATED,
        {
            "decision_id": "d1",
            "decision": "proceed",
            "evidence": [outcome.action.action_id],
        },
    )
    result = derive(store, full_span(store))
    assert {d.sequence for d in result.depended_results} == {completed_at}


def test_unreferenced_completion_is_not_reported(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
    ledger.complete(outcome.key, external_id="42")
    result = derive(store, full_span(store))
    assert result.depended_results == frozenset()
    assert result.uncertain_slots == frozenset()


def test_compensation_past_the_candidate_does_not_release_the_result(store: SQLiteStorage) -> None:
    """A compensation recorded past the edit point is presumed rewritten by it."""
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
    ledger.complete(outcome.key, external_id="42")
    store.append_event(
        RUN_ID,
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "d", "prerequisite": [outcome.key]},
    )
    candidate = store.last_sequence(RUN_ID)
    ledger.compensate(outcome.key, note="undone later")
    point = EditPoint(run_id=RUN_ID, anchor_sequence=0, candidate_sequence=candidate)
    assert len(derive(store, point).depended_results) == 1


# --- uncertain slots -----------------------------------------------------------


def test_claim_never_settled_holds_an_uncertain_slot(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
    claimed_at = store.last_sequence(RUN_ID)
    result = derive(store, full_span(store))
    assert result.uncertain_slots == frozenset(
        {
            UncertainSlot(
                key=outcome.key,
                action_id=outcome.action.action_id,
                action_type="slack.notify",
                status="started",
                sequence=claimed_at,
            )
        }
    )


def test_uncertain_failure_keeps_the_slot_open_with_the_latest_status(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("mail.send", {"to": "a@example.com"}, key="k1")
    ledger.fail(outcome.key, "timeout after send", certain=False)
    failed_at = store.last_sequence(RUN_ID)
    result = derive(store, full_span(store))
    assert len(result.uncertain_slots) == 1
    slot = next(iter(result.uncertain_slots))
    assert slot.status == "unknown"
    assert slot.sequence == failed_at


def test_certain_failure_closes_the_slot(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("mail.send", {"to": "a@example.com"}, key="k1")
    ledger.fail(outcome.key, "rejected before send", certain=True)
    result = derive(store, full_span(store))
    assert result.uncertain_slots == frozenset()


def test_slot_opened_before_the_anchor_is_not_reported(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    ledger.claim("slack.notify", {"channel": "#ops"}, key="k-old")
    anchor = store.last_sequence(RUN_ID)
    late = ledger.claim("slack.notify", {"channel": "#dev"}, key="k-new")
    point = EditPoint(
        run_id=RUN_ID, anchor_sequence=anchor, candidate_sequence=store.last_sequence(RUN_ID)
    )
    result = derive(store, point)
    assert {s.key for s in result.uncertain_slots} == {late.key}


# --- span edges -----------------------------------------------------------------


def test_empty_range_returns_all_empty_sets(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
    grant(store, "ap-1")
    ledger.complete(outcome.key)
    store.append_event(
        RUN_ID,
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "d", "prerequisite": [outcome.key]},
    )
    head = store.last_sequence(RUN_ID)
    point = EditPoint(run_id=RUN_ID, anchor_sequence=head, candidate_sequence=head)
    assert derive(store, point) == DerivationResult()


def test_events_after_the_candidate_are_invisible(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
    ledger.complete(outcome.key)
    candidate = store.last_sequence(RUN_ID)
    grant(store, "ap-late")
    ledger.claim("slack.notify", {"channel": "#ops"}, key="k-late")
    store.append_event(
        RUN_ID,
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "d", "prerequisite": [outcome.key]},
    )
    point = EditPoint(run_id=RUN_ID, anchor_sequence=0, candidate_sequence=candidate)
    result = derive(store, point)
    assert result == DerivationResult()


def test_archived_events_inside_the_span_are_still_seen(store: SQLiteStorage) -> None:
    ledger = ActionLedger(store, RUN_ID)
    outcome = ledger.claim("github.create_issue", {"title": "t"}, key="k1")
    ledger.complete(outcome.key)
    compacted_through = store.last_sequence(RUN_ID)
    store.append_event(
        RUN_ID,
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "d", "prerequisite": [outcome.key]},
    )
    grant(store, "ap-1")
    store.compact_run(RUN_ID, through_sequence=compacted_through)
    result = derive(store, full_span(store))
    assert {d.key for d in result.depended_results} == {outcome.key}
    assert {a.approval_id for a in result.unsettled_authorizations} == {"ap-1"}


# --- purity -----------------------------------------------------------------------


class _RecordingStorage(SQLiteStorage):
    """SQLite storage that records every method the derivation touches."""

    def __init__(self) -> None:
        super().__init__(":memory:")
        self.calls: list[str] = []

    def _note(self, name: str) -> None:
        self.calls.append(name)

    # readers
    def get_run(self, run_id: str) -> Run:
        self._note("get_run")
        return super().get_run(run_id)

    def read_events(self, run_id: str, *, after_sequence: int = 0, upto: int | None = None) -> Any:
        self._note("read_events")
        return super().read_events(run_id, after_sequence=after_sequence, upto=upto)

    def read_archived_events(self, run_id: str) -> Any:
        self._note("read_archived_events")
        return super().read_archived_events(run_id)

    # mutators
    def append_event(self, *args: Any, **kwargs: Any) -> Any:
        self._note("append_event")
        return super().append_event(*args, **kwargs)

    def append_sealed(self, event: Event) -> Any:
        self._note("append_sealed")
        return super().append_sealed(event)

    def extend_events(self, events: Iterable[Event]) -> int:
        self._note("extend_events")
        return super().extend_events(events)

    def put_version(self, *args: Any, **kwargs: Any) -> Any:
        self._note("put_version")
        return super().put_version(*args, **kwargs)

    def put_checkpoint(self, *args: Any, **kwargs: Any) -> Any:
        self._note("put_checkpoint")
        return super().put_checkpoint(*args, **kwargs)

    def delete_checkpoint(self, *args: Any, **kwargs: Any) -> Any:
        self._note("delete_checkpoint")
        return super().delete_checkpoint(*args, **kwargs)

    def compact_run(self, *args: Any, **kwargs: Any) -> Any:
        self._note("compact_run")
        return super().compact_run(*args, **kwargs)

    def create_run(self, *args: Any, **kwargs: Any) -> Any:
        self._note("create_run")
        return super().create_run(*args, **kwargs)

    def create_run_started(self, *args: Any, **kwargs: Any) -> Any:
        self._note("create_run_started")
        return super().create_run_started(*args, **kwargs)

    def update_run(self, *args: Any, **kwargs: Any) -> Any:
        self._note("update_run")
        return super().update_run(*args, **kwargs)


def test_derive_only_reads_storage() -> None:
    storage = _RecordingStorage()
    try:
        storage.create_run(Run(run_id=RUN_ID, goal="g"))
        storage.append_event(RUN_ID, EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, RUN_ID)
        outcome = ledger.claim("slack.notify", {"channel": "#ops"}, key="k1")
        grant(storage, "ap-1")
        ledger.complete(outcome.key)
        storage.append_event(
            RUN_ID,
            EventType.WORK_ADDED,
            {"task_id": "w1", "description": "d", "prerequisite": [outcome.key]},
        )
        storage.calls.clear()

        point = EditPoint(
            run_id=RUN_ID, anchor_sequence=0, candidate_sequence=storage.last_sequence(RUN_ID)
        )
        derive(storage, point)

        assert set(storage.calls) <= {"get_run", "read_events", "read_archived_events"}
    finally:
        storage.close()


# --- determinism property ------------------------------------------------------------


def _action_payload(
    key: str, action_id: str, action_type: str, status: str, n: int
) -> dict[str, Any]:
    action = {
        "run_id": RUN_ID,
        "action_id": action_id,
        "action_type": action_type,
        "status": status,
        "arguments": {"n": str(n)},
    }
    return {
        "key": key,
        "action_id": action_id,
        "action_type": action_type,
        "status": status,
        "action": action,
    }


def _build_events(ops: list[tuple[Any, ...]]) -> tuple[Event, ...]:
    """Fold an operation script into one concrete, sealed event prefix."""
    log = EventLog()
    log.append(RUN_ID, EventType.RUN_STARTED, {"goal": "property"})
    claimed: set[int] = set()
    for op in ops:
        kind = op[0]
        n = op[1]
        if kind == "grant":
            log.append(
                RUN_ID,
                EventType.APPROVAL_GRANTED,
                {"approval_id": f"ap{n}", "subject": f"s{n}"},
            )
        elif kind == "revoke":
            log.append(RUN_ID, EventType.APPROVAL_REVOKED, {"approval_id": f"ap{n}"})
        elif kind == "claim":
            claimed.add(n)
            log.append(
                RUN_ID,
                EventType.ACTION_RECORDED,
                _action_payload(f"k{n}", f"act{n}", f"type{op[2] % 3}", "started", n),
            )
        elif kind == "complete" and n in claimed:
            log.append(
                RUN_ID,
                EventType.ACTION_RECORDED,
                _action_payload(f"k{n}", f"act{n}", f"type{n % 3}", "completed", n),
            )
        elif kind == "abandon" and n in claimed:
            log.append(
                RUN_ID,
                EventType.ACTION_RECORDED,
                _action_payload(f"k{n}", f"act{n}", f"type{n % 3}", "unknown", n),
            )
        elif kind == "depend":
            log.append(
                RUN_ID,
                EventType.WORK_ADDED,
                {"task_id": f"w{n}-{op[2]}", "description": "step", "prerequisite": [f"k{n}"]},
            )
        elif kind == "noise":
            log.append(
                RUN_ID,
                EventType.EVIDENCE_ADDED,
                {"evidence_id": f"e{n}-{len(log)}", "summary": "nothing"},
            )
    return tuple(log.events(RUN_ID))


_op = st.integers(0, 2)
_script = st.lists(
    st.one_of(
        st.tuples(st.just("grant"), _op),
        st.tuples(st.just("revoke"), _op),
        st.tuples(st.just("claim"), _op, _op),
        st.tuples(st.just("complete"), _op),
        st.tuples(st.just("abandon"), _op),
        st.tuples(st.just("depend"), _op, _op),
        st.tuples(st.just("noise"), _op),
    ),
    max_size=10,
)


def _store_with_prefix(events: tuple[Event, ...]) -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=RUN_ID, goal="property"))
    storage.extend_events(events)
    return storage


@settings(max_examples=25, deadline=None)
@given(ops=_script)
def test_identical_prefixes_derive_identically(ops: list[tuple[Any, ...]]) -> None:
    events = _build_events(ops)
    left = _store_with_prefix(events)
    right = _store_with_prefix(events)
    try:
        head = len(events)
        for anchor in range(head + 1):
            for candidate in range(anchor, head + 1):
                point = EditPoint(
                    run_id=RUN_ID, anchor_sequence=anchor, candidate_sequence=candidate
                )
                assert derive(left, point) == derive(right, point)
        point = EditPoint(run_id=RUN_ID, anchor_sequence=0, candidate_sequence=head)
        assert derive(left, point) == derive(left, point)
    finally:
        left.close()
        right.close()
