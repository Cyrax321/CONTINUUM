"""Tests for the LangGraph adapter.

These tests verify the adapter without requiring langgraph to be installed,
using mocks where necessary. When langgraph IS available, integration-style
tests exercise the actual StateGraph integration.
"""

from __future__ import annotations

from typing import Any

import pytest

from continuum.adapters import GenericAgentAdapter
from continuum.adapters.langgraph import langgraph_available
from continuum.environment import StaticProvider, capture
from continuum.models import (
    Goal,
    Progress,
    RecoveryMode,
    SemanticState,
)
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> SQLiteStorage:
    return SQLiteStorage(":memory:")


# --------------------------------------------------------------------------- #
# Tests that work WITHOUT langgraph installed
# --------------------------------------------------------------------------- #


class TestLangGraphImport:
    def test_module_imports_without_langgraph(self) -> None:
        """The adapter module should import even without langgraph."""
        import continuum.adapters.langgraph as lg

        assert hasattr(lg, "LangGraphAgentAdapter")
        assert hasattr(lg, "langgraph_available")

    def test_langgraph_available_flag_is_bool(self) -> None:
        import continuum.adapters.langgraph as lg

        assert isinstance(lg.langgraph_available, bool)


class TestEnsureLangGraph:
    def test_ensure_langgraph_raises_when_not_installed(self) -> None:
        """_ensure_langgraph raises ImportError with helpful message when langgraph missing."""
        import continuum.adapters.langgraph as lg

        if lg.langgraph_available:
            pytest.skip("langgraph is installed; cannot test the missing-dep path")

        with pytest.raises(ImportError, match="pip install continuum-agent"):
            lg._ensure_langgraph()


class TestExtractRunId:
    def test_extracts_from_kwargs_run_id(self) -> None:
        from continuum.adapters.langgraph import _extract_run_id

        result = _extract_run_id((), {"run_id": "run_123"})
        assert result == "run_123"

    def test_extracts_from_kwargs_continuum_run_id(self) -> None:
        from continuum.adapters.langgraph import _extract_run_id

        result = _extract_run_id((), {"continuum_run_id": "run_456"})
        assert result == "run_456"

    def test_continuum_run_id_takes_precedence(self) -> None:
        from continuum.adapters.langgraph import _extract_run_id

        result = _extract_run_id((), {"continuum_run_id": "correct", "run_id": "wrong"})
        assert result == "correct"

    def test_extracts_from_first_dict_arg(self) -> None:
        from continuum.adapters.langgraph import _extract_run_id

        result = _extract_run_id(({"continuum_run_id": "run_789"},), {})
        assert result == "run_789"

    def test_returns_none_when_no_run_id(self) -> None:
        from continuum.adapters.langgraph import _extract_run_id

        result = _extract_run_id((), {"other": "value"})
        assert result is None


class TestAdapterConstruction:
    def test_construction_raises_without_langgraph(self, store: SQLiteStorage) -> None:
        """LangGraphAgentAdapter constructor requires langgraph."""
        import continuum.adapters.langgraph as lg

        if lg.langgraph_available:
            pytest.skip("langgraph is installed")

        with pytest.raises(ImportError, match="pip install continuum-agent"):
            lg.LangGraphAgentAdapter(store)


# --------------------------------------------------------------------------- #
# Tests that mock langgraph so we can exercise the adapter logic
# --------------------------------------------------------------------------- #


