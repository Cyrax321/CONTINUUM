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

# --- Partition invariants for leaf_tokens and location_tokens ----------------- #


def test_leaf_and_location_tokens_partition_token_set() -> None:
    tokens = frozenset(
        {
            "INV-001",
            "invoice",
            "/data/invoices/INV-5.pdf",
            "invoices/INV-5.pdf",
            "4821",
            "reports\\monthly.csv",
        }
    )
    leaves = leaf_tokens(tokens)
    locations = location_tokens(tokens)

    assert leaves == frozenset({"INV-001", "invoice", "4821"})
    assert locations == frozenset(
        {
            "/data/invoices/INV-5.pdf",
            "invoices/INV-5.pdf",
            "reports\\monthly.csv",
        }
    )
    # The two subsets must be disjoint and completely cover the original set.
    assert leaves & locations == frozenset()
    assert leaves | locations == tokens


@given(st.frozensets(st.text()))
def test_leaf_and_location_tokens_always_form_disjoint_partition(
    tokens: frozenset[str],
) -> None:
    leaves = leaf_tokens(tokens)
    locations = location_tokens(tokens)

    assert leaves.isdisjoint(locations)
    assert leaves | locations == tokens
    assert all("/" not in t and "\\" not in t for t in leaves)
    assert all("/" in t or "\\" in t for t in locations)


# --- identity_tokens extraction invariants ----------------------------------- #


def test_identity_tokens_extracts_strings_and_path_derivations() -> None:
    tokens = identity_tokens({"path": "/data/invoices/INV-5.pdf"})
    assert "/data/invoices/INV-5.pdf" in tokens
    assert "INV-5.pdf" in tokens
    assert "INV-5" in tokens


def test_identity_tokens_renders_integer_scalar_as_token() -> None:
    # Issue #36: row id or account number identifies a resource.
    tokens = identity_tokens({"row_id": 4821, "account": 100200})
    assert "4821" in tokens
    assert "100200" in tokens


def test_identity_tokens_discards_boolean_values() -> None:
    # Booleans are int subclasses in Python, but True/False name no resource.
    tokens = identity_tokens({"active": True, "archived": False, "resource": "res-01"})
    assert "True" not in tokens
    assert "False" not in tokens
    assert "res-01" in tokens


def test_identity_tokens_traverses_nested_mappings_and_lists() -> None:
    args = {
        "items": [
            {"id": 101, "name": "document-alpha.pdf"},
            {"id": 102, "name": "document-beta.pdf"},
        ],
        "meta": {"tenant": "tenant-north"},
    }
    tokens = identity_tokens(args)
    assert "101" in tokens
    assert "102" in tokens
    assert "document-alpha.pdf" in tokens
    assert "document-alpha" in tokens
    assert "document-beta.pdf" in tokens
    assert "document-beta" in tokens
    assert "tenant-north" in tokens


def test_identity_tokens_extracts_external_id() -> None:
    tokens = identity_tokens(
        {"action": "sync"},
        external_id="/logs/batch/job-42.log",
    )
    assert "/logs/batch/job-42.log" in tokens
    assert "job-42.log" in tokens
    assert "job-42" in tokens


def test_identity_tokens_strips_volatile_arguments() -> None:
    tokens = identity_tokens(
        {"request_id": "req-999", "target": "dataset-prod", "retry": 3},
        volatile=["request_id", "retry"],
    )
    assert "req-999" not in tokens
    assert "3" not in tokens
    assert "dataset-prod" in tokens


def test_identity_tokens_filters_weak_and_short_tokens() -> None:
    # Tokens with length < 3, stopwords, and generic weak words are filtered out.
    tokens = identity_tokens(
        {
            "short": "ab",
            "generic": "status",
            "item": "item",
            "kept": "invoice",
        }
    )
    assert "ab" not in tokens
    assert "status" not in tokens
    assert "item" not in tokens
    assert "invoice" in tokens


def test_identity_tokens_empty_inputs() -> None:
    assert identity_tokens(None) == frozenset()
    assert identity_tokens({}) == frozenset()


