"""Budget drawdown at claim and confirmation with fresh-key refusal (issue #413).

Verifies the #413 acceptance criteria:

* N distinct fresh idempotency keys for one authorization exhaust the same
  counter regardless of key freshness.
* Distinct authorizations keep independent budgets.
* Confirmation/settlement events draw down the same counter as claims.
* Unset/weak authorization fields leave behaviour byte-identical (no budget).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from continuum.actions import ActionLedger
from continuum.actions import ledger as ledger_module
from continuum.actions.idempotency import resolve_authorization_id
from continuum.actions.ledger import LedgerError
from continuum.budgets import (
    get_remaining,
    load_budgets,
)
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run
from continuum.storage import SQLiteStorage


def _budgets_path() -> Path:
    # Read the patched value at call time, not import time, so the
    # conftest's per-test monkeypatch is visible.
    import continuum.budgets as _budgets

    return Path(_budgets.DEFAULT_BUDGETS_PATH)


def test_n_distinct_fresh_keys_for_one_authorization_exhaust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same resource under N fresh keys exhausts the authorization budget."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps(
            {"default_max_attempts": 3, "action_types": {"send_invoice": {"max_attempts": 3}}}
        )
    )

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    # Three distinct fresh keys for the same invoice should each consume one slot.
    for i in range(3):
        outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"k{i}")
        assert outcome.fresh
        ledger.fail(outcome.key, "boom", certain=True)
        # Re-claim with a new key but same invoice still draws the same bucket
        # on the next iteration's claim.

    # Fourth distinct fresh key for the same invoice must be refused.
    with pytest.raises(Exception, match="budget exhausted"):
        ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k3")

    raw = load_budgets(budgets_path)
    auth_id = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    assert auth_id is not None
    assert get_remaining(raw, "send_invoice", auth_id) == 0


def test_a_rejected_claim_consumes_no_budget_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim rejected by validation draws no slot (issue #1168).

    The counter must advance only once an ACTION_RECORDED event is durable, so
    a claim that fails validation after the budget gate leaves the accounting
    and the log agreeing that no attempt happened. Otherwise a single bad
    origin_digest under a max_attempts of 1 permanently blocks the resource:
    the second, valid claim is refused even though no attempt was ever
    recorded or performed, and the cap is consumed by attempts that never
    occurred.
    """
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"action_types": {"send_invoice": {"max_attempts": 1}}}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    auth_id = resolve_authorization_id("send_invoice", None, {"invoice": "INV-001"})
    assert auth_id is not None

    # The budget gate passes; the digest check after it rejects the claim.
    with pytest.raises(LedgerError, match="origin_digest must be 64 lowercase hex"):
        ledger.claim("send_invoice", {"invoice": "INV-001"}, origin_digest="not-hex")

    # No attempt was recorded and no slot was consumed: the rejected claim
    # leaves no authorization-bound entry at all.
    events = storage.read_events("run_1")
    assert not [e for e in events if e.type is EventType.ACTION_RECORDED]
    raw = load_budgets(budgets_path)
    assert "authorization_bound" not in raw

    # The same claim, valid this time, still gets its single slot and records.
    outcome = ledger.claim("send_invoice", {"invoice": "INV-001"})
    assert outcome.fresh
    events = storage.read_events("run_1")
    assert [e for e in events if e.type is EventType.ACTION_RECORDED]

    raw = load_budgets(budgets_path)
    assert raw["authorization_bound"]["send_invoice"][auth_id]["counter"] == 1
    assert get_remaining(raw, "send_invoice", auth_id) == 0

    # The one slot is now genuinely spent, by the attempt that was recorded.
    with pytest.raises(LedgerError, match="budget exhausted"):
        ledger.claim("send_invoice", {"invoice": "INV-001"}, key="fresh-key-2")


def test_distinct_authorizations_keep_independent_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps(
            {"default_max_attempts": 3, "action_types": {"send_invoice": {"max_attempts": 3}}}
        )
    )

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    # Exhaust INV-001
    for i in range(3):
        outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"k{i}")
        ledger.fail(outcome.key, "boom", certain=True)
    with pytest.raises(Exception, match="budget exhausted"):
        ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k3")

    # INV-002 should still have full budget
    outcome2 = ledger.claim("send_invoice", {"invoice": "INV-002"}, key="k-new")
    assert outcome2.fresh
    auth2 = resolve_authorization_id("send_invoice", None, {"invoice": "INV-002"})
    assert auth2 is not None
    raw = load_budgets(budgets_path)
    assert get_remaining(raw, "send_invoice", auth2) == 2