class TestWithMockedLangGraph:
    """Tests that mock langgraph availability to exercise adapter logic."""

    @pytest.fixture
    def adapter(self, store: SQLiteStorage) -> Any:
        """Create a LangGraphAgentAdapter with langgraph mocked as available."""
        import continuum.adapters.langgraph as lg

        original = lg.langgraph_available
        lg.langgraph_available = True
        try:
            adapter = lg.LangGraphAgentAdapter(store)
            yield adapter
        finally:
            lg.langgraph_available = original

    def test_isinstance_generic_adapter(self, adapter: Any) -> None:
        assert isinstance(adapter, GenericAgentAdapter)

    def test_extract_semantic_state_default(self, adapter: Any) -> None:
        state = {
            "continuum_run_id": "run_lg_1",
            "goal": "Process records",
            "completed_count": 42,
            "total_count": 100,
        }
        semantic = adapter.extract_semantic_state(state)
        assert semantic.run_id == "run_lg_1"
        assert semantic.goal.description == "Process records"
        assert semantic.progress.completed == 42
        assert semantic.progress.total == 100

    def test_extract_semantic_state_minimal(self, adapter: Any) -> None:
        state = {"continuum_run_id": "run_lg_2"}
        semantic = adapter.extract_semantic_state(state)
        assert semantic.run_id == "run_lg_2"
        assert semantic.goal.description == "LangGraph agent task"

    def test_extract_semantic_state_custom_extractor(self, store: SQLiteStorage) -> None:
        import continuum.adapters.langgraph as lg

        original = lg.langgraph_available
        lg.langgraph_available = True
        try:

            def custom_extract(state: dict[str, Any]) -> SemanticState:
                return SemanticState(
                    run_id=state["rid"],
                    goal=Goal(description="custom"),
                    progress=Progress(completed=99),
                )

            adapter = lg.LangGraphAgentAdapter(store, state_to_semantic=custom_extract)
            semantic = adapter.extract_semantic_state({"rid": "custom_1"})
            assert semantic.run_id == "custom_1"
            assert semantic.progress.completed == 99
        finally:
            lg.langgraph_available = original

    def test_checkpoint_node_creates_checkpoint(self, adapter: Any) -> None:
        adapter.start_run(goal="LangGraph task", run_id="run_lg_3")

        state = {
            "continuum_run_id": "run_lg_3",
            "goal": "LangGraph task",
            "completed_count": 10,
        }
        result = adapter.checkpoint_node(state)
        assert result == {}

        # A checkpoint was persisted and is restorable. When the event log
        # already carries work (RUN_STARTED here), checkpoint_node projects the
        # authoritative state from the log rather than trusting the dict fields
        # (see issue #46), so the run is restorable to its checkpointed state.
        restored = adapter.restore_state("run_lg_3")
        assert restored.run_id == "run_lg_3"

    def test_checkpoint_node_no_run_id_is_noop(self, adapter: Any) -> None:
        result = adapter.checkpoint_node({"no_run_id": True})
        assert result == {}

    def test_wrap_tool_deduplicates(self, adapter: Any) -> None:
        adapter.start_run(goal="Tool test", run_id="run_lg_4")

        call_count = 0

        @adapter.wrap_tool("external.api_call")
        def api_call(endpoint: str, continuum_run_id: str = "") -> dict:
            nonlocal call_count
            call_count += 1
            return {"status": "ok", "endpoint": endpoint}

        # First call executes
        r1 = api_call(endpoint="/data", continuum_run_id="run_lg_4")
        assert call_count == 1
        assert r1 == {"status": "ok", "endpoint": "/data"}

        # Second call deduplicates
        r2 = api_call(endpoint="/data", continuum_run_id="run_lg_4")
        assert call_count == 1
        assert r2 == {"status": "ok", "endpoint": "/data"}

    def test_wrap_tool_different_args_executes(self, adapter: Any) -> None:
        adapter.start_run(goal="Tool test", run_id="run_lg_5")

        call_count = 0

        @adapter.wrap_tool("external.api_call")
        def api_call(endpoint: str, continuum_run_id: str = "") -> dict:
            nonlocal call_count
            call_count += 1
            return {"endpoint": endpoint}

        api_call(endpoint="/a", continuum_run_id="run_lg_5")
        api_call(endpoint="/b", continuum_run_id="run_lg_5")
        assert call_count == 2

    def test_wrap_tool_no_run_id_falls_through(self, adapter: Any) -> None:
        """If no run_id can be extracted, the tool runs without interception."""
        call_count = 0

        @adapter.wrap_tool("unscoped_action")
        def unscoped() -> str:
            nonlocal call_count
            call_count += 1
            return "done"

        result = unscoped()
        assert result == "done"
        assert call_count == 1

        # Second call also runs (no dedup without run_id)
        result2 = unscoped()
        assert result2 == "done"
        assert call_count == 2

    def test_wrap_tool_preserves_function_name(self, adapter: Any) -> None:
        @adapter.wrap_tool("test")
        def my_tool() -> str:
            """My docstring."""
            return "result"

        assert my_tool.__name__ == "my_tool"
        assert my_tool.__doc__ == "My docstring."

    def test_assess_graph_recovery(self, adapter: Any) -> None:
        adapter.start_run(goal="Recovery test", run_id="run_lg_6")

        state = SemanticState(
            run_id="run_lg_6",
            goal=Goal(description="Recovery test"),
        )
        env = capture("run_lg_6", StaticProvider(service="v1"))
        adapter.capture_state("run_lg_6", state, environment=env)

        decision = adapter.assess_graph_recovery("run_lg_6", current_environment=env)
        assert decision.mode is RecoveryMode.RESUME
        assert decision.safe

    def test_wrap_tool_uncertain_side_effect_blocks_resume(self, adapter: Any) -> None:
        """A tool that times out leaves the action uncertain, blocking resume."""
        adapter.start_run(goal="Uncertain", run_id="run_lg_7")

        state = SemanticState(
            run_id="run_lg_7",
            goal=Goal(description="Uncertain"),
        )
        env = capture("run_lg_7", StaticProvider(gateway="v1"))
        adapter.capture_state("run_lg_7", state, environment=env)

        @adapter.wrap_tool("payment.charge")
        def charge(continuum_run_id: str = "") -> dict:
            raise TimeoutError("gateway timeout")

        with pytest.raises(TimeoutError):
            charge(continuum_run_id="run_lg_7")

        decision = adapter.assess_graph_recovery("run_lg_7", current_environment=env)
        assert not decision.safe
        assert decision.mode is not RecoveryMode.RESUME


