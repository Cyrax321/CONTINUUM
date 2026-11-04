"""Unit-level contract for the ledger-anchored authorization id (issue #1951).

``resolve_authorization_id(..., ledger=...)`` first tries ``_resolve_via_ledger``:
a unique completed (or, failing that, a unique interrupted) stored action whose
identity tokens contain, or are contained by, the incoming ones anchors the id.
``tests/test_idempotency_tuple_drift.py`` reaches this path only through
``ActionLedger.claim``; these tests drive it directly with hand-built, in-memory
ledger stand-ins so every branch is asserted without storage.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from continuum.actions.idempotency import resolve_authorization_id
from continuum.models import ActionStatus
from continuum.security.hashing import stable_hash

ACTION = "send_invoice"
INVOICE = "INV-5001"


def expected_id(*tokens: str, action_type: str = ACTION) -> str:
    """The id a given set of canonical leaf tokens must hash to."""
    return stable_hash({"type": action_type, "tokens": sorted(tokens)})


def stored(
    arguments: dict[str, Any],
    status: Any = ActionStatus.COMPLETED,
    action_type: str = ACTION,
) -> SimpleNamespace:
    return SimpleNamespace(action_type=action_type, arguments=arguments, status=status)


class ReplayLedger:
    """Exposes ``_replay()``, the first accessor the resolver tries."""

    def __init__(self, actions: dict[str, Any], run_id: str | None = None) -> None:
        self._actions = actions
        self.run_id = run_id

    def _replay(self) -> dict[str, Any]:
        return self._actions


class FoldedLedger:
    """Exposes ``folded()`` only."""

    def __init__(self, actions: dict[str, Any]) -> None:
        self._actions = actions

    def folded(self) -> dict[str, Any]:
        return self._actions


class AllLedger:
    """Exposes ``all()`` returning a list; keys come from ``action_id``."""

    def __init__(self, actions: list[Any]) -> None:
        self._actions = actions

    def all(self) -> list[Any]:
        return self._actions


class RaisingLedger:
    """The accessor exists but raises."""

    def _replay(self) -> dict[str, Any]:
        raise RuntimeError("storage unavailable")


class OddStatus:
    """A status whose comparison raises, forcing the lowercase fallback."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        raise TypeError("incomparable")

    def __hash__(self) -> int:
        return 0

    def __str__(self) -> str:
        return self.text


def resolve(ledger: Any, arguments: dict[str, Any] | None = None, **kwargs: Any) -> str | None:
    return resolve_authorization_id(
        ACTION, None, arguments or {"invoice": INVOICE}, ledger=ledger, **kwargs
    )


# --- unique-match rule ---------------------------------------------------------- #


def test_a_unique_completed_match_anchors_the_id() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE})})

    assert resolve(ledger) == expected_id(INVOICE)


def test_a_stored_superset_anchors_to_the_stored_canonical_tokens() -> None:
    """``INV-5001.pdf`` derives from ``INV-5001``, so the file name collapses."""
    ledger = ReplayLedger({"a1": stored({"file": "INV-5001.pdf"})})

    assert resolve(ledger) == expected_id(INVOICE)


def test_an_incoming_superset_anchors_to_the_stored_tokens() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE})})

    assert resolve(ledger, {"file": "INV-5001.pdf"}) == expected_id(INVOICE)


def test_two_completed_matches_are_ambiguous() -> None:
    ledger = ReplayLedger(
        {
            "a1": stored({"invoice": INVOICE}),
            "a2": stored({"document": INVOICE}),
        }
    )

    assert resolve(ledger) is None


def test_a_completed_match_wins_over_an_uncertain_one() -> None:
    ledger = ReplayLedger(
        {
            "a1": stored({"invoice": INVOICE}, ActionStatus.COMPLETED),
            "a2": stored({"document": INVOICE}, ActionStatus.STARTED),
        }
    )

    assert resolve(ledger) == expected_id(INVOICE)


@pytest.mark.parametrize("status", [ActionStatus.STARTED, ActionStatus.UNKNOWN])
def test_a_single_uncertain_match_anchors(status: ActionStatus) -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE}, status)})

    assert resolve(ledger) == expected_id(INVOICE)


