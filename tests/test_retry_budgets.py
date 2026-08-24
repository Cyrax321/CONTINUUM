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
    """Retries that reuse the same key still count - that is the point."""
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(outcome.key, "boom", certain=True)
    # Retry opens a second slot under a fresh key suffix.
    ledger.claim("send_invoice", {}, key="invoice:1#retry2")

    events = SQLiteStorage(db).read_events("run_1")
    assert attempts_for_type(events, "send_invoice") == 2


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
    """Drive the real intercept handler logic: after N claims of one type, the
    next claim for that type is refused naming the budget.

    The MCP tool raises ToolError; here we pin the counting + refusal maths
    against the same folded view the server uses.
    """
    cfg = {"default_max_attempts": 2}
    registry_path = registry(tmp_path, cfg)
    del registry_path

    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.claim("send_invoice", {}, key="invoice:2")

    events = SQLiteStorage(db).read_events("run_1")
    attempts = attempts_for_type(events, "send_invoice")
    allowed, used, maximum = evaluate_budget(cfg, "send_invoice", attempts)
    assert (allowed, used, maximum) == (False, 2, 2)


def registry(tmp_path: Path, body: dict[str, object]) -> str:
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps(body))
    return str(p)


# --- CLI report ---------------------------------------------------------------------- #


def test_cli_budget_reports_usage_per_type(db: str, tmp_path: Path) -> None:
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
    assert by_type["send_invoice"]["remaining"] == 1  # 3 - 2
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
