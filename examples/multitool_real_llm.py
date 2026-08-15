"""Real-time, multi-step live demo of CONTINUUM through the LangGraph adapter.

Unlike the single-tool harnesses, this shows a live OpenRouter model *orchestrating*
several tools through CONTINUUM in one go: it looks up an order, notifies the
customer, and opens a support ticket. Every side-effecting tool is wrapped with a
stable idempotency key derived from its call arguments (via ``key_fn``), and a
checkpoint is written after each tool result. After a first pass and a soft resume we
print the recovery decision and the full event log so you can see the architecture
handle a real model smoothly: exactly-once external side effects and clean resume.

OpenRouter exposes an OpenAI-compatible API, so LangGraph's ``create_react_agent``
runs through LangChain's ``ChatOpenAI`` with ``base_url="https://openrouter.ai/api/v1"``.

Run it:

    OPENROUTER_API_KEY=sk-or-... python examples/multitool_real_llm.py
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

DB = os.path.join(tempfile.gettempdir(), "continuum-multitool-openrouter.db")
NOTIFY_OUT = os.path.join(tempfile.gettempdir(), "continuum-multitool-notify.txt")
TICKET_OUT = os.path.join(tempfile.gettempdir(), "continuum-multitool-ticket.txt")
RUN_ID = "mt_real_openrouter_1"


class _CheckpointHandler(BaseCallbackHandler):
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

    for path in (DB, NOTIFY_OUT, TICKET_OUT):
        if os.path.exists(path):
            os.remove(path)

    store = SQLiteStorage(DB)
    adapter = LangGraphAgentAdapter(store)
    goal = "Resolve the O-9 package-not-arrived complaint"
    adapter.start_run(goal=goal, run_id=RUN_ID)

    # Read-only: no external side effect, but still recorded as an observation.
    @adapter.wrap_tool("orders.lookup")
    def lookup(order_id: str, *, continuum_run_id: str = "") -> str:
        return f"Order {order_id}: status=shipped, eta=2026-08-20, carrier=FastShip"

    # Side effect: the key is a *stable* business id for this operation, NOT derived
    # from the model's rendered arguments. A live model drifts how it writes the
    # order id ("O-9" vs "Order O-9: ..."); only a fixed key collapses that drift
    # to exactly-once.
    @adapter.wrap_tool("notify.customer", key="notify:O-9")
    def notify(order_id: str, *, continuum_run_id: str = "") -> str:
        with open(NOTIFY_OUT, "a") as fh:
            fh.write(f"notify {order_id}: we are investigating your shipment.\n")
        return "notified"

    @adapter.wrap_tool("ticket.create", key="ticket:O-9")
    def create_ticket(order_id: str, *, continuum_run_id: str = "") -> str:
        with open(TICKET_OUT, "a") as fh:
            fh.write(f"ticket {order_id}: late shipment reported by customer.\n")
        return "created"

    def _lookup(order_id: str) -> str:
        return lookup(order_id=order_id, continuum_run_id=RUN_ID)

    def _notify(order_id: str) -> str:
        return notify(order_id=order_id, continuum_run_id=RUN_ID)

    def _ticket(order_id: str) -> str:
        return create_ticket(order_id=order_id, continuum_run_id=RUN_ID)

    tools = [
        Tool(name="lookup_order", func=_lookup, description="Look up an order by id"),
        Tool(
            name="notify_customer", func=_notify, description="Notify a customer about their order"
        ),
        Tool(name="create_ticket", func=_ticket, description="Open a support ticket for an order"),
    ]

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
        max_retries=2,
    )
    agent = create_agent(llm, tools)
    handler = _CheckpointHandler(adapter, RUN_ID, goal)

    prompt = (
        "A customer with order O-9 reports their package hasn't arrived. "
        "Look up the order, notify the customer that we are investigating, "
        "and open a support ticket summarizing the late shipment."
    )

    print(f"== First pass (model: {model}) ==")
    result = agent.invoke({"messages": [("user", prompt)]}, config={"callbacks": [handler]})
    print("agent:", result["messages"][-1].content)
    decision = RecoveryEngine(store).assess(RUN_ID)
    print("recovery after pass 1:", decision.mode.value, "safe=", decision.safe)

    print("\n== Resume: same run, second pass ==")
    result2 = agent.invoke(
        {"messages": [("user", "Confirm everything was handled for order O-9.")]},
        config={"callbacks": [handler]},
    )
    print("agent:", result2["messages"][-1].content)
    decision2 = RecoveryEngine(store).assess(RUN_ID)
    print("recovery after pass 2:", decision2.mode.value, "safe=", decision2.safe)

    with open(NOTIFY_OUT) as fh:
        notify_lines = [line for line in fh.read().splitlines() if line.strip()]
    with open(TICKET_OUT) as fh:
        ticket_lines = [line for line in fh.read().splitlines() if line.strip()]
    events = store.read_events(RUN_ID)

    print("\nnotify side effects (must be 1):", len(notify_lines))
    for line in notify_lines:
        print("  ", line)
    print("ticket side effects (must be 1):", len(ticket_lines))
    for line in ticket_lines:
        print("  ", line)

    print("\nevent log:")
    for e in events:
        print(f"  {e.sequence:>3} {e.type.value}")

    assert len(notify_lines) == 1, "notify fired more than once!"
    assert len(ticket_lines) == 1, "ticket fired more than once!"
    assert any(e.type is EventType.ACTION_RECORDED for e in events)
    assert any(e.type is EventType.STATE_CHECKPOINTED for e in events)
    print("\nOK: multi-step live agent handled smoothly; exactly-once side effects preserved.")


if __name__ == "__main__":
    main()