def test_two_uncertain_matches_do_not_anchor() -> None:
    ledger = ReplayLedger(
        {
            "a1": stored({"invoice": INVOICE}, ActionStatus.STARTED),
            "a2": stored({"document": INVOICE}, ActionStatus.UNKNOWN),
        }
    )

    assert resolve(ledger) is None


def test_a_failed_match_is_neither_completed_nor_uncertain() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE}, ActionStatus.FAILED)})

    assert resolve(ledger) is None


def test_an_action_of_another_type_is_skipped() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE}, action_type="void_invoice")})

    assert resolve(ledger) is None


# --- containment and location --------------------------------------------------- #


def test_an_underivable_extra_token_is_not_a_match() -> None:
    """``acme-corp`` is not a file-suffix form of ``INV-5001``."""
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE})})

    assert resolve(ledger, {"invoice": INVOICE, "customer": "acme-corp"}) is None


def test_a_stored_action_with_unrelated_tokens_is_not_a_match() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": "INV-6002"})})

    assert resolve(ledger) is None


def test_the_same_file_name_in_different_directories_is_not_a_match() -> None:
    ledger = ReplayLedger({"a1": stored({"path": "/tenants/acme/ledger-2024.csv"})})

    assert resolve(ledger, {"path": "/tenants/globex/ledger-2024.csv"}) is None


def test_a_shorter_rendering_of_the_same_path_is_a_match() -> None:
    ledger = ReplayLedger({"a1": stored({"path": "/tenants/acme/ledger-2024.csv"})})

    assert resolve(ledger, {"path": "acme/ledger-2024.csv"}) == expected_id("ledger-2024")


def test_arguments_with_no_distinctive_tokens_resolve_to_nothing() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE})})

    assert resolve(ledger, {"flag": True, "n": 1}) is None


# --- volatile fields ------------------------------------------------------------ #


def test_a_declared_volatile_field_is_stripped_from_both_sides() -> None:
    """A rotating ``request_id`` must neither block the match nor enter the id."""
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE, "request_id": "req-aaaa1"})})

    anchored = resolve(
        ledger,
        {"invoice": INVOICE, "request_id": "req-bbbb2"},
        volatile=("request_id",),
    )

    assert anchored == expected_id(INVOICE)


def test_an_undeclared_volatile_field_defeats_the_match() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE, "request_id": "req-aaaa1"})})

    assert resolve(ledger, {"invoice": INVOICE, "request_id": "req-bbbb2"}) is None


def test_volatile_may_be_a_one_shot_iterator() -> None:
    """The resolver materialises ``volatile``; an iterator must not run dry (issue #1346)."""
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE, "request_id": "req-aaaa1"})})

    anchored = resolve(
        ledger,
        {"invoice": INVOICE, "request_id": "req-bbbb2"},
        volatile=iter(["request_id"]),
    )

    assert anchored == expected_id(INVOICE)


# --- run id plumbing ------------------------------------------------------------ #


def test_the_run_id_is_not_part_of_the_anchored_id() -> None:
    stored_args = {"invoice": INVOICE, "run": "run-abc123"}
    ledger = ReplayLedger({"a1": stored(stored_args)}, run_id="run-abc123")

    assert resolve(ledger) == expected_id(INVOICE)


def test_the_run_id_is_stripped_from_the_incoming_arguments_too() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE})}, run_id="run-abc123")

    anchored = resolve(ledger, {"invoice": INVOICE, "run": "run-abc123"})

    assert anchored == expected_id(INVOICE)


def test_ledgers_with_different_run_ids_anchor_to_the_same_id() -> None:
    first = ReplayLedger({"a1": stored({"invoice": INVOICE, "run": "run-abc123"})}, "run-abc123")
    second = ReplayLedger({"a1": stored({"invoice": INVOICE, "run": "run-xyz789"})}, "run-xyz789")

    assert resolve(first) == resolve(second) == expected_id(INVOICE)


def test_without_a_run_id_the_plumbing_token_blocks_the_match() -> None:
    """Control for the tests above: the strip is what makes the match possible."""
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE, "run": "run-abc123"})})

    assert resolve(ledger) is None


# --- duck-typed ledger shapes --------------------------------------------------- #


