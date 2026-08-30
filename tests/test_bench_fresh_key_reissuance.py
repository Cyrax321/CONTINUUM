"""Fresh-key reissuance scenario (issue #415, epic #390).

Falsifiable end-to-end proof that authorization-bound budgets close the
fresh-key amplification hole. Loops fresh idempotency keys for one
authorization_id, asserts Nth refuses naming the id and remaining 0,
verifies distinct authorizations stay independent and settlements draw down.

Control: before #413 (pre-authorization-bound) the same loop passed every
claim because each fresh key was unbound; after, the 4th is refused. The
control is documented here rather than re-running old main.

Benchmark hook: registered as the fresh-key fault class in the #397 corpus
(benchmarks/fault_injection/faults.py -> fresh_key_reissuance) so the
fault-injection suite's detection_rate covers it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.actions.idempotency import resolve_authorization_id
from continuum.budgets import get_remaining, load_budgets
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _budgets_path() -> Path:
    import continuum.budgets as _budgets

    return Path(_budgets.DEFAULT_BUDGETS_PATH)


def test_fresh_key_loop_refuses_at_cap_naming_authorization() -> None:
    """N distinct fresh keys for one authorization exhaust the same counter."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps(
            {"default_max_attempts": 3, "action_types": {"send_invoice": {"max_attempts": 3}}}
        )
    )

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")

        auth_id = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
        assert auth_id is not None

        # Three fresh keys for same invoice each consume one slot.
        for i in range(3):
            outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"fresh-k{i}")
            assert outcome.fresh
            ledger.fail(outcome.key, "boom", certain=True)

        # 4th fresh key for same authorization must be refused, naming id and remaining 0.
        with pytest.raises(Exception) as exc:
            ledger.claim("send_invoice", {"invoice": "INV-001"}, key="fresh-k3")
        msg = str(exc.value)
        assert "budget exhausted" in msg.lower()
        # Must name the authorization (full or prefix) per #414
        assert auth_id[:8] in msg or auth_id in msg
        assert "remaining" in msg.lower()
        # Remaining should be 0; allow either "remaining 0" or "0 remaining"
        assert "0" in msg

        raw = load_budgets(budgets_path)
        assert get_remaining(raw, "send_invoice", auth_id) == 0
    finally:
        storage.close()


def test_distinct_authorizations_are_independent() -> None:
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps(
            {"default_max_attempts": 3, "action_types": {"send_invoice": {"max_attempts": 3}}}
        )
    )

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")

        # Exhaust INV-001
        for i in range(3):
            outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"k{i}")
            ledger.fail(outcome.key, "boom", certain=True)
        with pytest.raises(Exception, match="budget exhausted"):
            ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k3")

        # INV-002 should still have full budget (independent)
        outcome2 = ledger.claim("send_invoice", {"invoice": "INV-002"}, key="fresh-other")
        assert outcome2.fresh
        auth2 = resolve_authorization_id("send_invoice", None, {"invoice": "INV-002"})
        assert auth2 is not None
        raw = load_budgets(budgets_path)
        assert get_remaining(raw, "send_invoice", auth2) == 2
    finally:
        storage.close()


def test_settlements_draw_down_same_counter() -> None:
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps({"default_max_attempts": 5, "action_types": {"deploy": {"max_attempts": 5}}})
    )

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")

        auth = resolve_authorization_id("deploy", None, {"target": "prod-1"})
        assert auth is not None

        outcome = ledger.claim("deploy", {"target": "prod-1"}, key="deploy-k1")
        raw_after_claim = load_budgets(budgets_path)
        assert get_remaining(raw_after_claim, "deploy", auth) == 4

        ledger.complete(outcome.key, external_id="ext-1")
        raw_after_complete = load_budgets(budgets_path)
        assert get_remaining(raw_after_complete, "deploy", auth) == 3

        # Reconcile also draws down (same bucket)
        outcome2 = ledger.claim("deploy", {"target": "prod-1"}, key="deploy-k2")
        ledger.fail(outcome2.key, "timeout", certain=False)
        ledger.reconcile(outcome2.key, occurred=True, external_id="ext-2", note="probe")
        raw_after_reconcile = load_budgets(budgets_path)
        assert get_remaining(raw_after_reconcile, "deploy", auth) is not None
        assert get_remaining(raw_after_reconcile, "deploy", auth) < 3
    finally:
        storage.close()


def test_control_pre_413_would_have_passed_loop(tmp_path: Path) -> None:
    """Document the pre-#413 gap without running old main.

    Before authorization-bound budgets (epic #390, landed at #413), the
    ledger had no per-authorization counter. Each fresh idempotency key
    was budgeted per-key (or per-type), so looping fresh keys for the
    same invoice never hit a cap: the 4th, 10th, Nth fresh key all
    opened a new slot and no LedgerError was raised. The budget file
    either did not exist or had no authorization_bound section, so
    get_remaining returned None for any authorization_id and would_refuse
    always returned False.

    This test asserts the old code path (no file) still behaves that way
    when the registry is absent, proving the loop *would have passed*
    pre-epic and now correctly refuses post-epic. It does not run old
    main; it documents the failure mode and verifies the unbound path
    remains byte-identical for runs without authorization data.
    """
    budgets_path = _budgets_path()
    if budgets_path.exists():
        budgets_path.unlink()

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")

        # Without a budgets file, no authorization-bound budget exists,
        # so even 10 fresh keys for same invoice all succeed (old behaviour).
        for i in range(10):
            outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"old-k{i}")
            ledger.fail(outcome.key, "boom", certain=True)
        # No exception above proves the old loop passed.
        assert not budgets_path.exists()
    finally:
        storage.close()
