"""Isolate the authorization-bound budget registry per test.

The ledger's drawdown logic (issue #413) persists counters in
``.continuum/budgets.json`` via ``DEFAULT_BUDGETS_PATH``. Without isolation,
in-memory tests that claim with the same resource tokens (e.g. invoice
INV-001) share the global file and exhaust the fallback cap of 3 across
tests, making the suite order-dependent and causing spurious
``LedgerError: budget exhausted`` failures. Each test gets a fresh
temporary registry so budgets are per-test, not per-workspace.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_budget_registry(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    fake = tmp_path / ".continuum" / "budgets.json"
    # Both modules import the constant at load time; patch both to an
    # absolute per-test path that matches the layout tests create via
    # monkeypatch.chdir(tmp_path) + Path(".continuum/budgets.json").
    # Also export via env so subprocess workers (test_reconciliation) inherit
    # the same per-test file instead of sharing the repo-root global.
    monkeypatch.setattr("continuum.budgets.DEFAULT_BUDGETS_PATH", str(fake))
    monkeypatch.setattr("continuum.actions.ledger.DEFAULT_BUDGETS_PATH", str(fake))
    monkeypatch.setenv("CONTINUUM_BUDGETS_PATH", str(fake))
    yield
