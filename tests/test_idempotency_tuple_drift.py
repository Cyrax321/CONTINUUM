"""Tuple-shaped arguments must not defeat drift-tolerant dedup (issue #1815).

``identity_tokens`` used to descend only into ``list`` and ``Mapping`` values,
so a resource named inside a tuple produced an empty token set. The ledger's
drift fallback then had nothing to compare and opened a second attempt slot,
letting the same external side effect fire twice. These are the end-to-end
consequences; the token-extraction unit tests live in
``tests/test_idempotency_tokens.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from continuum.actions import ActionLedger
from continuum.models import Run, UnknownSideEffect
from continuum.storage import SQLiteStorage


@pytest.fixture
def ledger() -> Iterator[ActionLedger]:
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    yield ActionLedger(store, "run_1")
    store.close()


def test_a_drifted_reclaim_with_tuple_arguments_is_refused(
    ledger: ActionLedger,
) -> None:
    """A tuple argument names the same resource a list does.

    Session 1 claims with ``files`` as a tuple; session 2 re-names the field and
    re-claims. Before issue #1815 the tuple yielded no tokens, so the drift
    fallback bailed out and the side effect ran again under a fresh key.
    """
    first = ledger.claim("file.process", {"files": ("invoice-101.pdf",)})

    with pytest.raises(UnknownSideEffect):
        ledger.claim("file.process", {"items": ("invoice-101.pdf",)})

    assert first.fresh


def test_tuple_and_list_arguments_open_one_attempt_not_two(
    ledger: ActionLedger,
) -> None:
    """The same resource in a tuple and in a list is one unit of work.

    A list-shaped claim already matches a tuple-shaped prior claim, so the two
    shapes must not be able to each claim the action independently.
    """
    ledger.claim("file.process", {"files": ("invoice-101.pdf",)})

    with pytest.raises(UnknownSideEffect):
        ledger.claim("file.process", {"files": ["invoice-101.pdf"]})


def test_a_different_resource_in_a_tuple_still_claims_freshly(
    ledger: ActionLedger,
) -> None:
    """Fixing the blind spot must not collapse distinct resources into one.

    The guard against false matches is unchanged -- containment of token sets
    plus location agreement -- so a different filename stays separate work.
    """
    ledger.claim("file.process", {"files": ("invoice-101.pdf",)})

    other = ledger.claim("file.process", {"files": ("invoice-202.pdf",)})
    assert other.fresh
    assert other.action.arguments == {"files": ("invoice-202.pdf",)}
