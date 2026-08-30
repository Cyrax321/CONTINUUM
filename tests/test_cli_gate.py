"""The pre-action gate (issue #217).

The gate closes the enforcement half of the durability gap: a registered
side-effect tool call is allowed only when a live ledger claim already exists
for its derived key. These tests cover the decision table, the key-derivation
contract against the real idempotency function, config failure modes, and the
exit-code contract a hook transport depends on (0 allow, 2 deny).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.actions.idempotency import idempotency_key
from continuum.cli import ExitCode, main
from continuum.gate import (
    GateConfigError,
    decide,
    load_gate_config,
    render_key,
)
from continuum.models import Action, ActionStatus, Run
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def payload(tool: str, **args: object) -> dict[str, object]:
    return {"tool_name": tool, "tool_input": args}


CONFIG = {
    "tools": {
        "send_invoice": {"key_template": "invoice:{customer}:{invoice_id}"},
        "mcp__acme__charge": {"key_template": "charge:{id}", "action_type": "charge_card"},
    }
}


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "gate.db")
    with SQLiteStorage(path) as store:
        store.create_run_started(Run(run_id="run_1", goal="Do side effects"))
    yield path


# --- pure logic -------------------------------------------------------------- #


def test_render_key_substitutes_top_level_fields() -> None:
    assert render_key("invoice:{customer}:{invoice_id}", {"customer": "acme", "invoice_id": 7}) == (
        "invoice:acme:7"
    )


def test_render_key_refuses_a_template_the_call_cannot_satisfy() -> None:
    with pytest.raises(GateConfigError, match="needs argument"):
        render_key("invoice:{invoice_id}", {"customer": "acme"})


@pytest.mark.parametrize("padded", (" 123 ", "123\n", "\n123", "\t123 \r\n"))
def test_render_key_strips_surrounding_whitespace_from_values(padded: str) -> None:
    """Whitespace in an argument must not fork the key (issue #361).

    Key templates are configuration, but the values substituted into them come
    from the model, and an LLM routinely emits a trailing space or newline. Left
    in, ``invoice: 123 `` and ``invoice:123`` are two ledger keys for one
    invoice, which is the one thing the derived key exists to prevent.
    """
    assert render_key("invoice:{id}", {"id": padded}) == render_key("invoice:{id}", {"id": "123"})


def test_render_key_handles_whitespace_only_value() -> None:
    """A whitespace-only value normalizes to an empty string (issue #512).

    When an LLM supplies whitespace-only for an argument, normalize_key_value
    strips it to empty. The template still renders and decide handles the
    resulting key gracefully without error.
    """
    assert render_key("invoice:{id}", {"id": "   "}) == "invoice:"
    decision = decide(
        {"send_invoice": {"key_template": "invoice:{id}"}},
        "send_invoice",
        {"id": "   "},
        run_id="run_1",
        actions_by_key={},
    )
    assert decision.allow is False
    assert "invoice:" in decision.reason


def test_render_key_leaves_non_strings_to_the_templates_format_spec() -> None:
    """Only strings are stripped; a numeric value keeps its type (issue #361).

    Stringifying every value before formatting would be simpler, and would break
    any template carrying a format spec: ``{amount:.2f}`` against ``"1.5"``
    raises rather than rendering, so a working configuration would start failing
    closed on every gated call.
    """
    assert render_key("amount:{amount:.2f}", {"amount": 1.5}) == "amount:1.50"
    assert render_key("invoice:{id}", {"id": 7}) == "invoice:7"


def test_load_gate_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_gate_config(tmp_path / "missing.json") is None


def test_load_gate_config_fails_closed_on_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "gate.json"
    path.write_text("{nope")
    with pytest.raises(GateConfigError):
        load_gate_config(path)


def test_load_gate_config_requires_a_template_per_tool(tmp_path: Path) -> None:
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"tools": {"send_invoice": {}}}))
    with pytest.raises(GateConfigError, match="key_template"):
        load_gate_config(path)


def test_decide_allows_ungated_tools_without_touching_the_ledger() -> None:
    decision = decide(CONFIG["tools"], "Read", {"path": "x"}, run_id="r", actions_by_key={})
    assert decision.allow is True


def _decide(db: str, tool_input: dict[str, object], tool: str = "send_invoice"):
    from continuum.actions.ledger import fold_action_events

    with SQLiteStorage(db) as store:
        folded = fold_action_events(store.read_events("run_1"))
    return decide(CONFIG["tools"], tool, tool_input, run_id="run_1", actions_by_key=folded)


def _seed(db: str, action_type: str, rendered: str, status: ActionStatus) -> None:
    """Append ACTION_RECORDED events shaped exactly like the real writer."""
    from continuum.events import EventType

    key = str(idempotency_key(action_type, None, scope="run_1", key=rendered))
    action = Action(run_id="run_1", action_type=action_type, status=status)
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.ACTION_RECORDED,
            {"key": key, "action": action.model_dump(mode="json")},
        )


# --- the decision table ------------------------------------------------------- #


def test_unclaimed_side_effect_is_denied_with_claim_instructions(db: str) -> None:
    decision = _decide(db, {"customer": "acme", "invoice_id": 7})
    assert decision.allow is False
    assert "continuum_intercept_action" in decision.reason
    assert "invoice:acme:7" in decision.reason


def test_a_live_claim_is_allowed(db: str) -> None:
    _seed(db, "send_invoice", "invoice:acme:7", ActionStatus.STARTED)
    assert _decide(db, {"customer": "acme", "invoice_id": 7}).allow is True


def test_a_completed_call_is_denied_even_though_a_record_exists(db: str) -> None:
    """This is the dedup verdict made physical: the model cannot re-fire an
    effect the ledger knows already happened, claim or no claim."""
    _seed(db, "send_invoice", "invoice:acme:7", ActionStatus.COMPLETED)
    decision = _decide(db, {"customer": "acme", "invoice_id": 7})
    assert decision.allow is False
    assert "already completed" in decision.reason


def test_a_padded_retry_hits_the_dedup_verdict_of_the_clean_claim(db: str) -> None:
    """The harm whitespace stripping prevents, end to end (issue #361).

    The record is for ``invoice:acme:7``. A retry whose arguments carry stray
    whitespace means the same invoice, so it has to land on the same key and be
    refused as already completed. Before the fix it derived
    ``invoice: acme :7``, found no record of itself, and was allowed through --
    a second send of one invoice, which is precisely what the ledger key exists
    to make impossible.
    """
    _seed(db, "send_invoice", "invoice:acme:7", ActionStatus.COMPLETED)
    decision = _decide(db, {"customer": " acme ", "invoice_id": "7\n"})
    assert decision.allow is False
    assert "already completed" in decision.reason


def test_an_uncertain_outcome_is_denied_with_reconcile_instructions(db: str) -> None:
    _seed(db, "send_invoice", "invoice:acme:7", ActionStatus.UNKNOWN)
    decision = _decide(db, {"customer": "acme", "invoice_id": 7})
    assert decision.allow is False
    assert "continuum_reconcile_action" in decision.reason


def test_a_closed_attempt_must_be_reclaimed_before_retrying(db: str) -> None:
    _seed(db, "send_invoice", "invoice:acme:7", ActionStatus.FAILED)
    decision = _decide(db, {"customer": "acme", "invoice_id": 7})
    assert decision.allow is False
    assert "Claim it again" in decision.reason


def test_a_different_key_of_the_same_tool_is_independently_gated(db: str) -> None:
    _seed(db, "send_invoice", "invoice:acme:7", ActionStatus.STARTED)
    other = _decide(db, {"customer": "acme", "invoice_id": 8})
    assert other.allow is False
    same = _decide(db, {"customer": "acme", "invoice_id": 7})
    assert same.allow is True


def test_action_type_override_matches_the_configured_type(db: str) -> None:
    _seed(db, "charge_card", "charge:c1", ActionStatus.STARTED)
    decision = decide(
        CONFIG["tools"],
        "mcp__acme__charge",
        {"id": "c1"},
        run_id="run_1",
        actions_by_key={
            str(idempotency_key("charge_card", None, scope="run_1", key="charge:c1")): (
                Action(run_id="run_1", action_type="charge_card", status=ActionStatus.STARTED)
            )
        },
    )
    assert decision.allow is True


# --- the CLI surface ------------------------------------------------------------ #


def write_config(tmp_path: Path) -> str:
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(CONFIG))
    return str(path)


def test_gate_denies_an_unclaimed_call_with_exit_two(db: str, tmp_path: Path) -> None:
    code, out, err = run(
        "--db",
        db,
        "--json",
        "gate",
        "--config",
        write_config(tmp_path),
        "--payload-file",
        str(_payload_file(tmp_path, customer="acme", invoice_id=7)),
    )
    assert code == 2
    assert json.loads(out)["allow"] is False
    assert "continuum_intercept_action" in err


def _payload_file(tmp_path: Path, **args: object) -> Path:
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(payload("send_invoice", **args)))
    return p


def test_gate_allows_after_a_real_claim_via_the_ledger_api(db: str, tmp_path: Path) -> None:
    from continuum.actions.ledger import ActionLedger

    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim(
            "send_invoice", {}, key="invoice:acme:9", scoped_to_run=True
        )
    code, out, err = run(
        "--db",
        db,
        "--json",
        "gate",
        "--config",
        write_config(tmp_path),
        "--payload-file",
        str(_payload_file(tmp_path, customer="acme", invoice_id=9)),
    )
    assert code == ExitCode.OK, err
    assert json.loads(out)["allow"] is True


def test_gate_fast_paths_when_no_config_exists(db: str, tmp_path: Path) -> None:
    p = tmp_path / "p.json"
    p.write_text(json.dumps(payload("send_invoice", customer="a", invoice_id=1)))
    code, out, _ = run("--db", db, "--json", "gate", "--payload-file", str(p))
    assert code == ExitCode.OK
    assert json.loads(out)["reason"] == "no gate configured"


def test_gate_denies_when_the_registry_is_corrupt(db: str, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    p = tmp_path / "p.json"
    p.write_text(json.dumps(payload("send_invoice", customer="a", invoice_id=1)))
    code, _, err = run("--db", db, "--json", "gate", "--config", str(bad), "--payload-file", str(p))
    assert code == 2
    assert "denying until it is fixed" in err


def test_gate_denies_a_gated_call_when_no_run_is_active(tmp_path: Path) -> None:
    empty = str(tmp_path / "empty.db")
    with SQLiteStorage(empty):
        pass
    p = tmp_path / "p.json"
    p.write_text(json.dumps(payload("send_invoice", customer="a", invoice_id=1)))
    code, _, err = run(
        "--db",
        empty,
        "--json",
        "gate",
        "--config",
        write_config(tmp_path),
        "--payload-file",
        str(p),
    )
    assert code == 2
    assert "no active CONTINUUM run" in err


def test_gate_tolerates_an_unparseable_payload(db: str, tmp_path: Path) -> None:
    p = tmp_path / "p.json"
    p.write_text("{junk")
    code, _, err = run(
        "--db", db, "--json", "gate", "--config", write_config(tmp_path), "--payload-file", str(p)
    )
    assert code == ExitCode.OK
    assert "allowing" in err


# --- config errors name a file the operator can open (issue #333) ------------- #


@pytest.mark.parametrize(
    "filename,module,loader,error",
    [
        ("gate.json", "continuum.gate", "load_gate_config", "GateConfigError"),
        ("budgets.json", "continuum.budgets", "load_budgets", "BudgetConfigError"),
        ("reconcilers.json", "continuum.reconcilers", "load_reconcilers", "ReconcilerConfigError"),
        ("gateway.json", "continuum.gateway", "load_gateway_config", "GatewayConfigError"),
    ],
)
def test_a_registry_error_names_an_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    module: str,
    loader: str,
    error: str,
) -> None:
    """Every registry loader, not just the gate.

    #333 fixed this for two of the gate's four messages. The rationale is that a
    relative path depends on the cwd of whatever loaded the registry, which for a
    hook or the sidecar is not the operator's shell, so the message names a file
    they cannot find. That reasoning is identical for the budget, reconciler and
    gateway registries, which all load from `.continuum/` by relative default, so
    this covers the class rather than the one instance.

    Parameterised deliberately: a fifth registry added later without resolving
    its path shows up here as a missing case rather than as a support question.
    """
    import importlib

    monkeypatch.chdir(tmp_path)
    relative = Path(".continuum") / filename
    relative.parent.mkdir(exist_ok=True)
    relative.write_text("{ this is not valid json")

    mod = importlib.import_module(module)
    with pytest.raises(getattr(mod, error)) as caught:
        getattr(mod, loader)(relative)

    message = str(caught.value)
    # The reported path must be absolute. Asserting the relative form is absent
    # would be unsound, since the absolute path legitimately ends with it.
    reported = Path(message.split(" is not valid JSON")[0])
    assert reported.is_absolute(), message
    assert reported.name == filename, message
    assert reported == relative.resolve(), message
