"""End-to-end integration: CONTINUUM architecture on a real LangChain pipeline.

These tests compile and execute an actual LangChain LCEL pipeline (composed
from ``RunnableLambda`` steps) wired to the LangChain adapter, exercising the
three pillars of the architecture:

* durable semantic checkpoints (``checkpoint_node``),
* exactly-once external side effects (``wrap_tool`` idempotency), and
* recovery validation (``CheckpointManager.restore`` + ``RecoveryEngine.assess``)
  on state produced by a real LangChain run.
"""

from __future__ import annotations

import warnings

import pytest

from continuum.adapters.langchain import LangChainAgentAdapter
from continuum.checkpoint.manager import CheckpointManager
from continuum.events import EventType
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage
from continuum.storage.base import Storage

warnings.filterwarnings("ignore")

langchain_core = pytest.importorskip("langchain_core")
from langchain_core.runnables import RunnableLambda  # noqa: E402


@pytest.fixture
def store() -> Storage:
    return SQLiteStorage(":memory:")


@pytest.mark.skipif(langchain_core is None, reason="langchain-core not installed")
class TestLangChainArchitecture:
    def test_checkpoint_written_and_side_effect_is_idempotent(self, store: Storage) -> None:
        adapter = LangChainAgentAdapter(store)
        run_id = "lc_arch_1"
        adapter.start_run(goal="process order", run_id=run_id)

        calls = {"notify": 0}

        @adapter.wrap_tool("notify.customer")
        def notify(order_id: str, *, continuum_run_id: str = "") -> str:
            calls["notify"] += 1
            return f"notified {order_id}"

        def work(state: dict) -> dict:
            result = notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
            return {**state, "notified": result.startswith("notified")}

        chain = RunnableLambda(work) | RunnableLambda(adapter.checkpoint_node)

        initial = {"continuum_run_id": run_id, "order_id": "O-1", "notified": False}
        out = chain.invoke(initial)
        assert out["notified"] is True
        # external side effect executed once on the first invocation
        assert calls["notify"] == 1

        # A duplicate invocation (e.g. a resumed run) must not re-fire the side effect.
        chain.invoke(initial)
        assert calls["notify"] == 1

        # The checkpoint node must have persisted a STATE_CHECKPOINTED event.
        events = store.read_events(run_id)
        assert any(e.type is EventType.STATE_CHECKPOINTED for e in events)

        # The checkpoint must be restorable into a SemanticState.
        restored = CheckpointManager(store).restore(run_id)
        assert restored.state.run_id == run_id

    def test_recovery_assesses_resume_on_langchain_state(self, store: Storage) -> None:
        adapter = LangChainAgentAdapter(store)
        run_id = "lc_arch_2"
        adapter.start_run(goal="process order", run_id=run_id)

        def work(state: dict) -> dict:
            return {**state, "notified": True}

        chain = RunnableLambda(work) | RunnableLambda(adapter.checkpoint_node)

        chain.invoke({"continuum_run_id": run_id, "order_id": "O-2", "notified": False})

        # The recovery engine validates the LangChain-produced state and, for a
        # run started deterministically through the adapter, returns a safe
        # RESUME decision: no uncertain side effects and no environment drift.
        decision = RecoveryEngine(store).assess(run_id)
        assert decision.safe is True
        assert decision.mode.value == "resume"

    def test_explicit_key_deduplicates_against_argument_drift(self, store: Storage) -> None:
        """An explicit idempotency key collapses repeated calls even when the
        argument text drifts between invocations.

        This is the failure an LLM-driven tool hits in practice: the same logical
        operation arrives with differently-spelled arguments (e.g. a model stuffs
        a generated sentence into ``order_id`` instead of the id), which defeats
        argument-hash dedup. A stable ``key`` must still collapse them to one
        external side effect.
        """
        adapter = LangChainAgentAdapter(store)
        run_id = "lc_arch_key_1"
        adapter.start_run(goal="process order", run_id=run_id)

        calls = {"notify": 0}

        @adapter.wrap_tool("notify.customer", key="notify:O-9")
        def notify(order_id: str, *, continuum_run_id: str = "") -> str:
            calls["notify"] += 1
            return f"notified {order_id}"

        # First call: the model passes a clean id.
        notify(order_id="O-9", continuum_run_id=run_id)
        assert calls["notify"] == 1

        # Second call "resumes" with a drifted argument spelling of the same op.
        notify(
            order_id="Your order O-9 has been processed successfully.",
            continuum_run_id=run_id,
        )
        # The explicit key identifies the operation, so no second side effect.
        assert calls["notify"] == 1

    def test_key_fn_derives_key_from_call_arguments(self, store: Storage) -> None:
        adapter = LangChainAgentAdapter(store)
        run_id = "lc_arch_key_2"
        adapter.start_run(goal="process order", run_id=run_id)

        calls = {"notify": 0}

        @adapter.wrap_tool(
            "notify.customer",
            key_fn=lambda *a, **k: f"notify:{str(k.get('order_id', '')).strip()}",
        )
        def notify(order_id: str, *, continuum_run_id: str = "") -> str:
            calls["notify"] += 1
            return f"notified {order_id}"

        notify(order_id="O-9", continuum_run_id=run_id)
        # A second call with equivalent identity but drifted text collapses.
        notify(order_id="O-9 ", continuum_run_id=run_id)
        assert calls["notify"] == 1

    def test_crash_after_checkpoint_does_not_duplicate_side_effect(self, store: Storage) -> None:
        adapter = LangChainAgentAdapter(store)
        run_id = "lc_arch_3"
        adapter.start_run(goal="process order", run_id=run_id)

        calls = {"notify": 0}

        @adapter.wrap_tool("notify.customer")
        def notify(order_id: str, *, continuum_run_id: str = "") -> str:
            calls["notify"] += 1
            return f"notified {order_id}"

        def work(state: dict) -> dict:
            notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
            return {**state, "notified": True}

        def crash(state: dict) -> dict:
            # Simulate a process crash on the first (non resumed) run, AFTER the
            # checkpoint node has already persisted durable state.
            if not state.get("recovered"):
                raise RuntimeError("simulated crash after checkpoint")
            return state

        chain = (
            RunnableLambda(work) | RunnableLambda(adapter.checkpoint_node) | RunnableLambda(crash)
        )

        with pytest.raises(RuntimeError):
            chain.invoke(
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

        # Resume: re-invoking the pipeline re-runs work + checkpoint, but the
        # side effect is idempotent so it is not re-executed.
        chain.invoke(
            {"continuum_run_id": run_id, "order_id": "O-3", "notified": False, "recovered": True}
        )
        assert calls["notify"] == 1