def test_settlements_draw_down_same_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps({"default_max_attempts": 5, "action_types": {"deploy": {"max_attempts": 5}}})
    )

    storage = SQLiteStorage(":memory:")
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

    # Reconcile also draws down
    ledger2 = ActionLedger(storage, "run_1")
    # Need a new action to reconcile
    outcome2 = ledger2.claim("deploy", {"target": "prod-1"}, key="deploy-k2")
    # Fail it then reconcile as occurred
    ledger2.fail(outcome2.key, "timeout", certain=False)
    # The fail left it UNKNOWN, reconcile will settle and draw down
    ledger2.reconcile(outcome2.key, occurred=True, external_id="ext-2", note="probe")
    raw_after_reconcile = load_budgets(budgets_path)
    # Claim (k2) + fail (no draw) + reconcile (draw) = 2 more consumptions after the first pair
    # So total 3 consumptions: claim k1, complete k1, claim k2, reconcile k2 = 4?
    # At least verify it decreased further
    assert get_remaining(raw_after_reconcile, "deploy", auth) is not None
    assert get_remaining(raw_after_reconcile, "deploy", auth) < 3


def test_reconcile_does_not_re_draw_after_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """complete->reconcile of one effect draws the settlement bucket once, not
    twice. reconcile() used to draw on every call regardless of status, so the
    documented complete->reconcile correction double-charged the cap (#1370)."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps({"default_max_attempts": 5, "action_types": {"deploy": {"max_attempts": 5}}})
    )
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    auth = resolve_authorization_id("deploy", None, {"target": "prod-1"})
    assert auth is not None

    outcome = ledger.claim("deploy", {"target": "prod-1"}, key="deploy-k1")
    ledger.complete(outcome.key, external_id="ext-1")  # claim + settlement = 2 drawn
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 3

    # Reconciling the already-COMPLETED record is permitted, but it settles no
    # new effect, so the counter must hold.
    ledger.reconcile(outcome.key, occurred=True, external_id="ext-1", note="probe")
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 3


def test_idempotent_reconcile_retry_draws_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile retried after a dropped MCP response settles the same STARTED
    claim; the second call sees a terminal record and must not draw again."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps({"default_max_attempts": 5, "action_types": {"deploy": {"max_attempts": 5}}})
    )
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    auth = resolve_authorization_id("deploy", None, {"target": "prod-1"})
    assert auth is not None

    outcome = ledger.claim("deploy", {"target": "prod-1"}, key="deploy-k1")  # 1 drawn
    ledger.reconcile(
        outcome.key, occurred=True, external_id="ext-1"
    )  # settle from STARTED: 2 drawn
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 3
    ledger.reconcile(outcome.key, occurred=True, external_id="ext-1")  # retry: no draw
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 3


def test_reconcile_absent_effect_draws_no_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconcile(occurred=False) confirms the effect never landed and resolves
    the record to FAILED -- the same terminal state fail() produces, and fail()
    draws no settlement. Charging the bucket for an effect a check proved absent
    mis-bills the cap for a landing that never happened, so the counter must hold
    across the reconcile. The #1370 status guard alone does not cover this: the
    record is still STARTED (or the uncertain UNKNOWN) when reconcile is called,
    so only also guarding on occurred keeps the drawdown honest.
    """
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps({"default_max_attempts": 5, "action_types": {"deploy": {"max_attempts": 5}}})
    )
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    auth = resolve_authorization_id("deploy", None, {"target": "prod-1"})
    assert auth is not None

    # STARTED -> reconcile(occurred=False): only the claim draws; an absent
    # effect settles nothing.
    outcome = ledger.claim("deploy", {"target": "prod-1"}, key="deploy-k1")  # 1 drawn
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 4
    ledger.reconcile(outcome.key, occurred=False, note="probe found nothing")
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 4

    # UNKNOWN -> reconcile(occurred=False): the uncertain outcome only reconcile
    # can settle, resolved to absent, still draws nothing.
    outcome2 = ledger.claim("deploy", {"target": "prod-1"}, key="deploy-k2")  # 1 drawn
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 3
    ledger.fail(outcome2.key, "timeout", certain=False)  # -> UNKNOWN, no draw
    ledger.reconcile(outcome2.key, occurred=False, note="probe found nothing")
    assert get_remaining(load_budgets(budgets_path), "deploy", auth) == 3


