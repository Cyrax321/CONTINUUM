"""End-to-end integration: CONTINUUM architecture on a real LangChain agent.

Uses ``langchain.agents.create_agent`` (the current LangChain agent runtime)
driven offline by a scripted fake chat model, so no API key or network is
required. The agent's tool is wrapped with the LangChain adapter's ``wrap_tool``
(exactly-once side effects) and a checkpoint callback persists CONTINUUM state
on every tool result.

This exercises the architecture against a genuine agent loop: the model issues
tool calls, the framework invokes the wrapped tool, and CONTINUUM guarantees the
external side effect fires once even when the agent calls the tool repeatedly.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import Tool
from pydantic import PrivateAttr

from continuum.adapters.langchain import LangChainAgentAdapter
from continuum.events import EventType
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage
from continuum.storage.base import Storage

warnings.filterwarnings("ignore")

langchain = pytest.importorskip("langchain")


class _ScriptedLLM(BaseChatModel):
    """A deterministic fake chat model that replays a fixed script of messages.

    The script is a list of ``AIMessage`` objects. The first message(s) carry
    ``tool_calls`` to drive a real tool-calling agent loop; the final message is
    the agent's answer. This makes a genuine LangChain agent run offline.
    """

    messages: list = []
    _i: int = PrivateAttr(default=0)
    _tools: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedLLM:
        self._tools = list(tools)
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        msg = self.messages[self._i]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @property
    def _llm_type(self) -> str:
        return "scripted"


class _CheckpointHandler(BaseCallbackHandler):
    """Persists a CONTINUUM checkpoint after every agent tool result."""

    def __init__(self, adapter: LangChainAgentAdapter, run_id: str, goal: str) -> None:
        self._adapter = adapter
        self._run_id = run_id
        self._goal = goal

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self._adapter.checkpoint_node(
            {
                "continuum_run_id": self._run_id,
                "goal": self._goal,
                "last_tool": str(output)[:120],
            }
        )


@pytest.fixture
def store() -> Storage:
    return SQLiteStorage(":memory:")


def _make_tool(adapter: LangChainAgentAdapter, run_id: str, calls: dict) -> Tool:
    @adapter.wrap_tool("notify.customer")
    def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
        calls["notify"] += 1
        return f"notified {order_id}"

    def notify_tool(order_id: str) -> str:
        return _notify(order_id=order_id, continuum_run_id=run_id)

    return Tool(name="notify", func=notify_tool, description="Notify a customer")


def _run_agent(tool: Tool, script: list, handler: _CheckpointHandler) -> None:
    llm = _ScriptedLLM(messages=script)
    agent = create_agent(llm, [tool])
    agent.invoke({"messages": [("user", "notify the customer")]}, config={"callbacks": [handler]})


@pytest.mark.skipif(langchain is None, reason="langchain not installed")
class TestRealLangChainAgent:
    def test_agent_loop_is_exactly_once_and_checkpoints(self, store: Storage) -> None:
        run_id = "lc_agent_real_1"
        adapter = LangChainAgentAdapter(store)
        adapter.start_run(goal="process order", run_id=run_id)

        calls: dict[str, int] = {"notify": 0}
        tool = _make_tool(adapter, run_id, calls)
        handler = _CheckpointHandler(adapter, run_id, "process order")

        # The model calls the tool twice in one run; CONTINUUM must collapse
        # that into a single external side effect.
        script = [
            AIMessage(content="", tool_calls=[{"name": "notify", "args": {"order_id": "O-9"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "notify", "args": {"order_id": "O-9"}, "id": "c2"}]),
            AIMessage(content="All done."),
        ]
        _run_agent(tool, script, handler)

        assert calls["notify"] == 1
        assert any(e.type is EventType.STATE_CHECKPOINTED for e in store.read_events(run_id))
        assert RecoveryEngine(store).assess(run_id).safe is True

    def test_idempotent_across_separate_agent_invocations(self, store: Storage) -> None:
        run_id = "lc_agent_real_2"
        adapter = LangChainAgentAdapter(store)
        adapter.start_run(goal="process order", run_id=run_id)

        calls: dict[str, int] = {"notify": 0}
        tool = _make_tool(adapter, run_id, calls)
        handler = _CheckpointHandler(adapter, run_id, "process order")

        script = [
            AIMessage(content="", tool_calls=[{"name": "notify", "args": {"order_id": "O-9"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "notify", "args": {"order_id": "O-9"}, "id": "c2"}]),
            AIMessage(content="All done."),
        ]
        # Two full agent runs over the same CONTINUUM run. The ledger must keep
        # the side effect at exactly one execution.
        _run_agent(tool, script, handler)
        _run_agent(tool, script, handler)

        assert calls["notify"] == 1
