"""Real-LLM harness for the CONTINUUM LangGraph adapter via OpenRouter.

OpenRouter exposes an OpenAI-compatible API, so LangGraph's ``create_react_agent``
runs against it through LangChain's ``ChatOpenAI`` with
``base_url="https://openrouter.ai/api/v1"``. This drives the LangGraph adapter with
a genuine tool-calling model instead of the scripted fake used in
``tests/test_integration_langgraph.py``.

The notify tool is wrapped with an explicit ``key`` so exactly-once holds even
though a live model may render the same operation with different argument text
between calls. We run the agent twice over the same run (a first pass plus a
resume) and assert the external side effect fires exactly once.

Run it:

    OPENROUTER_API_KEY=sk-or-... \\
    OPENROUTER_MODEL=openai/gpt-4o-mini \\
    python examples/langgraph_real_llm.py
"""

from __future__ import annotations

import os
import tempfile

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import Tool
from langchain_openai import ChatOpenAI

from continuum.adapters.langgraph import LangGraphAgentAdapter
from continuum.events import EventType
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


class _CheckpointHandler(BaseCallbackHandler):
    """Persists a CONTINUUM checkpoint after every agent tool result."""

    def __init__(self, adapter: LangGraphAgentAdapter, run_id: str, goal: str) -> None:
        self._adapter = adapter
        self._run_id = run_id
        self._goal = goal

    def on_tool_end(self, output: object, **kwargs: object) -> None:
        self._adapter.checkpoint_node(
            {
                "continuum_run_id": self._run_id,
                "goal": self._goal,
                "last_tool": str(output)[:120],
            }
        )


def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    db_path = os.path.join(tempfile.gettempdir(), "continuum-langgraph-openrouter.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    store = SQLiteStorage(db_path)
    adapter = LangGraphAgentAdapter(store)

    run_id = "lg_real_openrouter_1"
    goal = "Notify the customer about their shipped order O-9"
    adapter.start_run(goal=goal, run_id=run_id)

    side_effects = {"notify": 0}

    @adapter.wrap_tool("notify.customer", key="notify:O-9")
    def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
        side_effects["notify"] += 1
        return f"notified {order_id}"

    def notify_tool(order_id: str) -> str:
        return _notify(order_id=order_id, continuum_run_id=run_id)

    tool = Tool(name="notify", func=notify_tool, description="Notify a customer about their order")

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
        max_retries=2,
    )
    agent = create_agent(llm, [tool])
    handler = _CheckpointHandler(adapter, run_id, goal)

    print(f"== First invocation (model: {model}) ==")
    result = agent.invoke(
        {"messages": [("user", "Please notify the customer about order O-9.")]},
        config={"callbacks": [handler]},
    )
    print("agent:", result["messages"][-1].content)
    decision = RecoveryEngine(store).assess(run_id)
    print("recovery after run 1:", decision.mode.value, "safe=", decision.safe)
    print("external side effects so far:", side_effects["notify"])

    print("\n== Resume: same run, second invocation ==")
    result2 = agent.invoke(
        {"messages": [("user", "Make sure the customer for order O-9 was notified.")]},
        config={"callbacks": [handler]},
    )
    print("agent:", result2["messages"][-1].content)
    decision2 = RecoveryEngine(store).assess(run_id)
    print("recovery after run 2:", decision2.mode.value, "safe=", decision2.safe)
    print("external side effects total (must be 1):", side_effects["notify"])

    events = store.read_events(run_id)
    print("\nevent log:")
    for e in events:
        print(f"  {e.sequence:>3} {e.type.value}")

    assert side_effects["notify"] == 1, "side effect fired more than once!"
    assert any(e.type is EventType.STATE_CHECKPOINTED for e in events)
    print("\nOK: exactly-once side effect preserved across resume.")


if __name__ == "__main__":
    main()