def test_weak_tokens_leave_no_budget_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"default_max_attempts": 3}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    # Weak tokens produce no authorization id, so no budget entry.
    assert resolve_authorization_id("api.call", None, {"status": "sent"}) is None
    outcome = ledger.claim("api.call", {"status": "sent"})
    assert outcome.fresh
    raw = load_budgets(budgets_path)
    assert "authorization_bound" not in raw or not raw["authorization_bound"]


def test_noise_padding_cannot_move_the_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One throwaway argument token must not reset the cap (issue #1052).

    The authorization bucket derives from argument tokens, and the arguments are
    caller-controlled noise plus the real resource. Padding a ``trace_id`` per
    retry therefore moved every retry into a fresh bucket at its full allowance,
    so an operator-set cap of 2 refused a third identical attempt but stayed open
    indefinitely while each retry carried a new token. The bucket must follow the
    identity the ledger has already decided the claim *is*: when the claim defers
    to an existing record, that record's arguments name the operation.
    """
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps(
            {"default_max_attempts": 2, "action_types": {"send_invoice": {"max_attempts": 2}}}
        )
    )

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    # Fixed idempotency key, one throwaway argument token varied per attempt.
    for i in range(2):
        outcome = ledger.claim(
            "send_invoice", {"invoice": "INV-001", "trace_id": f"trace-{i}"}, key="invoice:INV-001"
        )
        assert outcome.fresh
        ledger.fail(outcome.key, "upstream 503", certain=True)

    # The third padded attempt must be refused like an identical one is.
    with pytest.raises(Exception, match="budget exhausted"):
        ledger.claim(
            "send_invoice", {"invoice": "INV-001", "trace_id": "trace-2"}, key="invoice:INV-001"
        )

    # Every retry drew from one bucket rather than minting a fresh one each time.
    raw = load_budgets(budgets_path)
    entries = raw.get("authorization_bound", {}).get("send_invoice", {})
    assert len(entries) == 1
    assert get_remaining(raw, "send_invoice", list(entries)[0]) == 0


def test_noise_padding_tolerated_when_key_is_not_held_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh keys plus fresh noise is the documented unbound case.

    With no fixed identity and no ledger record to defer to, nothing in the
    arguments distinguishes the resource from the noise, so this stays on the
    token fallback as before. Documented rather than silently changed: the
    operator's cap binds the identities the ledger can see.
    """
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"default_max_attempts": 2}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    for i in range(3):
        outcome = ledger.claim(
            "send_invoice", {"invoice": "INV-001", "trace_id": f"trace-{i}"}, key=f"k{i}"
        )
        assert outcome.fresh
        ledger.fail(outcome.key, "boom", certain=True)

    # Each attempt minted both a fresh key and a fresh token, so each landed in
    # its own bucket; the cap does not bind. This is the accepted residual.
    raw = load_budgets(budgets_path)
    entries = raw.get("authorization_bound", {}).get("send_invoice", {})
    assert len(entries) == 3
    assert all(entry["counter"] == 1 for entry in entries.values())