# --- same_location invariants and drift regression cases ---------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Docstring drift case: invoices/INV-5.pdf inside /data/invoices/INV-5.pdf
        ("/data/invoices/INV-5.pdf", "invoices/INV-5.pdf"),
        ("/data/invoices/INV-5.pdf", "./invoices/INV-5.pdf"),
        ("/data/invoices/INV-5.pdf", "/data/invoices/INV-5.pdf"),
        # Cross-platform separator rendering
        ("data\\invoices\\INV-5.pdf", "invoices/INV-5.pdf"),
        ("data/invoices/INV-5.pdf", "data\\invoices\\INV-5.pdf"),
        # Relative prefix with dot segments
        ("a/b/c/report.csv", "./b/c/report.csv"),
    ],
)
def test_same_location_accepts_valid_path_drift(left: str, right: str) -> None:
    assert same_location(left, right) is True
    assert same_location(right, left) is True, "same_location must be symmetric"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Issue #365: same basename in different containers must not collapse
        ("/tenants/acme/report.csv", "/tenants/globex/report.csv"),
        ("a/report.csv", "b/report.csv"),
        ("/var/data/output.json", "/opt/data/output.json"),
        # Basename shared with no directory in common
        ("dir1/file.txt", "dir2/file.txt"),
        ("x/y/z.dat", "a/b/z.dat"),
    ],
)
def test_same_location_rejects_distinct_containers_issue_365(left: str, right: str) -> None:
    assert same_location(left, right) is False
    assert same_location(right, left) is False, "same_location must be symmetric"


def test_same_location_degenerate_and_empty_paths() -> None:
    assert same_location("", "") is False
    assert same_location("", "invoices/INV-5.pdf") is False
    assert same_location("invoices/INV-5.pdf", "") is False
    assert same_location(".", ".") is False
    assert same_location("///", "///") is False


_SEGMENT_STRATEGY = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_-"),
    min_size=1,
    max_size=16,
)


@given(
    prefix=st.lists(_SEGMENT_STRATEGY, min_size=1, max_size=4),
    suffix=st.lists(_SEGMENT_STRATEGY, min_size=1, max_size=4),
)
def test_same_location_suffix_drift_property(prefix: list[str], suffix: list[str]) -> None:
    full_path = "/".join(prefix + suffix)
    suffix_path = "/".join(suffix)
    assert same_location(full_path, suffix_path) is True
    assert same_location(suffix_path, full_path) is True


@given(
    segments_a=st.lists(_SEGMENT_STRATEGY, min_size=1, max_size=4),
    segments_b=st.lists(_SEGMENT_STRATEGY, min_size=1, max_size=4),
)
def test_same_location_is_symmetric(segments_a: list[str], segments_b: list[str]) -> None:
    path_a = "/".join(segments_a)
    path_b = "/".join(segments_b)
    assert same_location(path_a, path_b) == same_location(path_b, path_a)


# --- locations_agree invariants ---------------------------------------------- #


def test_locations_agree_when_one_or_both_sides_have_no_location_tokens() -> None:
    # A side carrying no path-like token makes no location claim and cannot contradict one.
    has_location = frozenset({"/data/invoices/INV-5.pdf"})
    empty_location = frozenset()

    assert locations_agree(empty_location, has_location) is True
    assert locations_agree(has_location, empty_location) is True
    assert locations_agree(empty_location, empty_location) is True


def test_locations_agree_matches_on_reconcilable_pair() -> None:
    left = frozenset({"/data/invoices/INV-5.pdf", "/var/backup/log.txt"})
    right = frozenset({"invoices/INV-5.pdf"})
    assert locations_agree(left, right) is True
    assert locations_agree(right, left) is True


def test_locations_agree_refuses_when_no_pair_reconciles_issue_365() -> None:
    left = frozenset({"/tenants/acme/report.csv"})
    right = frozenset({"/tenants/globex/report.csv"})
    assert locations_agree(left, right) is False
    assert locations_agree(right, left) is False


def test_locations_agree_multiple_paths_distinct_containers() -> None:
    left = frozenset({"tenant_a/report.csv", "tenant_a/summary.txt"})
    right = frozenset({"tenant_b/report.csv", "tenant_b/summary.txt"})
    assert locations_agree(left, right) is False
    assert locations_agree(right, left) is False
