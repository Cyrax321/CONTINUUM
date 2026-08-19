"""Tests for the OpenAI Agents SDK adapter.

Tests verify the adapter without requiring openai-agents to be installed,
using mocks where necessary. When the SDK IS available, integration-style
tests exercise the actual tool wrapping and hooks.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from continuum.adapters import GenericAgentAdapter
from continuum.adapters.openai import openai_agents_available
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
# Tests that work WITHOUT openai-agents installed
# --------------------------------------------------------------------------- #


class TestOpenAIImport:
    def test_module_imports_without_openai_agents(self) -> None:
        """The adapter module should import even without openai-agents."""
        import continuum.adapters.openai as oa

        assert hasattr(oa, "OpenAIAgentAdapter")
        assert hasattr(oa, "openai_agents_available")
        assert hasattr(oa, "ContinuumContext")

    def test_openai_agents_available_flag_is_bool(self) -> None:
        import continuum.adapters.openai as oa

        assert isinstance(oa.openai_agents_available, bool)


class TestEnsureOpenAIAgents:
    def test_ensure_raises_when_not_installed(self) -> None:
        """_ensure_openai_agents raises ImportError with helpful message."""
        import continuum.adapters.openai as oa

        if oa.openai_agents_available:
            pytest.skip("openai-agents is installed; cannot test missing-dep path")

        with pytest.raises(ImportError, match="pip install continuum-agent"):
            oa._ensure_openai_agents()


class TestContinuumContext:
    def test_to_semantic_state_minimal(self) -> None:
        from continuum.adapters.openai import ContinuumContext

        ctx = ContinuumContext(continuum_run_id="run_1")
        state = ctx.to_semantic_state()
        assert state.run_id == "run_1"
        assert state.goal.description == ""

    def test_to_semantic_state_with_goal(self) -> None:
        from continuum.adapters.openai import ContinuumContext

        ctx = ContinuumContext(continuum_run_id="run_2", goal="Process data")
        state = ctx.to_semantic_state()
        assert state.goal.description == "Process data"
        assert state.progress.completed == 0

    def test_to_semantic_state_with_metadata(self) -> None:
        from continuum.adapters.openai import ContinuumContext

        ctx = ContinuumContext(
            continuum_run_id="run_3",
            metadata={"completed_count": 50},
        )
        state = ctx.to_semantic_state()
        assert state.run_id == "run_3"


class TestExtractRunId:
    def test_extract_from_continuum_context(self) -> None:
        from continuum.adapters.openai import ContinuumContext, _extract_run_id

        ctx = ContinuumContext(continuum_run_id="run_direct")
        result = _extract_run_id(ctx)
        assert result == "run_direct"

    def test_extract_from_wrapper_with_context(self) -> None:
        from continuum.adapters.openai import ContinuumContext, _extract_run_id

        inner = ContinuumContext(continuum_run_id="run_wrapped")
        wrapper = MagicMock()
        wrapper.context = inner
        result = _extract_run_id(wrapper)
        assert result == "run_wrapped"

    def test_extract_none_from_empty_wrapper(self) -> None:
        from continuum.adapters.openai import _extract_run_id

        wrapper = MagicMock()
        wrapper.context = "not_a_continuum_context"
        result = _extract_run_id(wrapper)
        assert result is None

    def test_extract_none_from_none(self) -> None:
        from continuum.adapters.openai import _extract_run_id

        result = _extract_run_id(None)
        assert result is None


class TestExtractRunIdFromToolContext:
    def test_from_tool_input_dict(self) -> None:
        from continuum.adapters.openai import _extract_run_id_from_tool_context

        ctx = MagicMock()
        ctx.tool_input = {"continuum_run_id": "run_from_input", "x": 42}
        result = _extract_run_id_from_tool_context(ctx)
        assert result == "run_from_input"

    def test_falls_back_to_agent_context(self) -> None:
        from continuum.adapters.openai import ContinuumContext, _extract_run_id_from_tool_context

        inner_ctx = ContinuumContext(continuum_run_id="run_fallback")
        ctx = MagicMock()
        ctx.tool_input = None
        ctx.context = inner_ctx
        result = _extract_run_id_from_tool_context(ctx)
        assert result == "run_fallback"


class TestAdapterConstruction:
    def test_construction_raises_without_openai_agents(self, store: SQLiteStorage) -> None:
        """OpenAIAgentAdapter constructor requires openai-agents."""
        import continuum.adapters.openai as oa

        if oa.openai_agents_available:
            pytest.skip("openai-agents is installed")

        with pytest.raises(ImportError, match="pip install continuum-agent"):
            oa.OpenAIAgentAdapter(store)


# --------------------------------------------------------------------------- #
# Tests that mock openai-agents so we can exercise the adapter logic
# --------------------------------------------------------------------------- #


class TestWithMockedOpenAIAgents:
    """Tests that mock openai-agents availability to exercise adapter logic."""

    @pytest.fixture
    def adapter(self, store: SQLiteStorage) -> Any:
        """Create an OpenAIAgentAdapter with openai-agents mocked as available."""
        import sys
        import types

        import continuum.adapters.openai as oa

        # Mock the agents module so `from agents import RunHooks` works
        mock_agents = types.ModuleType("agents")
        mock_run_hooks = type("RunHooks", (), {})
        mock_agents.RunHooks = mock_run_hooks
        sys.modules["agents"] = mock_agents

        original = oa.openai_agents_available
        oa.openai_agents_available = True
        try:
            adapter = oa.OpenAIAgentAdapter(store)
            yield adapter
        finally:
            oa.openai_agents_available = original
            sys.modules.pop("agents", None)

    def test_isinstance_generic_adapter(self, adapter: Any) -> None:
        assert isinstance(adapter, GenericAgentAdapter)

    def test_ensure_run_exists_creates_missing_run(
        self, adapter: Any, store: SQLiteStorage
    ) -> None:
        """Regression test for issue #21.

        A fresh OpenAI agent run must be auto-provisioned. ``get_run`` raises
        ``RunNotFound`` for an absent run (it does not return ``None``), so the
        original code never reached its ``create_run`` branch and the first
        contact with a new run failed.
        """
        import types

        from continuum.models import RunStatus
        from continuum.storage import RunNotFound

        with pytest.raises(RunNotFound):
            store.get_run("run_fresh_oa")

        adapter._ensure_run_exists("run_fresh_oa", types.SimpleNamespace(name="my-agent"))

        run = store.get_run("run_fresh_oa")
        assert run.run_id == "run_fresh_oa"
        assert run.status == RunStatus.STARTED

    def test_ensure_run_exists_is_idempotent(self, adapter: Any, store: SQLiteStorage) -> None:
        """An already-existing run is left untouched, with no duplicate create."""
        import types

        from continuum.models import Run

        store.create_run(Run(run_id="run_existing_oa", goal="preexisting"))
        adapter._ensure_run_exists("run_existing_oa", types.SimpleNamespace(name="my-agent"))
        assert store.get_run("run_existing_oa").goal == "preexisting"

    def test_start_run_and_capture_restore(self, adapter: Any) -> None:
        adapter.start_run(goal="OpenAI task", run_id="run_oa_1")

        state = SemanticState(
            run_id="run_oa_1",
            goal=Goal(description="OpenAI task"),
            progress=Progress(total=10, completed=3),
        )
        adapter.capture_state("run_oa_1", state, reason="test checkpoint")

        restored = adapter.restore_state("run_oa_1")
        assert restored.run_id == "run_oa_1"
        assert restored.progress.completed == 3

    def test_create_semantic_state_default(self, adapter: Any) -> None:
        from continuum.adapters.openai import ContinuumContext

        ctx = ContinuumContext(continuum_run_id="run_oa_2", goal="Custom goal")
        state = adapter.create_semantic_state(ctx)
        assert state.run_id == "run_oa_2"
        assert state.goal.description == "Custom goal"

    def test_create_semantic_state_custom_extractor(self, store: SQLiteStorage) -> None:
        import continuum.adapters.openai as oa

        original = oa.openai_agents_available
        oa.openai_agents_available = True
        try:

            def custom_extract(ctx: oa.ContinuumContext) -> SemanticState:
                return SemanticState(
                    run_id=ctx.continuum_run_id,
                    goal=Goal(description=f"Custom: {ctx.goal}"),
                    progress=Progress(completed=99),
                )

            adapter = oa.OpenAIAgentAdapter(store, state_to_semantic=custom_extract)
            ctx = oa.ContinuumContext(continuum_run_id="custom_run", goal="test")
            state = adapter.create_semantic_state(ctx)
            assert state.progress.completed == 99
        finally:
            oa.openai_agents_available = original

    def test_assess_agent_recovery(self, adapter: Any) -> None:
        adapter.start_run(goal="Recovery test", run_id="run_oa_3")

        state = SemanticState(
            run_id="run_oa_3",
            goal=Goal(description="Recovery test"),
        )
        env = capture("run_oa_3", StaticProvider(service="v1"))
        adapter.capture_state("run_oa_3", state, environment=env)

        decision = adapter.assess_agent_recovery("run_oa_3", current_environment=env)
        assert decision.mode is RecoveryMode.RESUME
        assert decision.safe

    def test_intercept_action_deduplicates(self, adapter: Any) -> None:
        adapter.start_run(goal="Action test", run_id="run_oa_4")

        call_count = 0

        def external_call() -> dict:
            nonlocal call_count
            call_count += 1
            return {"tx_id": "abc123"}

        r1 = adapter.intercept_action(
            "run_oa_4",
            "stripe.charge",
            external_call,
            arguments={"amount": 500},
        )
        assert call_count == 1

        r2 = adapter.intercept_action(
            "run_oa_4",
            "stripe.charge",
            external_call,
            arguments={"amount": 500},
        )
        assert call_count == 1
        assert r1 == r2

    def test_create_run_hooks_returns_instance(self, adapter: Any) -> None:
        hooks = adapter.create_run_hooks()
        assert hooks is not None
        assert hasattr(hooks, "on_agent_start")
        assert hasattr(hooks, "on_agent_end")
        assert hasattr(hooks, "on_tool_start")
        assert hasattr(hooks, "on_tool_end")

    def test_create_run_hooks_with_options(self, adapter: Any) -> None:
        tool_start_calls = []
        tool_end_calls = []

        hooks = adapter.create_run_hooks(
            checkpoint_on_agent_end=False,
            checkpoint_on_tool_end=True,
            on_tool_start_fn=lambda ctx, tool: tool_start_calls.append(tool),
            on_tool_end_fn=lambda ctx, tool, result: tool_end_calls.append((tool, result)),
        )

        assert hooks is not None


# --------------------------------------------------------------------------- #
# Tests that require openai-agents to actually be installed
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not openai_agents_available,
    reason="openai-agents not installed",
)
class TestWithRealOpenAIAgents:
    """Integration tests that require openai-agents."""

    def test_adapter_with_real_openai_agents(self, store: SQLiteStorage) -> None:
        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)
        assert isinstance(adapter, GenericAgentAdapter)

    def test_wrap_function_tool_creates_function_tool(self, store: SQLiteStorage) -> None:
        from agents import FunctionTool

        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)
        adapter.start_run(goal="Tool test", run_id="run_real_oa_1")

        @adapter.wrap_function_tool("external.api_call")
        def api_call(ctx: Any, endpoint: str) -> dict:
            return {"endpoint": endpoint, "status": "ok"}

        assert isinstance(api_call, FunctionTool)

    def test_wrap_function_tool_preserves_parameters(self, store: SQLiteStorage) -> None:
        """The wrapped tool should expose the original function's parameters."""
        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)

        @adapter.wrap_function_tool("api.call", name_override="my_api_call")
        def api_call(ctx: Any, endpoint: str, method: str = "GET") -> dict:
            return {"endpoint": endpoint, "method": method}

        schema = api_call.params_json_schema
        assert "endpoint" in schema["properties"]
        assert "method" in schema["properties"]
        assert api_call.name == "my_api_call"
        assert api_call.description == ""

    def test_wrap_function_tool_keeps_param_types_in_schema(self, store: SQLiteStorage) -> None:
        """The generated wrapper must preserve real type annotations, not ``Any``.

        Typing every parameter as ``Any`` (the old behaviour) produced a tool JSON
        schema with no ``type`` key, which strict schema validators such as
        OpenRouter reject. The SDK derives the schema from the first-class type
        hints, so they must survive into the wrapped function.
        """
        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)

        @adapter.wrap_function_tool("api.call")
        def api_call(ctx: Any, endpoint: str, limit: int = 10) -> dict:
            return {}

        props = api_call.params_json_schema["properties"]
        assert props["endpoint"]["type"] == "string"
        assert props["limit"]["type"] == "integer"

    def test_create_run_hooks_with_real_sdk(self, store: SQLiteStorage) -> None:
        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)
        hooks = adapter.create_run_hooks()
        assert hooks is not None
        assert hasattr(hooks, "on_agent_start")
        assert hasattr(hooks, "on_agent_end")
        assert hasattr(hooks, "on_tool_start")
        assert hasattr(hooks, "on_tool_end")

    def test_continuum_context_round_trip(self, store: SQLiteStorage) -> None:
        from continuum.adapters.openai import ContinuumContext, OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)
        ctx = ContinuumContext(
            continuum_run_id="run_ctx_1",
            goal="Test round trip",
            metadata={"completed_count": 5},
        )
        state = adapter.create_semantic_state(ctx)
        assert state.run_id == "run_ctx_1"
        assert state.goal.description == "Test round trip"

    def test_wrap_function_tool_explicit_key_deduplicates_across_drift(
        self, store: SQLiteStorage, monkeypatch: Any
    ) -> None:
        """An explicit key collapses repeated calls even when the LLM renders the
        same operation with different argument text between calls.

        The live OpenAI Agents SDK `FunctionTool` invocation path is version
        sensitive, so we stub `function_tool` to return the generated wrapper and
        drive `intercept_action` directly. This tests the forwarding behaviour
        (the relevant change) without depending on SDK internals.
        """
        import agents

        from continuum.adapters.openai import ContinuumContext, OpenAIAgentAdapter

        captured: list[str | None] = []

        def fake_function_tool(
            name_override: str | None = None, description_override: str | None = None
        ):  # noqa: ANN001
            def deco(fn: Any) -> Any:
                def wrapper(ctx: Any, endpoint: Any = None, **kw: Any) -> Any:
                    return fn(ctx, endpoint=endpoint, **kw)

                wrapper.__name__ = getattr(fn, "__name__", "wrapper")
                return wrapper

            return deco

        monkeypatch.setattr(agents, "function_tool", fake_function_tool)

        adapter = OpenAIAgentAdapter(store)
        run_id = "run_oa_key"
        adapter.start_run(goal="Tool key test", run_id=run_id)

        def recorder(
            run_id: str,
            action_type: str,
            action_fn: Any,
            *,
            arguments: Any = None,  # noqa: ANN001
            volatile: Any = (),
            scoped_to_run: bool = True,
            key: str | None = None,
            **kw: Any,
        ) -> Any:
            captured.append(key)
            return action_fn()

        adapter.intercept_action = recorder  # type: ignore[assignment]

        @adapter.wrap_function_tool("api.call", key="api:endpoint")
        def api_call(ctx: Any, endpoint: str) -> dict:
            return {"endpoint": endpoint}

        ctx = ContinuumContext(continuum_run_id=run_id, goal="k")
        api_call(ctx=ctx, endpoint="A")
        # Drifted argument text, same explicit key: still one ledger claim.
        api_call(ctx=ctx, endpoint="A totally different string")
        assert captured == ["api:endpoint", "api:endpoint"]

    def test_wrap_function_tool_key_fn_derives_key_from_args(
        self, store: SQLiteStorage, monkeypatch: Any
    ) -> None:
        import agents

        from continuum.adapters.openai import ContinuumContext, OpenAIAgentAdapter

        captured: list[str | None] = []

        def fake_function_tool(
            name_override: str | None = None, description_override: str | None = None
        ):  # noqa: ANN001
            def deco(fn: Any) -> Any:
                def wrapper(ctx: Any, endpoint: Any = None, **kw: Any) -> Any:
                    return fn(ctx, endpoint=endpoint, **kw)

                wrapper.__name__ = getattr(fn, "__name__", "wrapper")
                return wrapper

            return deco

        monkeypatch.setattr(agents, "function_tool", fake_function_tool)

        adapter = OpenAIAgentAdapter(store)
        run_id = "run_oa_keyfn"
        adapter.start_run(goal="Tool keyfn test", run_id=run_id)

        def recorder(
            run_id: str,
            action_type: str,
            action_fn: Any,
            *,
            arguments: Any = None,  # noqa: ANN001
            volatile: Any = (),
            scoped_to_run: bool = True,
            key: str | None = None,
            **kw: Any,
        ) -> Any:
            captured.append(key)
            return action_fn()

        adapter.intercept_action = recorder  # type: ignore[assignment]

        @adapter.wrap_function_tool(
            "api.call",
            key_fn=lambda ctx, endpoint: f"api:{str(endpoint).strip()}",
        )
        def api_call(ctx: Any, endpoint: str) -> dict:
            return {"endpoint": endpoint}

        ctx = ContinuumContext(continuum_run_id=run_id, goal="k")
        api_call(ctx=ctx, endpoint="O-9")
        api_call(ctx=ctx, endpoint="O-9 ")
        assert captured == ["api:O-9", "api:O-9"]

    def test_wrap_function_tool_rejects_both_key_and_key_fn(self, store: SQLiteStorage) -> None:
        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)

        with pytest.raises(ValueError):

            @adapter.wrap_function_tool("api.call", key="x", key_fn=lambda ctx: "y")
            def api_call(ctx: Any, endpoint: str) -> dict:
                return {}