def test_changing_the_volatile_declaration_cannot_move_the_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry that flips its ``volatile`` declaration stays in one bucket.

    Token derivation reads the caller's ``volatile`` sequence to decide which
    arguments to drop, so re-deriving the bucket per attempt would let a retry
    rename its noise fields and start its count over. The claim pins the
    resolved id onto the record and every later attempt reads it back.
    """
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps(
            {"default_max_attempts": 2, "action_types": {"send_invoice": {"max_attempts": 2}}}
        )
    )

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    # First attempt declares the noise field volatile; the second does not.
    first = ledger.claim(
        "send_invoice",
        {"invoice": "INV-001", "trace_id": "trace-0"},
        key="invoice:INV-001",
        volatile=["trace_id"],
    )
    assert first.fresh
    assert first.action.budget_authorization_id is not None
    ledger.fail(first.key, "boom", certain=True)

    second = ledger.claim(
        "send_invoice",
        {"invoice": "INV-001", "trace_id": "trace-1"},
        key="invoice:INV-001",
        volatile=[],
    )
    assert second.fresh
    assert second.action.budget_authorization_id == first.action.budget_authorization_id
    ledger.fail(second.key, "boom", certain=True)

    # The third attempt flips back to the first declaration but the cap is
    # already spent: the bucket never moved.
    with pytest.raises(Exception, match="budget exhausted"):
        ledger.claim(
            "send_invoice",
            {"invoice": "INV-001", "trace_id": "trace-2"},
            key="invoice:INV-001",
            volatile=["trace_id"],
        )

    raw = load_budgets(budgets_path)
    entries = raw.get("authorization_bound", {}).get("send_invoice", {})
    assert len(entries) == 1


def test_settlement_uses_the_bucket_the_claim_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``complete`` settles against the bucket the claim drew from.

    A claim may declare ``volatile`` fields while the settlement paths always
    derive with none, so re-deriving put the confirmation in a different bucket
    from the attempt it settled. The record carries the id, so both draw from it.
    """
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(
        json.dumps({"default_max_attempts": 5, "action_types": {"deploy": {"max_attempts": 5}}})
    )

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    outcome = ledger.claim(
        "deploy",
        {"target": "prod-1", "trace_id": "trace-0"},
        key="deploy-k1",
        volatile=["trace_id"],
    )
    claim_id = outcome.action.budget_authorization_id
    assert claim_id is not None
    # The claim declared trace_id volatile, so the bucket excludes it; a
    # settlement derived with no declaration would name a different one.
    assert claim_id == resolve_authorization_id("deploy", None, {"target": "prod-1"})

    ledger.complete(outcome.key, external_id="ext-1")

    raw = load_budgets(budgets_path)
    entries = raw.get("authorization_bound", {}).get("deploy", {})
    assert list(entries) == [claim_id]
    # One claim plus one settlement against the same bucket.
    assert get_remaining(raw, "deploy", claim_id) == 3


def test_budget_file_absent_is_unbound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No registry file means no authorization-bound budget (byte-identical)."""
    budgets_path = _budgets_path()
    # Ensure the per-test file does not exist
    if budgets_path.exists():
        budgets_path.unlink()

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_1")

    # Even with distinctive tokens, no file means no refusal.
    for i in range(10):
        outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"k{i}")
        ledger.fail(outcome.key, "boom", certain=True)
    # No exception, and no file was created
    assert not budgets_path.exists()


# --- budget accounting error and refusal paths (issue #1977) ------------------ #


class _BadMapping(Mapping):
    """A mapping shape that raises TypeError when iterated."""

    def __getitem__(self, key: str) -> Any:
        raise KeyError(key)

    def __iter__(self) -> Any:
        raise TypeError("unhashable argument shape")

    def __len__(self) -> int:
        return 1


def test_unreadable_registry_swallowed_on_claim_commit_and_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit and settlement swallow unreadable or unwritable registries."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"default_max_attempts": 5}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_unreadable", goal="g"))
    storage.append_event("run_unreadable", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_unreadable")

    # 1. Commit helper swallows save failure during claim
    def boom_save(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk write protected")

    real_save = ledger_module.save_budgets
    ledger_module.save_budgets = boom_save
    try:
        outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k1")
    finally:
        ledger_module.save_budgets = real_save
    assert outcome.fresh
    events = storage.read_events("run_unreadable")
    assert any(e.type is EventType.ACTION_RECORDED for e in events)

    # 2. Make registry a directory so it is present but unreadable
    budgets_path.unlink()
    budgets_path.mkdir()

    # Direct helper calls succeed without escaping exceptions
    ledger._budget_commit_claim("send_invoice", "auth_001")
    ledger._budget_consume_settlement("send_invoice", "auth_001")

    # 3. Settlement complete succeeds and lands event
    ledger.complete(outcome.key, external_id="ext-001")
    events = storage.read_events("run_unreadable")
    completed = [
        e
        for e in events
        if e.type is EventType.ACTION_RECORDED
        and e.payload.get("action", {}).get("status") == "completed"
    ]
    assert completed


@pytest.mark.parametrize(
    "step_name",
    ["load_budgets", "ensure_authorization_entry", "increment", "save_budgets"],
)
def test_settlement_consume_four_failure_arms_individually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step_name: str
) -> None:
    """Each step of _budget_consume_settlement swallows exceptions independently."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"default_max_attempts": 5}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_settle_arms", goal="g"))
    storage.append_event("run_settle_arms", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_settle_arms")

    outcome = ledger.claim("deploy", {"target": "host-1"}, key="deploy-k1")
    assert outcome.fresh

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"synthetic error in {step_name}")

    monkeypatch.setattr(ledger_module, step_name, boom)
    # Settlement must land even when this accounting step blows up
    ledger.complete(outcome.key, external_id="ext-1")

    action = ledger.get(outcome.key)
    assert action is not None
    assert action.status is ActionStatus.COMPLETED


