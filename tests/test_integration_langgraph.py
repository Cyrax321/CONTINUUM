"""End-to-end integration: CONTINUUM architecture on a real LangGraph graph.

These tests compile and execute an actual LangGraph ``StateGraph`` wired to the
LangGraph adapter, exercising the three pillars of the architecture:

* durable semantic checkpoints (``checkpoint_node``),
* exactly-once external side effects (``wrap_tool`` idempotency), and
* recovery validation (``CheckpointManager.restore`` + ``RecoveryEngine.assess``)
  on state produced by a real LangGraph run.
"""

from __future__ import annotations

import warnings

import pytest
from typing_extensions import TypedDict

from continuum.adapters.langgraph import LangGraphAgentAdapter
from continuum.checkpoint.manager import CheckpointManager
from continuum.events import EventType
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage
from continuum.storage.base import Storage

warnings.filterwarnings("ignore")

langgraph = pytest.importorskip("langgraph")
from langgraph.graph import END, START, StateGraph  # noqa: E402


@pytest.fixture
def store() -> Storage:
    return SQLiteStorage(":memory:")


class OrderState(TypedDict):
    continuum_run_id: str
    order_id: str
    notified: bool
    recovered: bool


@pytest.mark.skipif(langgraph is None, reason="langgraph not installed")
class TestLangGraphArchitecture:
    def test_checkpoint_written_and_side_effect_is_idempotent(self, store: Storage) -> None:
        adapter = LangGraphAgentAdapter(store)
        run_id = "lg_arch_1"
        adapter.start_run(goal="process order", run_id=run_id)

        calls = {"notify": 0}

        @adapter.wrap_tool("notify.customer")
        def notify(order_id: str, *, continuum_run_id: str = "") -> str:
            calls["notify"] += 1
            return f"notified {order_id}"

        def work(state: dict) -> dict:
            result = notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
            return {"notified": result.startswith("notified")}

        def checkpoint(state: dict) -> dict:
            return adapter.checkpoint_node(state)

        def finalize(state: dict) -> dict:
            return {}

        builder = StateGraph(OrderState)
        builder.add_node("work", work)
        builder.add_node("checkpoint", checkpoint)
        builder.add_node("finalize", finalize)
        builder.add_edge(START, "work")
        builder.add_edge("work", "checkpoint")
        builder.add_edge("checkpoint", "finalize")
        builder.add_edge("finalize", END)
        graph = builder.compile()

        initial = {
            "continuum_run_id": run_id,
            "order_id": "O-1",
            "notified": False,
            "recovered": False,
        }
        out = graph.invoke(initial)
        assert out["notified"] is True
        # external side effect executed once on the first invocation
        assert calls["notify"] == 1

        # A duplicate invocation (e.g. a resumed run) must not re-fire the side effect.
        graph.invoke(initial)
        assert calls["notify"] == 1

        # The checkpoint node must have persisted a STATE_CHECKPOINTED event.
        events = store.read_events(run_id)
        assert any(e.type is EventType.STATE_CHECKPOINTED for e in events)

        # The checkpoint must be restorable into a SemanticState.
        restored = CheckpointManager(store).restore(run_id)
        assert restored.state.run_id == run_id

    def test_recovery_assesses_resume_on_langgraph_state(self, store: Storage) -> None:
        adapter = LangGraphAgentAdapter(store)
        run_id = "lg_arch_2"
        adapter.start_run(goal="process order", run_id=run_id)

        def work(state: dict) -> dict:
            return {"notified": True}

        def checkpoint(state: dict) -> dict:
            return adapter.checkpoint_node(state)

        builder = StateGraph(OrderState)
        builder.add_node("work", work)
        builder.add_node("checkpoint", checkpoint)
        builder.add_edge(START, "work")
        builder.add_edge("work", "checkpoint")
        builder.add_edge("checkpoint", END)
        graph = builder.compile()

        graph.invoke(
            {
                "continuum_run_id": run_id,
                "order_id": "O-2",
                "notified": False,
                "recovered": False,
            }
        )

        # The recovery engine validates the LangGraph-produced state and, for a
        # run started deterministically through the adapter, returns a safe
        # RESUME decision: no uncertain side effects and no environment drift.
        decision = RecoveryEngine(store).assess(run_id)
        assert decision.safe is True
        assert decision.mode.value == "resume"

    def test_crash_after_checkpoint_does_not_duplicate_side_effect(self, store: Storage) -> None:
        adapter = LangGraphAgentAdapter(store)
        run_id = "lg_arch_3"
        adapter.start_run(goal="process order", run_id=run_id)

        calls = {"notify": 0}

        @adapter.wrap_tool("notify.customer")
        def notify(order_id: str, *, continuum_run_id: str = "") -> str:
            calls["notify"] += 1
            return f"notified {order_id}"

        def work(state: dict) -> dict:
            notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
            return {"notified": True}

        def checkpoint(state: dict) -> dict:
            return adapter.checkpoint_node(state)

        def crash(state: dict) -> dict:
            # Simulate a process crash on the first (non resumed) run, AFTER the
            # checkpoint node has already persisted durable state.
            if not state.get("recovered"):
                raise RuntimeError("simulated crash after checkpoint")
            return {}

        builder = StateGraph(OrderState)
        builder.add_node("work", work)
        builder.add_node("checkpoint", checkpoint)
        builder.add_node("crash", crash)
        builder.add_edge(START, "work")
        builder.add_edge("work", "checkpoint")
        builder.add_edge("checkpoint", "crash")
        builder.add_edge("crash", END)
        graph = builder.compile()

        with pytest.raises(RuntimeError):
            graph.invoke(
                {
                    "continuum_run_id": run_id,
                    "order_id": "O-3",
                    "notified": False,
                    "recovered": False,
                }
            )

        # The checkpoint survived the crash.
        events = store.read_events(run_id)
        assert any(e.type is EventType.STATE_CHECKPOINTED for e in events)

        # Resume: re-invoking the graph re-runs work + checkpoint, but the
        # side effect is idempotent so it is not re-executed.
        graph.invoke(
            {
                "continuum_run_id": run_id,
                "order_id": "O-3",
                "notified": False,
                "recovered": True,
            }
        )
        assert calls["notify"] == 1