# --------------------------------------------------------------------------- #
# Tests that require langgraph to actually be installed
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not langgraph_available,
    reason="langgraph not installed",
)
class TestWithRealLangGraph:
    """Integration tests that require langgraph."""

    def test_adapter_with_real_langgraph(self, store: SQLiteStorage) -> None:
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)
        assert isinstance(adapter, GenericAgentAdapter)

    def test_wrap_tool_with_real_langgraph(self, store: SQLiteStorage) -> None:
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)
        adapter.start_run(goal="Real LG", run_id="run_real_1")

        calls = 0

        @adapter.wrap_tool("send_email")
        def send_email(to: str, continuum_run_id: str = "") -> str:
            nonlocal calls
            calls += 1
            return f"sent to {to}"

        result = send_email(to="user@example.com", continuum_run_id="run_real_1")
        assert result == "sent to user@example.com"
        assert calls == 1

        result2 = send_email(to="user@example.com", continuum_run_id="run_real_1")
        assert result2 == "sent to user@example.com"
        assert calls == 1

    def test_wrap_tool_explicit_key_deduplicates_across_drift(self, store: SQLiteStorage) -> None:
        """An explicit key collapses repeated calls even when the argument text
        drifts between invocations (the LLM argument-drift failure mode)."""
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)
        adapter.start_run(goal="Real LG key", run_id="run_real_key")
        calls = 0

        @adapter.wrap_tool("notify.customer", key="notify:O-9")
        def notify(order_id: str, continuum_run_id: str = "") -> str:
            nonlocal calls
            calls += 1
            return f"notified {order_id}"

        notify(order_id="O-9", continuum_run_id="run_real_key")
        # Drifted argument text, same explicit key: must not re-fire.
        notify(
            order_id="Your order O-9 has been processed successfully.",
            continuum_run_id="run_real_key",
        )
        assert calls == 1

    def test_wrap_tool_key_fn_derives_key_from_args(self, store: SQLiteStorage) -> None:
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)
        adapter.start_run(goal="Real LG keyfn", run_id="run_real_keyfn")
        calls = 0

        @adapter.wrap_tool(
            "notify.customer",
            key_fn=lambda *a, **k: f"notify:{str(k.get('order_id', '')).strip()}",
        )
        def notify(order_id: str, continuum_run_id: str = "") -> str:
            nonlocal calls
            calls += 1
            return f"notified {order_id}"

        notify(order_id="O-9", continuum_run_id="run_real_keyfn")
        notify(order_id="O-9 ", continuum_run_id="run_real_keyfn")
        assert calls == 1

    def test_wrap_tool_rejects_both_key_and_key_fn(self, store: SQLiteStorage) -> None:
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)

        with pytest.raises(ValueError):

            @adapter.wrap_tool("x", key="k", key_fn=lambda: "k")
            def tool() -> None: ...