def test_wrap_function_tool_invocation_binds_args_and_intercepts(
    store: SQLiteStorage,
) -> None:
    """Regression test for issue #37.

    The OpenAI Agents SDK decides whether a tool takes context by inspecting
    ``__signature__``. If the wrapper's signature drops ``ctx``, the SDK invokes
    the tool with ``takes_context=False``, consuming the first real argument as
    ``ctx`` and silently bypassing CONTINUUM's idempotency interception.

    This drives the real ``on_invoke_tool`` path (no ``function_tool`` stub) so
    the SDK actually performs argument binding. The tool must receive a genuine
    ``ToolContext`` and correctly bound arguments, and a repeated identical call
    must be deduplicated by the ledger.
    """
    from agents.tool_context import ToolContext

    from continuum.actions.ledger import ActionLedger
    from continuum.adapters.openai import (
        ContinuumContext,
        OpenAIAgentAdapter,
        openai_agents_available,
    )

    if not openai_agents_available:
        pytest.skip("openai-agents not installed")

    run_id = "run_oa_37"
    adapter = OpenAIAgentAdapter(store)
    adapter.start_run(goal="issue 37", run_id=run_id)
    ledger = ActionLedger(store, run_id=run_id)

    seen_ctx: list[object] = []

    @adapter.wrap_function_tool("external.api_call")
    def api_call(ctx: Any, endpoint: str, method: str = "GET") -> dict:
        seen_ctx.append(ctx)
        return {"endpoint": endpoint, "method": method}

    class FakeTC(ToolContext):
        def __init__(self) -> None:
            self.tool_name = "api_call"
            self.context = ContinuumContext(continuum_run_id=run_id, goal="g")
            self.tool_input = {"continuum_run_id": run_id}

    payload = '{"endpoint": "https://x", "method": "POST"}'
    first = asyncio.run(api_call.on_invoke_tool(FakeTC(), payload))
    second = asyncio.run(api_call.on_invoke_tool(FakeTC(), payload))

    assert first == {"endpoint": "https://x", "method": "POST"}
    assert second == {"endpoint": "https://x", "method": "POST"}
    # ctx must be a real ToolContext, never the endpoint string (the #37 failure).
    assert all(isinstance(c, ToolContext) for c in seen_ctx)
    # Two identical calls must deduplicate to a single ledger entry.
    assert len(ledger.all()) == 1
