from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.events import EventType
from continuum.models import Run, UnknownSideEffect
from continuum.storage.sqlite import SQLiteStorage
from recovery_audit.harness.fault_injection import InterleavePoint, InterleavingStorage


@pytest.fixture
def store_path(tmp_path: Path) -> str:
    path = str(tmp_path / "run.db")
    store = SQLiteStorage(path)
    store.create_run(Run(run_id="r1", goal="audit"))
    store.append_event("r1", EventType.RUN_STARTED, {"goal": "audit"})
    store.close()
    return path


def test_concurrent_claims_do_not_open_two_slots(store_path: str) -> None:
    """Two interleaved claims on one identity must not both succeed.

    This is the audit's acceptance criterion: an action whose outcome may be
    uncertain must not be given a second execution slot.
    """
    store_a = InterleavingStorage(store_path)
    store_b = SQLiteStorage(store_path)
    ledger_a = ActionLedger(store_a, "r1", holder_id="A")
    ledger_b = ActionLedger(store_b, "r1", holder_id="B")

    held: dict[str, object] = {}

    def worker_b_claims() -> None:
        try:
            held["outcome"] = ledger_b.claim("charge_card", {"amount": 10})
        except Exception as exc:
            held["exception"] = exc

    # B's whole claim runs inside A's, just before A's ACTION_RECORDED is
    # written: after A's fold has already decided the slot is free.
    store_a.arm(InterleavePoint("append_event", 1), worker_b_claims)
    outcome_a = None
    error_a = None
    try:
        outcome_a = ledger_a.claim("charge_card", {"amount": 10})
    except Exception as exc:
        error_a = exc
    outcome_b = held.get("outcome")
    error_b = held.get("exception")

    assert store_a.fired, "the interleave did not run; the test is misconfigured"
    fresh_a = bool(getattr(outcome_a, "fresh", False))
    fresh_b = bool(getattr(outcome_b, "fresh", False))

    # At most one worker may open a fresh slot:
    assert (int(fresh_a) + int(fresh_b)) == 1, (
        f"expected exactly 1 fresh slot, got fresh_a={fresh_a}, fresh_b={fresh_b}"
    )
    loser = error_a if error_a is not None else error_b
    assert isinstance(loser, UnknownSideEffect), (
        f"expected loser to raise UnknownSideEffect, got {loser}"
    )


def test_losing_worker_is_refused_or_raises_unknown_side_effect(store_path: str) -> None:
    """When two claims race, exactly one wins fresh=True and the other is refused."""
    store_a = InterleavingStorage(store_path)
    store_b = SQLiteStorage(store_path)
    ledger_a = ActionLedger(store_a, "r1", holder_id="A")
    ledger_b = ActionLedger(store_b, "r1", holder_id="B")

    held: dict[str, object] = {}

    def worker_b_claims() -> None:
        try:
            held["outcome"] = ledger_b.claim("charge_card", {"amount": 10})
        except Exception as exc:
            held["exception"] = exc

    store_a.arm(InterleavePoint("append_event", 1), worker_b_claims)
    outcome_a = None
    error_a = None
    try:
        outcome_a = ledger_a.claim("charge_card", {"amount": 10})
    except Exception as exc:
        error_a = exc

    outcome_b = held.get("outcome")
    error_b = held.get("exception")

    fresh_a = bool(getattr(outcome_a, "fresh", False))
    fresh_b = bool(getattr(outcome_b, "fresh", False))
    assert int(fresh_a) + int(fresh_b) == 1
    loser = error_a if error_a is not None else error_b
    assert isinstance(loser, UnknownSideEffect)


def test_loser_subsequent_retry_finds_existing_slot(store_path: str) -> None:
    """After a race, the winning slot is durable and a subsequent claim sees it."""
    store_a = InterleavingStorage(store_path)
    store_b = SQLiteStorage(store_path)
    ledger_a = ActionLedger(store_a, "r1", holder_id="A")
    ledger_b = ActionLedger(store_b, "r1", holder_id="B")

    held: dict[str, object] = {}

    def worker_b_claims() -> None:
        try:
            held["outcome"] = ledger_b.claim("charge_card", {"amount": 10})
        except Exception as exc:
            held["exception"] = exc

    store_a.arm(InterleavePoint("append_event", 1), worker_b_claims)
    with contextlib.suppress(Exception):
        ledger_a.claim("charge_card", {"amount": 10})

    # Re-claiming must now raise UnknownSideEffect because the slot is STARTED
    with pytest.raises(UnknownSideEffect):
        ledger_b.claim("charge_card", {"amount": 10})
