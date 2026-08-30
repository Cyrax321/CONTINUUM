"""Authorization identity (issue #412): stable bucket for budgets.

Reuses the identity-token machinery of idempotency.py; no second scheme.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from continuum.actions import ActionLedger
from continuum.actions.idempotency import resolve_authorization_id
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_explicit_key_is_stable_and_ignores_arguments() -> None:
    """Explicit key wins: same key -> same id even if arguments differ."""
    a = resolve_authorization_id("send_invoice", "invoice:INV-001", {"invoice": "INV-001"})
    b = resolve_authorization_id(
        "send_invoice", "invoice:INV-001", {"invoice": "INV-002", "amount": 999}
    )
    assert a is not None
    assert a == b

    c = resolve_authorization_id("send_invoice", "invoice:INV-002", {"invoice": "INV-001"})
    assert c is not None
    assert a != c


def test_explicit_key_different_types_are_different_operations() -> None:
    """Action-type drift is NOT bridged by design."""
    a = resolve_authorization_id("send_invoice", "k-1", {"x": 1})
    b = resolve_authorization_id("send-invoice-email", "k-1", {"x": 1})
    assert a != b


def test_renamed_argument_fields_map_to_same_token_identity() -> None:
    """Field name drift must not create a new budget bucket."""
    a = resolve_authorization_id("send_invoice", None, {"invoice_id": "INV-001"})
    b = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    assert a is not None and b is not None
    assert a == b

    # Richer shape with path still collapses to same base after canonicalization
    c = resolve_authorization_id(
        "send_invoice",
        None,
        {"invoice_id": "INV-001", "target": "/tmp/e2e-outbox/INV-001.sent"},
    )
    assert c == a


def test_relative_vs_absolute_path_is_same_identity() -> None:
    """Same file rendered two ways is one authorization."""
    a = resolve_authorization_id(
        "bench.send", None, {"file": "/data/invoices/INV-5.pdf", "invoice": "INV-5"}
    )
    b = resolve_authorization_id(
        "bench.send", None, {"file": "invoices/INV-5.pdf", "invoice": "INV-5"}
    )
    assert a is not None and b is not None
    assert a == b


def test_numeric_resource_ids_are_distinctive() -> None:
    """Row ids count as identity (issue #36)."""
    a = resolve_authorization_id("db.update", None, {"row_id": 4821})
    b = resolve_authorization_id("db.update", None, {"id": 4821})
    assert a is not None and b is not None
    assert a == b

    c = resolve_authorization_id("db.update", None, {"row_id": 9999})
    assert c != a


def test_plain_word_resource_ids_are_distinctive() -> None:
    """Plain words name a resource as well as INV-001 (issue #33)."""
    a = resolve_authorization_id("publish", None, {"topic": "invoice"})
    b = resolve_authorization_id("publish", None, {"subject": "invoice"})
    assert a is not None and b is not None
    assert a == b

    c = resolve_authorization_id("publish", None, {"topic": "dataset"})
    assert c != a


def test_unbound_when_no_distinctive_token() -> None:
    """Weak or absent tokens mean unbound, today's behaviour byte-identical."""
    # Only weak tokens (status words, short ids)
    assert resolve_authorization_id("api.call", None, {"status": "sent", "id": 1}) is None
    assert resolve_authorization_id("api.call", None, {}) is None
    assert resolve_authorization_id("api.call", None, None) is None
    # Whitespace or stopword-only
    assert resolve_authorization_id("publish", None, {"topic": "the"}) is None


def test_determinism_across_calls() -> None:
    """Identical inputs produce identical ids on any machine."""
    first = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    second = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    assert first == second
    # Also with explicit key
    k1 = resolve_authorization_id("send_invoice", "k-1", {"x": 1})
    k2 = resolve_authorization_id("send_invoice", "k-1", {"x": 1})
    assert k1 == k2


def test_determinism_across_processes() -> None:
    """Spawn a fresh interpreter and compare hashes."""
    from pathlib import Path

    # Run in a fresh process with cwd set to the worktree so the import
    # resolves to this checkout, not to a sibling worktree that may be mid-
    # migration (e.g. ConstraintPin). Use PYTHONPATH to prefer this src.
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**dict(__import__("os").environ), "PYTHONPATH": src}
    code = (
        "from continuum.actions.idempotency import resolve_authorization_id;"
        "print(resolve_authorization_id('send_invoice', None, {'invoice':'INV-001'}))"
    )
    out = subprocess.check_output([sys.executable, "-c", code], text=True, env=env).strip()
    local = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    assert out == local