@pytest.mark.parametrize(
    "ledger",
    [
        pytest.param(ReplayLedger({"a1": stored({"invoice": INVOICE})}), id="_replay"),
        pytest.param(FoldedLedger({"a1": stored({"invoice": INVOICE})}), id="folded"),
        pytest.param(AllLedger([stored({"invoice": INVOICE})]), id="all"),
        pytest.param({"a1": stored({"invoice": INVOICE})}, id="mapping"),
    ],
)
def test_every_ledger_shape_resolves(ledger: Any) -> None:
    assert resolve(ledger) == expected_id(INVOICE)


def test_actions_may_be_plain_mappings() -> None:
    ledger = {
        "a1": {
            "action_type": ACTION,
            "arguments": {"invoice": INVOICE},
            "status": "completed",
        }
    }

    assert resolve(ledger) == expected_id(INVOICE)


def test_the_all_accessor_picks_the_matching_action_among_several() -> None:
    """Actions without an ``action_id`` are keyed by position and none is lost."""
    ledger = AllLedger(
        [
            stored({"invoice": "INV-6002"}),
            stored({"invoice": INVOICE}),
        ]
    )

    assert resolve(ledger) == expected_id(INVOICE)


def test_an_accessor_that_raises_resolves_to_none() -> None:
    assert resolve(RaisingLedger()) is None


def test_a_ledger_with_no_accessor_resolves_to_none() -> None:
    assert resolve(object()) is None


def test_no_ledger_hashes_the_incoming_tokens() -> None:
    """Control: without a ledger the id comes straight from the incoming tokens."""
    assert resolve_authorization_id(ACTION, None, {"invoice": INVOICE}) == expected_id(INVOICE)


# --- status classification ------------------------------------------------------ #


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(ActionStatus.COMPLETED, id="enum"),
        pytest.param("completed", id="string"),
        pytest.param(OddStatus("completed"), id="odd-lowercase"),
        pytest.param(OddStatus("COMPLETED"), id="odd-uppercase"),
    ],
)
def test_every_completed_form_classifies_the_same(status: Any) -> None:
    other = stored({"document": INVOICE}, ActionStatus.COMPLETED)
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE}, status), "a2": other})

    # Two completed matches are ambiguous, so None proves ``status`` counted as completed.
    assert resolve(ledger) is None


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(ActionStatus.STARTED, id="enum"),
        pytest.param("started", id="string"),
        pytest.param("unknown", id="string-unknown"),
        pytest.param(OddStatus("started"), id="odd-started"),
        pytest.param(OddStatus("UNKNOWN"), id="odd-unknown"),
    ],
)
def test_every_uncertain_form_classifies_the_same(status: Any) -> None:
    other = stored({"document": INVOICE}, ActionStatus.STARTED)
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE}, status), "a2": other})

    # Two uncertain matches are ambiguous. A lone match would anchor whether or not
    # ``status`` was classified, so None is what proves it counted as uncertain.
    assert resolve(ledger) is None


def test_a_missing_status_is_ignored() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE}, status=None)})

    assert resolve(ledger) is None


def test_a_stored_action_with_no_distinctive_tokens_is_skipped() -> None:
    ledger = ReplayLedger(
        {
            "a1": stored({"flag": True}),
            "a2": stored({"invoice": INVOICE}),
        }
    )

    assert resolve(ledger) == expected_id(INVOICE)


@pytest.mark.parametrize(
    ("value", "counts_as_completed"),
    [("completed", True), ("started", False), ("unknown", False)],
)
def test_a_status_object_is_classified_by_its_value_attribute(
    value: str, counts_as_completed: bool
) -> None:
    """An enum-like object that is not an ``ActionStatus`` is read through ``.value``."""
    partner_status = ActionStatus.COMPLETED if counts_as_completed else ActionStatus.STARTED
    other = stored({"document": INVOICE}, partner_status)
    ledger = ReplayLedger(
        {"a1": stored({"invoice": INVOICE}, SimpleNamespace(value=value)), "a2": other}
    )

    # Paired with a match of the same kind the result is ambiguous (None). Were the
    # status misclassified or dropped, the other match would anchor on its own.
    assert resolve(ledger) is None


class UnprintableRunId:
    """A truthy run id whose string form cannot be built."""

    def __str__(self) -> str:
        raise ValueError("unprintable")


def test_an_unusable_run_id_does_not_break_resolution() -> None:
    ledger = ReplayLedger({"a1": stored({"invoice": INVOICE})})
    ledger.run_id = UnprintableRunId()  # type: ignore[assignment]

    assert resolve(ledger) == expected_id(INVOICE)
