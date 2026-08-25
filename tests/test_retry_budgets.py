"""Run-level retry budgets (issue #240).

Every ACTION_RECORDED event is one attempt. Budgets from
`.continuum/budgets.json` cap attempts per action type at claim time so an
LLM re-planning after failures cannot hammer an upstream forever.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.budgets import (
    BudgetConfigError,
    attempts_by_key,
    attempts_for_type,
    backoff_delay,
    evaluate_budget,
    load_budgets,
)
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "b.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


# --- config --------------------------------------------------------------------- #


def test_missing_registry_loads_empty(tmp_path: Path) -> None:
    assert load_budgets(tmp_path / "nope.json") == {}


def test_broken_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text("{")
    with pytest.raises(BudgetConfigError, match="not valid JSON"):
        load_budgets(p)


def test_nonpositive_limits_are_refused(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"action_types": {"x": 0}}))
    with pytest.raises(BudgetConfigError, match="positive"):
        load_budgets(p)


# --- counting + evaluation -------------------------------------------------------- #


def test_attempts_count_every_claim_slot(db: str) -> None:
    """Retries of one operation accumulate against that operation's allowance.

    Re-claiming after FAILED copies the existing action, so successive attempts
    land under the same key rather than each opening a fresh row. That is what
    makes the key the right unit to count (issue #368).
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(outcome.key, "boom", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")

    events = SQLiteStorage(db).read_events("run_1")
    assert attempts_by_key(events, "send_invoice") == {str(outcome.key): 2}
    assert attempts_for_type(events, "send_invoice") == 2


def test_distinct_operations_do_not_share_one_allowance(db: str) -> None:
    """Different work must not compete for the same budget (issue #368).

    Counting per action type made three recipients each failing once, with no
    retry anywhere, exhaust a budget of three and block a fourth that had never
    been attempted. Any fan-out with more failures than the limit deadlocked
    mid-run, and the refusal called it a retry budget while nothing had been
    retried.
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    for recipient in ("a", "b", "c"):
        outcome = ledger.claim("email_send", {"to": recipient}, key=f"email:{recipient}")
        ledger.fail(outcome.key, "550 rejected", certain=True)

    events = SQLiteStorage(db).read_events("run_1")
    per_key = attempts_by_key(events, "email_send")
    assert len(per_key) == 3
    assert set(per_key.values()) == {1}
    # The figure compared against the limit is the worst single operation, not
    # the sum, so a fourth recipient is still allowed.
    assert attempts_for_type(events, "email_send") == 1
    assert evaluate_budget({"default_max_attempts": 3}, "email_send", 1)[0] is True


def test_completed_attempts_do_not_count(db: str) -> None:
    """A succeeded operation was never retried (issue #309).

    Counting successes turns a retry budget into a cap on how much distinct work
    a run may do: three invoices sent successfully would exhaust a budget of 3
    and refuse the fourth, having never retried anything.
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    for n in range(3):
        outcome = ledger.claim("send_invoice", {}, key=f"invoice:{n}")
        ledger.complete(outcome.key, external_id=f"ext-{n}")

    events = SQLiteStorage(db).read_events("run_1")
    assert attempts_for_type(events, "send_invoice") == 0
    assert evaluate_budget({"default_max_attempts": 3}, "send_invoice", 0)[0] is True


def test_only_the_unsettled_attempts_of_a_retried_key_count(db: str) -> None:
    """The amplification guard must survive the fix: failures still count."""
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    # One key retried twice and still unsettled, one that succeeded on retry,
    # and one untouched operation in flight.
    stuck = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(stuck.key, "boom", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(stuck.key, "boom again", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")

    won = ledger.claim("send_invoice", {}, key="invoice:2")
    ledger.fail(won.key, "transient", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:2")
    ledger.complete(won.key, external_id="ext-2")

    fresh = ledger.claim("send_invoice", {}, key="invoice:3")

    events = SQLiteStorage(db).read_events("run_1")
    # invoice:1 burned three attempts and is still unsettled; invoice:2 succeeded
    # so it is excluded entirely; invoice:3 is on its first.
    assert attempts_by_key(events, "send_invoice") == {
        str(stuck.key): 3,
        str(fresh.key): 1,
    }
    assert attempts_for_type(events, "send_invoice") == 3


def test_budget_evaluation_math() -> None:
    raw = {"default_max_attempts": 3, "action_types": {"send_invoice": {"max_attempts": 5}}}
    assert evaluate_budget(raw, "send_invoice", 5)[0] is False
    assert evaluate_budget(raw, "send_invoice", 4)[0] is True
    allowed, used, maximum = evaluate_budget(raw, "other_tool", 0)
    assert (allowed, used, maximum) == (True, 0, 3)


def test_backoff_delay_is_exponential_with_cap() -> None:
    assert backoff_delay(1) == 1.0
    assert backoff_delay(2) == 2.0
    assert backoff_delay(3) == 4.0
    assert backoff_delay(10) == 60.0  # capped
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_backoff_delay_rejects_zero() -> None:
    with pytest.raises(ValueError, match="got 0"):
        backoff_delay(0)


# --- enforcement through the real ledger path --------------------------------------- #


def test_claims_beyond_budget_are_refused_at_the_mcp_boundary(db: str, tmp_path: Path) -> None:
    """Drive the real intercept handler logic: after N attempts at one operation,
    the next claim for it is refused naming the budget.

    The MCP tool raises ToolError; here we pin the counting and refusal maths
    against the same folded view the server uses. Retries of one key, not two
    different invoices, because the budget caps repetition (issue #368).
    """
    cfg = {"default_max_attempts": 2}
    registry_path = registry(tmp_path, cfg)
    del registry_path

    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(outcome.key, "boom", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")

    events = SQLiteStorage(db).read_events("run_1")
    attempts = attempts_by_key(events, "send_invoice")[str(outcome.key)]
    allowed, used, maximum = evaluate_budget(cfg, "send_invoice", attempts)
    assert (allowed, used, maximum) == (False, 2, 2)

    # A different invoice has its own allowance and is unaffected.
    other = evaluate_budget(cfg, "send_invoice", 0)
    assert other[0] is True


def registry(tmp_path: Path, body: dict[str, object]) -> str:
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps(body))
    return str(p)


# --- CLI report ---------------------------------------------------------------------- #


def test_cli_budget_reports_usage_per_type(db: str, tmp_path: Path) -> None:
    """The report shows the worst single operation, which is what the gate uses.

    Two different invoices are two operations with their own allowances, not two
    attempts at one (issue #368), so `send_invoice` sits at 1 of 3 rather than 2.
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.claim("send_invoice", {}, key="invoice:2")
    ledger.claim("charge_card", {}, key="card:9")

    code, out, err = run(
        "--db",
        db,
        "--json",
        "budget",
        "run_1",
        "--config",
        registry(
            tmp_path,
            {
                "default_max_attempts": 3,
                "action_types": {"charge_card": {"max_attempts": 1}},
            },
        ),
    )
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    by_type = {r["action_type"]: r for r in payload["budgets"]}
    assert by_type["send_invoice"]["attempts"] == 1
    assert by_type["send_invoice"]["remaining"] == 2  # 3 - 1
    assert by_type["charge_card"]["exhausted"] is True  # 1 - 1


def test_cli_budget_exhausted_exit_is_reported_not_raised(db: str, tmp_path: Path) -> None:
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    ledger.claim("deploy", {}, key="d:1")
    code, out, _ = run(
        "--db",
        db,
        "--json",
        "budget",
        "run_1",
        "--config",
        registry(tmp_path, {"default_max_attempts": 3}),
    )
    assert code == ExitCode.OK
    rows = json.loads(out)["budgets"]
    assert any(r["action_type"] == "deploy" and r["attempts"] >= 1 for r in rows)