def test_action_type_not_bridged_for_token_fallback() -> None:
    """Same tokens under different types must be different buckets."""
    a = resolve_authorization_id("send_invoice", None, {"invoice": "INV-005"})
    b = resolve_authorization_id("send-invoice-email", None, {"invoice": "INV-005"})
    assert a is not None and b is not None
    assert a != b


def test_ledger_anchored_identity_uses_unique_match() -> None:
    """When a ledger is supplied, only a unique completed/interrupted anchors."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    ledger = ActionLedger(storage, "run_1")

    first = ledger.claim(
        "send_invoice",
        {"invoice_id": "INV-001", "target": "/tmp/e2e-outbox/INV-001.sent"},
    )
    ledger.complete(first.key, external_id="INV-001.sent")

    # Drifted re-claim (field rename, no path) should anchor to the prior
    anchored = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"}, ledger=ledger)
    pure = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    assert anchored is not None
    assert anchored == pure

    # Different invoice with no match -> unbound when ledger is non-empty
    assert (
        resolve_authorization_id("send_invoice", None, {"invoice": "INV-004"}, ledger=ledger)
        is None
    )


def test_ledger_ambiguous_returns_none() -> None:
    """Multiple completed actions sharing tokens is ambiguous, return None."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_2", goal="g"))
    ledger = ActionLedger(storage, "run_2")
    # Force two distinct completed actions with same tokens via explicit keys
    a1 = ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k1")
    ledger.complete(a1.key, external_id="a1")
    a2 = ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k2")
    ledger.complete(a2.key, external_id="a2")

    assert (
        resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"}, ledger=ledger)
        is None
    )


def test_explicit_key_precedence_over_token_match() -> None:
    """Key > token-match: key present means token identity is ignored."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_3", goal="g"))
    ledger = ActionLedger(storage, "run_3")
    first = ledger.claim("send_invoice", {"invoice": "INV-001"})
    ledger.complete(first.key, external_id="INV-001")

    # Even though a token match exists, explicit key determines the id
    via_key = resolve_authorization_id(
        "send_invoice", "my-stable-key", {"invoice": "INV-001"}, ledger=ledger
    )
    via_key2 = resolve_authorization_id(
        "send_invoice", "my-stable-key", {"different": "args"}, ledger=ledger
    )
    assert via_key is not None
    assert via_key == via_key2


def test_empty_action_type_and_empty_key_are_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        resolve_authorization_id("", "k", {})
    with pytest.raises(ValueError, match="non-empty"):
        resolve_authorization_id("   ", None, {})
    with pytest.raises(ValueError, match="non-empty"):
        resolve_authorization_id("send_invoice", "", {})


def test_volatile_fields_excluded_from_token_identity() -> None:
    """Volatile fields must not affect the authorization bucket."""
    a = resolve_authorization_id(
        "call", None, {"payload": "INV-001", "attempt": 1}, volatile=["attempt"]
    )
    b = resolve_authorization_id(
        "call", None, {"payload": "INV-001", "attempt": 2}, volatile=["attempt"]
    )
    assert a is not None and b is not None
    assert a == b


def test_file_extension_still_collapses_but_dotted_name_stays_distinct() -> None:
    """INV-001.sent is derived from INV-001, alice.smith is not from alice."""
    a = resolve_authorization_id("export.report", None, {"dataset": "report"})
    b = resolve_authorization_id("export.report", None, {"dataset": "report.csv"})
    assert a == b

    c = resolve_authorization_id("email.send", None, {"to": "alice"})
    d = resolve_authorization_id("email.send", None, {"to": "alice.smith"})
    assert c != d