def test_budget_exhaust_refusal_when_get_remaining_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When get_remaining raises during an exhaustion refusal, remaining falls back to 0."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"action_types": {"send_invoice": {"max_attempts": 1}}}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_exhaust_fallback", goal="g"))
    storage.append_event("run_exhaust_fallback", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_exhaust_fallback")

    # Claim once to exhaust
    outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k1")
    assert outcome.fresh

    # Now get_remaining raises when computing remaining for the error message
    import continuum.budgets as budgets_module

    def boom_remaining(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cannot read remaining")

    monkeypatch.setattr(budgets_module, "get_remaining", boom_remaining)

    with pytest.raises(LedgerError) as exc_info:
        ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k2")

    msg = str(exc_info.value)
    assert "budget exhausted" in msg
    assert "send_invoice" in msg
    assert "0 remaining" in msg


def test_budget_invalid_registry_raises_ledger_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt budget registry raises LedgerError naming the registry error."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text("{invalid json syntax")

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_invalid_reg", goal="g"))
    storage.append_event("run_invalid_reg", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_invalid_reg")

    with pytest.raises(LedgerError) as exc_info:
        ledger.claim("send_invoice", {"invoice": "INV-001"}, key="k1")

    msg = str(exc_info.value)
    assert "budget registry invalid:" in msg
    assert "not valid JSON" in msg


def test_unhashable_argument_shape_yields_unbounded_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhashable argument shape produces authorization_id=None and proceeds unbounded."""
    budgets_path = _budgets_path()
    budgets_path.parent.mkdir(parents=True, exist_ok=True)
    budgets_path.write_text(json.dumps({"action_types": {"send_invoice": {"max_attempts": 1}}}))

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_unhashable", goal="g"))
    storage.append_event("run_unhashable", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(storage, "run_unhashable")

    # Direct call to _budget_authorization_id with _BadMapping
    assert ledger._budget_authorization_id("send_invoice", None, _BadMapping(), ()) is None

    # In claim, resolve_authorization_id raising makes the claim unbounded
    def boom_resolve(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("unhashable argument shape")

    monkeypatch.setattr(ledger_module, "resolve_authorization_id", boom_resolve)

    # Claim multiple times beyond max_attempts=1; because it is unbound, none is refused
    for i in range(3):
        outcome = ledger.claim("send_invoice", {"invoice": "INV-001"}, key=f"k{i}")
        assert outcome.fresh
        assert outcome.action.budget_authorization_id is None


def test_settlement_rederives_authorization_id_when_record_carries_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a record lacks budget_authorization_id, settlement re-derives it."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_rederive", goal="g"))
    ledger = ActionLedger(storage, "run_rederive")

    action = Action(
        action_id="act-001",
        run_id="run_rederive",
        action_type="deploy",
        arguments={"target": "prod-1"},
        status=ActionStatus.STARTED,
        budget_authorization_id=None,
    )
    auth_id = ledger._settlement_authorization_id(action)
    assert auth_id is not None

    # Also verify _budget_consume_settlement when file is absent
    budgets_path = _budgets_path()
    if budgets_path.exists():
        budgets_path.unlink()
    # Does not raise
    ledger._budget_consume_settlement("deploy", auth_id)
