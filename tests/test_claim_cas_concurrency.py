"""Claim must compare-and-set its record, not just claim-then-check (issue #1889).

``ActionLedger.claim`` resolves the folded log, decides a slot is free, and only
then appends. If another worker appends between the fold and the record, the
claim is deciding against a stale log: both workers see an unoccupied slot,
both append, and both perform the side effect.

The tests below pin the log head at resolution time and pass it to
``append_event`` as ``expected_sequence``. The losing claim must be rejected by
the storage-level compare-and-set, re-fold against the new head, discover the
winning claim, and raise ``UnknownSideEffect`` instead of opening a second
slot. An unrelated concurrent event must be absorbed by a retry rather than
reported as a conflict.

The interleaving is forced deterministically with events rather than guessed at
with timing, and ``atomic_claim`` is neutralised to the ``Storage`` base-class
no-op so the only thing standing between the two workers is the compare-and-set
itself. A backend that does not implement ``atomic_claim`` is exactly the
residual the issue describes; the ledger's safety must not depend on the
backend wrapping it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.events import EventType
from continuum.models import ActionStatus, Run, UnknownSideEffect
from continuum.storage import SQLiteStorage


@contextmanager
def _noop_atomic_claim(run_id: str | None = None, key: str | None = None) -> Iterator[None]:
    """The ``Storage`` base-class default: resolve and append with no transaction."""
    yield


@pytest.fixture
def two_workers(tmp_path: Any) -> tuple[SQLiteStorage, ActionLedger, ActionLedger]:
    """Two ledgers over one store, each with its own connection, no lease held.

    Mirrors the issue's reproduction: separate workers, no coordinator lease,
    racing an identical action identity.
    """
    db = tmp_path / "claim_cas.db"
    store_a = SQLiteStorage(db)
    store_a.create_run(Run(run_id="run_cas", goal="issue 1889"))
    store_a.append_event("run_cas", EventType.RUN_STARTED, {"goal": "issue 1889"})
    store_b = SQLiteStorage(db)
    # B's backend does not wrap claim in a transaction (the base default), so
    # nothing serialises the two workers except the storage compare-and-set.
    store_b.atomic_claim = _noop_atomic_claim  # type: ignore[method-assign]
    ledger_a = ActionLedger(store_a, "run_cas", holder_id="A")
    ledger_b = ActionLedger(store_b, "run_cas", holder_id="B")
    yield store_a, ledger_a, ledger_b
    store_a.close()
    store_b.close()


def _started_records(store: SQLiteStorage) -> list[Any]:
    """ACTION_RECORDED events that opened an in-flight slot (status STARTED)."""
    return [
        event
        for event in store.read_events("run_cas")
        if event.type is EventType.ACTION_RECORDED
        and (event.payload or {}).get("status") == ActionStatus.STARTED.value
    ]


def test_sequential_claims_of_one_identity_open_one_slot(two_workers: tuple[Any, ...]) -> None:
    """The issue's reproduction: the second claim must not report a fresh slot."""
    _store, ledger_a, ledger_b = two_workers

    outcome_a = ledger_a.claim("charge_card", {"amount": 10})
    assert outcome_a.fresh

    with pytest.raises(UnknownSideEffect):
        ledger_b.claim("charge_card", {"amount": 10})

    actions = ledger_a.all()
    assert len(actions) == 1
    assert actions[0].status is ActionStatus.UNKNOWN


def test_claim_losing_the_race_does_not_open_a_second_slot(
    two_workers: tuple[Any, ...],
) -> None:
    """B folds before A's append commits; the compare-and-set must reject B's stale write.

    On the pre-fix claim-then-check path both workers record an ACTION_RECORDED
    with status STARTED and both return ``fresh=True``, which is the duplicate
    side effect the ledger exists to prevent.
    """
    store, ledger_a, ledger_b = two_workers
    real_resolve_claim = ledger_b.resolve_claim

    b_folded = threading.Event()
    a_recorded = threading.Event()
    b_resolutions = 0

    def b_resolve_claim(action_type: str, arguments: Any = None, **kwargs: Any) -> Any:
        """Fold first, then let A claim, so B's resolution is against a stale log."""
        nonlocal b_resolutions
        resolution = real_resolve_claim(action_type, arguments, **kwargs)
        b_resolutions += 1
        if b_resolutions == 1:
            b_folded.set()
            assert a_recorded.wait(timeout=30), "worker A never recorded its claim"
        return resolution

    ledger_b.resolve_claim = b_resolve_claim  # type: ignore[method-assign]

    results: dict[str, Any] = {}

    def worker_a() -> None:
        assert b_folded.wait(timeout=30), "worker B never folded the log"
        results["a"] = ledger_a.claim("charge_card", {"amount": 10})
        a_recorded.set()

    def worker_b() -> None:
        try:
            results["b"] = ledger_b.claim("charge_card", {"amount": 10})
        except UnknownSideEffect as exc:
            results["b"] = exc

    thread_a = threading.Thread(target=worker_a)
    thread_b = threading.Thread(target=worker_b)
    thread_b.start()
    thread_a.start()
    thread_a.join(timeout=60)
    thread_b.join(timeout=60)
    assert not thread_a.is_alive() and not thread_b.is_alive(), "claim workers deadlocked"

    assert results["a"].fresh, "A won the race and must hold the only fresh slot"
    assert isinstance(results["b"], UnknownSideEffect), (
        f"B must defer to the winning claim, not open a second slot (got {results['b']!r})"
    )

    started = _started_records(store)
    assert len(started) == 1, (
        f"exactly one in-flight slot may exist for one identity, found {len(started)}"
    )

    actions = ledger_a.all()
    assert len(actions) == 1
    assert actions[0].status is ActionStatus.UNKNOWN, "B must mark the winning claim uncertain"
    pending = ledger_a.pending()
    assert len(pending) == 1 and pending[0].status is ActionStatus.UNKNOWN
    assert store.verify_events("run_cas").ok


def test_unrelated_concurrent_write_is_retried_not_reported_as_conflict(
    two_workers: tuple[Any, ...],
) -> None:
    """A concurrent event unrelated to the key must not fail the claim.

    The compare-and-set rejects the stale write, the re-fold finds no competing
    claim for this identity, and the claim retries against the new head and
    succeeds. A spurious failure here would make CAS-capable backends strictly
    less reliable than the claim-then-check path they replace.
    """
    store, _ledger_a, ledger_b = two_workers
    real_resolve_claim = ledger_b.resolve_claim

    wrote_unrelated = False

    def b_resolve_claim(action_type: str, arguments: Any = None, **kwargs: Any) -> Any:
        """After B resolves, land an unrelated event before B records."""
        nonlocal wrote_unrelated
        resolution = real_resolve_claim(action_type, arguments, **kwargs)
        if not wrote_unrelated:
            wrote_unrelated = True
            store.append_event(
                "run_cas",
                EventType.PERCEPTION_OBSERVED,
                {"content_hash": "unrelated-to-the-claimed-action"},
            )
        return resolution

    ledger_b.resolve_claim = b_resolve_claim  # type: ignore[method-assign]

    outcome = ledger_b.claim("charge_card", {"amount": 10})

    assert outcome.fresh, "a concurrent unrelated event must be absorbed by a retry"
    assert outcome.action.status is ActionStatus.STARTED
    actions = ledger_b.all()
    assert len(actions) == 1
    assert len(_started_records(store)) == 1
    assert store.verify_events("run_cas").ok
