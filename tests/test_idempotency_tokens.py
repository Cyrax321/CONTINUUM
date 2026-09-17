"""Direct tests for the identity-token helpers (issue #1233).

The five pure functions in ``continuum.actions.idempotency`` carry the ledger's
defensive fallback: the path that recognises an already-recorded action even
when the two attempts describe the same resource differently (a renamed
argument field, a path re-rendered between sessions). ``tests/test_action_ledger.py``
exercises that fallback end to end, so the helpers are covered indirectly, but
the invariants their docstrings state were asserted only in prose:

- ``leaf_tokens`` and ``location_tokens`` partition the token set between them,
- the container a leaf is reached through is set aside rather than discarded,
  so two files that share a name in different directories do not collapse,
- a side carrying no path-like token makes no location claim and cannot
  contradict one,
- ``same_location`` compares suffixes, not spellings, and is no machine's
  filesystem check.

``#365`` is the reason ``location_tokens`` exists. Leaf comparison alone matched
``/tenants/acme/report.csv`` against ``/tenants/globex/report.csv`` -- the two
share every leaf -- and globex was never notified. Removing either half of the
pair, or letting the two predicates drift apart, is what these tests catch.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from continuum.actions.idempotency import (
    identity_tokens,
    leaf_tokens,
    location_tokens,
    locations_agree,
    same_location,
)

# A portable path vocabulary: ascii segments, no separators inside a segment, so
# the generated paths mean the same thing on posix and Windows and never depend
# on the machine running the suite (issue #842).
_SEGMENT = st.text(
    alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyz0123456789_.-")),
    min_size=1,
    max_size=6,
).filter(lambda s: s not in (".", ".."))  # normpath collapses these to nothing
_PATH = st.lists(_SEGMENT, min_size=1, max_size=4).map(lambda parts: "/".join(parts))
_WORD = st.text(
    alphabet=st.sampled_from(
        list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    ),
    min_size=1,
    max_size=10,
)
_ARGUMENT_VALUE = st.one_of(_PATH, _WORD, st.integers(min_value=1, max_value=10**6))


def test_leaf_and_location_tokens_partition_the_set() -> None:
    """Between them the two halves keep every token, and keep none in common."""
    tokens = identity_tokens({"invoice": "INV-001", "outbox": "/data/out/INV-001.pdf", "count": 12})
    leaves, locations = leaf_tokens(tokens), location_tokens(tokens)
    assert leaves | locations == tokens
    assert not (leaves & locations)


@given(values=st.lists(_ARGUMENT_VALUE, max_size=6))
def test_partition_holds_for_generated_arguments(values: list) -> None:
    """The partition is not an accident of one hand-picked argument set.

    ``leaf_tokens`` and ``location_tokens`` are two halves of one predicate: if
    either side learned a filter the other did not, a token would be silently
    dropped from the identity comparison or counted twice.
    """
    tokens = identity_tokens({"items": values})
    leaves, locations = leaf_tokens(tokens), location_tokens(tokens)
    assert leaves | locations == tokens
    assert not (leaves & locations)


def test_leaf_tokens_reach_a_path_through_its_basename() -> None:
    """``/data/invoices/INV-5.pdf`` identifies the file, not the directory tree."""
    tokens = identity_tokens({"path": "/data/invoices/INV-5.pdf"})
    assert leaf_tokens(tokens) == frozenset({"INV-5", "INV-5.pdf"})
    # The container survives, set aside rather than discarded.
    assert location_tokens(tokens) == frozenset({"/data/invoices/INV-5.pdf"})


def test_path_drift_agrees_on_location() -> None:
    """One resource rendered twice: the sparser path is a trailing part of it.

    This is the drift case leaf comparison exists to serve. The two spellings
    disagree on the container, so the leaves are what match them on, and the
    locations have to reconcile for the match to stand.
    """
    long_args = identity_tokens({"path": "/data/invoices/INV-5.pdf"})
    short_args = identity_tokens({"path": "invoices/INV-5.pdf"})
    assert leaf_tokens(long_args) == leaf_tokens(short_args) == frozenset({"INV-5", "INV-5.pdf"})
    assert locations_agree(location_tokens(long_args), location_tokens(short_args))


def test_same_named_files_in_different_directories_disagree() -> None:
    """The #365 regression: shared leaves, different resources.

    ``/tenants/acme/report.csv`` and ``/tenants/globex/report.csv`` agree on
    every leaf, which is why the locations are the only thing that can tell them
    apart. Accepting this pair is what silently swallowed the second side
    effect; the ledger requires ``locations_agree`` for exactly this reason.
    """
    acme = identity_tokens({"outbox_file": "/tenants/acme/report.csv"})
    globex = identity_tokens({"outbox_file": "/tenants/globex/report.csv"})
    assert leaf_tokens(acme) == leaf_tokens(globex) == frozenset({"report", "report.csv"})
    assert not locations_agree(location_tokens(acme), location_tokens(globex))


def test_basename_shared_with_no_directory_in_common_is_a_different_file() -> None:
    """Agreeing on the last segment is not agreeing on the file."""
    assert not same_location("a/b/report.csv", "x/y/report.csv")
    assert same_location("/a/b/report.csv", "b/report.csv")


def test_same_location_compares_suffixes_not_spellings() -> None:
    assert same_location("report.csv", "report.csv")
    # Drift shortens or lengthens a path; it does not reorder it.
    assert same_location("/data/invoices/INV-5.pdf", "invoices/INV-5.pdf")


def test_same_location_is_separator_agnostic() -> None:
    """The token was written by whatever machine recorded the action."""
    assert same_location("a/b/report.csv", "a\\b\\report.csv")


def test_same_location_rejects_an_empty_path() -> None:
    assert not same_location("/data/report.csv", "")


@given(path=_PATH)
def test_same_location_is_reflexive(path: str) -> None:
    assert same_location(path, path)


@given(a=_PATH, b=_PATH)
def test_same_location_is_symmetric(a: str, b: str) -> None:
    """Which side of the comparison a token set lands on must not matter.

    The implementation picks a shorter and a longer path internally, so an
    asymmetry here would mean two attempts match in one order and not the
    other.
    """
    assert same_location(a, b) == same_location(b, a)


@given(a=_PATH, b=_PATH)
def test_locations_agree_is_symmetric(a: str, b: str) -> None:
    """The stored side and the incoming side can swap without changing the answer."""
    left, right = (
        location_tokens(identity_tokens({"p": a})),
        location_tokens(identity_tokens({"p": b})),
    )
    assert locations_agree(left, right) == locations_agree(right, left)


def test_a_side_with_no_location_never_contradicts() -> None:
    """An argument rename that drops the path entirely still matches on its leaves.

    A token set with no path-like member is making no claim about where the
    resource is, so it cannot disagree with one that is.
    """
    with_path = location_tokens(identity_tokens({"path": "/data/invoices/INV-5.pdf"}))
    assert locations_agree(with_path, frozenset())
    assert locations_agree(frozenset(), with_path)
    assert locations_agree(frozenset(), frozenset())


def test_integer_argument_renders_as_a_token() -> None:
    """A row id of 4821 identifies a row as well as ``INV-001`` identifies an invoice (#36)."""
    assert identity_tokens({"invoice_id": 4821}) == frozenset({"4821"})


def test_bool_is_not_a_token_but_an_int_is() -> None:
    """``True`` names no resource, and ``bool`` is an ``int`` subclass."""
    assert identity_tokens({"flag": True, "count": 4821}) == frozenset({"4821"})


def test_weak_and_short_tokens_are_dropped() -> None:
    """Only what cannot distinguish one resource from another is discarded."""
    assert identity_tokens({"a": "the"}) == frozenset()  # stopword
    assert identity_tokens({"a": "ab"}) == frozenset()  # shorter than three characters
    # A plain word still names a resource (#33).
    assert identity_tokens({"a": "invoice"}) == frozenset({"invoice"})


def test_stem_and_full_name_both_survive() -> None:
    """``INV-001.sent`` and ``INV-001`` are one resource, the derivation pair the
    ledger's superset rule depends on."""
    assert identity_tokens({"a": "INV-001.sent"}) == frozenset({"INV-001", "INV-001.sent"})


def test_nested_mappings_and_lists_are_collected() -> None:
    """Arguments arrive in arbitrary shapes; the collector walks them all."""
    tokens = identity_tokens({"outer": {"inner": "/p/q.txt", "n": 5}, "items": ["L1", "/m/n.txt"]})
    assert tokens == frozenset({"/p/q.txt", "q.txt", "/m/n.txt", "n.txt"})


def test_volatile_arguments_are_excluded() -> None:
    """A retry counter differs on every call without changing the work."""
    assert identity_tokens({"invoice": "INV-9", "attempt": 342}, volatile=["attempt"]) == (
        frozenset({"INV-9"})
    )
    assert identity_tokens({"invoice": "INV-9", "attempt": 342}) == frozenset({"INV-9", "342"})


def test_external_id_is_collected() -> None:
    """An outcome's external id is as much a resource identity as an argument."""
    assert "EXT-42" in identity_tokens({"invoice": "INV-9"}, external_id="EXT-42")


def test_no_arguments_yield_no_tokens() -> None:
    assert identity_tokens(None) == frozenset()
    assert identity_tokens({}) == frozenset()


def test_helpers_are_exported() -> None:
    """The fallback's building blocks are public API, not internals."""
    from continuum.actions import idempotency

    for name in (
        "identity_tokens",
        "leaf_tokens",
        "location_tokens",
        "locations_agree",
        "same_location",
    ):
        assert name in idempotency.__all__
        assert hasattr(idempotency, name)


@pytest.mark.parametrize("value", [4821, "INV-001", "/data/INV-001.pdf"])
def test_scalar_argument_survives_as_a_token(value: object) -> None:
    """A scalar value is a token whatever type it arrives as."""
    assert identity_tokens({"v": value}) != frozenset()
