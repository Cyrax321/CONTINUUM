"""Replay-safety guard (issue #237).

The decision table is shared by the gate, the gateway and the thin
adapters via `evaluate`; `protected_call` adds memoised execution; and
`langgraph_protected_node` closes the interrupt-replay window (#6208,
ACRFence) for graph nodes. The chaos matrix at the bottom encodes the
crash points from the durable-execution survey as executable tests.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TypedDict

import pytest

pytest.importorskip("langgraph.checkpoint.base")

from langgraph.graph import StateGraph  # noqa: E402

from continuum.actions import ActionLedger  # noqa: E402
from continuum.cli import ExitCode, main  # noqa: E402
from continuum.events import EventType  # noqa: E402
from continuum.models import ActionStatus, Run  # noqa: E402
from continuum.replayguard import (  # noqa: E402
    GuardKind,
    ReplayBlocked,
    evaluate,
    langgraph_protected_node,
    protected_call,
)
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "rg.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


def seed(db: str, action_type: str, rendered: str, status: ActionStatus) -> None:
    from continuum.actions.idempotency import idempotency_key

    key = idempotency_key(action_type, None, scope="run_1", key=rendered)
    action_status = {
        ActionStatus.STARTED: {"status": "started"},
        ActionStatus.COMPLETED: {"status": "completed", "external_id": "x-1"},
        ActionStatus.UNKNOWN: {"status": "unknown", "side_effect_uncertain": True},
        ActionStatus.FAILED: {"status": "failed"},
    }
    payload = {
        "key": key,
        "action": {
            "action_id": f"a_{rendered}_{status.value}",
            "action_type": action_type,
            "run_id": "run_1",
            **action_status[status],
        },
    }
    with SQLiteStorage(db) as store:
        store.append_event("run_1", EventType.ACTION_RECORDED, payload)


def verdict(db: str, action_type: str, rendered: str):
    from continuum.actions.ledger import fold_action_events

    with SQLiteStorage(db) as store:
        folded = fold_action_events(store.read_events("run_1"))
    return evaluate(
        action_type=action_type,
        rendered_key=rendered,
        run_id="run_1",
        actions_by_key=folded,
    )


# --- decision table ------------------------------------------------------------ #


def test_decision_table_matches_the_gate_contract(db: str) -> None:
    assert verdict(db, "send_invoice", "i:1").kind is GuardKind.DENY_UNCLAIMED
    seed(db, "send_invoice", "i:1", ActionStatus.STARTED)
    assert verdict(db, "send_invoice", "i:1").kind is GuardKind.ALLOW
    seed(db, "send_invoice", "i:2", ActionStatus.COMPLETED)
    assert verdict(db, "send_invoice", "i:2").kind is GuardKind.SKIP_DUPLICATE
    seed(db, "send_invoice", "i:3", ActionStatus.UNKNOWN)
    assert verdict(db, "send_invoice", "i:3").kind is GuardKind.BLOCK_UNCERTAIN
    seed(db, "send_invoice", "i:4", ActionStatus.FAILED)
    assert verdict(db, "send_invoice", "i:4").kind is GuardKind.DENY_RECLAIM


def test_block_uncertain_decision_carries_its_key(db: str) -> None:
    # The decision table contract: every decision names the ledger key it is
    # about, so protected_call can act on it. BLOCK_UNCERTAIN used to be built
    # without the key, which turned the documented ReplayBlocked into a bare
    # AssertionError at the `decision.key is not None` checkpoint (issue #735).
    seed(db, "send_invoice", "i:3", ActionStatus.UNKNOWN)
    assert verdict(db, "send_invoice", "i:3").key is not None


def test_protected_call_raises_replayblocked_for_an_uncertain_action(db: str) -> None:
    # The public contract: uncertain states raise ReplayBlocked rather than
    # guessing. With the key missing the wrapper died on an AssertionError
    # first, so callers could not catch the documented exception (issue #735).
    seed(db, "send_invoice", "i:5", ActionStatus.UNKNOWN)
    with pytest.raises(ReplayBlocked) as excinfo:
        protected_call(
            SQLiteStorage(db),
            "run_1",
            action_type="send_invoice",
            key="i:5",
            fn=lambda: "SHOULD_NOT_RUN",
        )
    assert excinfo.value.decision.kind is GuardKind.BLOCK_UNCERTAIN
    assert excinfo.value.decision.key is not None


def make_run(db: str) -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})


def test_gate_delegates_to_the_replayguard_core(db: str) -> None:
    from continuum.actions.ledger import fold_action_events
    from continuum.gate import decide as gate_decide

    seed(db, "send_invoice", "i:9", ActionStatus.STARTED)
    with SQLiteStorage(db) as store:
        folded = fold_action_events(store.read_events("run_1"))
    gate = gate_decide(
        config={"tools": {"send_invoice": {"key_template": "{invoice_id}"}}},
        tool_name="send_invoice",
        tool_input={"invoice_id": "i:9"},
        run_id="run_1",
        actions_by_key=folded,
    )
    core = evaluate(
        action_type="send_invoice",
        rendered_key="i:9",
        run_id="run_1",
        actions_by_key=folded,
    )
    assert gate.allow is True and core.kind is GuardKind.ALLOW


# --- protected_call: memoisation + failure semantics ----------------------------- #


def test_protected_call_executes_once_then_returns_cached_result(db: str) -> None:
    calls: list[int] = []

    def effect() -> dict[str, object]:
        calls.append(len(calls))
        return {"sent": True}

    kind1, result1 = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="send_email",
        key="email:a@b",
        fn=effect,
    )
    assert kind1 is GuardKind.ALLOW and result1 == {"sent": True}

    kind2, result2 = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="send_email",
        key="email:a@b",
        fn=effect,
    )
    assert kind2 is GuardKind.SKIP_DUPLICATE
    assert result2 == {"sent": True}
    assert len(calls) == 1, "side effect must not re-fire"


def test_replay_returns_the_callers_own_dict_even_with_a_return_key(db: str) -> None:
    # A dict result containing the literal key "return" used to be journalled
    # as-is and then unwrapped on replay, so the second call returned one
    # member of the dict while the first returned the whole thing (issue #736).
    payload = {"return": "receipt-1", "amount": 100}

    kind1, result1 = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="send_invoice",
        key="inv:9",
        fn=lambda: payload,
    )
    kind2, result2 = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="send_invoice",
        key="inv:9",
        fn=lambda: payload,
    )
    assert kind1 is GuardKind.ALLOW and kind2 is GuardKind.SKIP_DUPLICATE
    assert result2 == result1 == payload, "replay must answer as the first call did"


def test_non_dict_results_round_trip_through_the_envelope(db: str) -> None:
    kind1, result1 = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="charge_card",
        key="card:2",
        fn=lambda: "charged",
    )
    kind2, result2 = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="charge_card",
        key="card:2",
        fn=lambda: "charged",
    )
    assert kind1 is GuardKind.ALLOW and result1 == "charged"
    assert kind2 is GuardKind.SKIP_DUPLICATE and result2 == "charged"


def test_a_legacy_return_journal_still_unwraps_on_replay(db: str) -> None:
    # Records written before the envelope (issue #736) wrap non-dict results
    # as {"return": ...}; replay must keep unwrapping those.
    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_1")
        outcome = ledger.claim("legacy_call", {}, key="legacy:1")
        ledger.complete(outcome.key, result={"return": "legacy-value"})
    kind, value = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="legacy_call",
        key="legacy:1",
        fn=lambda: "SHOULD_NOT_RUN",
    )
    assert kind is GuardKind.SKIP_DUPLICATE
    assert value == "legacy-value"


def test_exception_marks_uncertain_failure_and_reraises(db: str) -> None:
    with pytest.raises(RuntimeError):
        protected_call(
            SQLiteStorage(db),
            "run_1",
            action_type="charge_card",
            key="card:c1",
            fn=lambda: (_ for _ in ()).throw(RuntimeError("timeout after send")),
        )
    action = last_action(db)
    assert action.status is ActionStatus.UNKNOWN
    assert action.side_effect_uncertain is True


def last_action(db: str):
    from continuum.actions.ledger import fold_action_events

    with SQLiteStorage(db) as store:
        folded = fold_action_events(store.read_events("run_1"))
    return list(folded.values())[-1]


# --- LangGraph node protection ----------------------------------------------------- #


class S(TypedDict):  # type: ignore[valid-type]
    value: str


def _graph_with_protected_node(db: str, counter: list[int]):
    g = StateGraph(dict)
    g.add_node(
        "effect",
        langgraph_protected_node(SQLiteStorage(db), "run_1")(
            lambda state: (counter.append(1), {"value": f"done-{len(counter)}"})[1]
        ),
    )
    g.set_entry_point("effect")
    return g.compile()


def test_langgraph_node_fires_once_across_resume(db: str) -> None:
    counter: list[int] = []

    app1 = _graph_with_protected_node(db, counter)
    cfg = {"configurable": {"thread_id": "t1"}}
    app1.invoke({"value": "start"}, cfg)
    assert len(counter) == 1

    # Fresh graph instance over the same storage = resumed process. The
    # completed node is skipped and its journalled result feeds the graph.
    app2 = _graph_with_protected_node(db, counter)
    app2.invoke({"value": "start"}, cfg)
    assert len(counter) == 1, "protected node re-fired on resume"


def test_chaos_matrix_crash_points(db: str) -> None:
    """Zylos crash-point matrix, encoded: each point leaves the ledger in a
    state whose recovery story is deterministic."""
    # 1. crash AFTER claim BEFORE effect: STARTED survives -> uncertain on resume.
    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("api.call", {}, key="c:before")
    v = verdict(db, "api.call", "c:before")
    assert v.kind is GuardKind.ALLOW  # live slot; the retried call may proceed

    # 2. crash AFTER effect BEFORE journal write: indistinguishable from (1),
    #    which is WHY reconciliation exists - the honest answer is "check".
    #    (Encoded above; both collapse to a live STARTED slot.)

    # 3. journal write survived but process died later: COMPLETED -> skip.
    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_1")
        outcome = ledger.claim("api.call", {}, key="c:after")
        ledger.complete(outcome.key, external_id="resp-200")
    v3 = verdict(db, "api.call", "c:after")
    assert v3.kind is GuardKind.SKIP_DUPLICATE


def test_cli_still_green_after_refactor(db: str) -> None:
    code, _, err = run("--db", db, "--json", "verify", "run_1")
    assert code == ExitCode.OK


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()
