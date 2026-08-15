"""Real-LLM harness for the CONTINUUM OpenAI Agents SDK adapter via OpenRouter.

OpenRouter exposes an OpenAI-compatible API. The OpenAI Agents SDK normally
talks to the Responses API, but it also ships ``OpenAIChatCompletionsModel``,
which uses the Chat Completions endpoint that OpenRouter fully supports. We point
an ``AsyncOpenAI`` client at ``https://openrouter.ai/api/v1`` and wrap the model
in that class, so a real model drives the agent through CONTINUUM's adapter.

The notify tool is wrapped with an explicit ``key`` so that exactly-once holds
even though a live model may render the same operation with different argument
text between calls. We run the agent twice over the same run (a first pass plus a
resume) and assert the external side effect fires exactly once.

Run it:

    OPENROUTER_API_KEY=sk-or-... \\
    OPENROUTER_MODEL=openai/gpt-4o-mini \\
    python examples/openai_real_llm.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from agents import Agent, Runner
from openai import AsyncOpenAI

from continuum.adapters.openai import ContinuumContext, OpenAIAgentAdapter
from continuum.events import EventType
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


async def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

    db_path = os.path.join(tempfile.gettempdir(), "continuum-openai-openrouter.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    store = SQLiteStorage(db_path)
    adapter = OpenAIAgentAdapter(store)

    run_id = "oa_real_openrouter_1"
    goal = "Notify the customer about their shipped order O-9"
    adapter.start_run(goal=goal, run_id=run_id)

    side_effects = {"notify": 0}

    @adapter.wrap_function_tool("notify.customer", key="notify:O-9")
    def notify(ctx: object, order_id: str) -> str:
        side_effects["notify"] += 1
        return f"notified {order_id}"

    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

    chat_model = OpenAIChatCompletionsModel(model=model, openai_client=client)

    agent = Agent(
        name="notifier",
        instructions="Use the notify tool to notify the customer about their order O-9.",
        tools=[notify],
        model=chat_model,
    )
    ctx = ContinuumContext(continuum_run_id=run_id, goal=goal)
    hooks = adapter.create_run_hooks()

    print(f"== First invocation (model: {model}) ==")
    result = await Runner.run(
        starting_agent=agent,
        input="Please notify the customer about order O-9.",
        context=ctx,
        hooks=hooks,
    )
    print("agent:", result.final_output)
    decision = RecoveryEngine(store).assess(run_id)
    print("recovery after run 1:", decision.mode.value, "safe=", decision.safe)
    print("external side effects so far:", side_effects["notify"])

    print("\n== Resume: same run, second invocation ==")
    result2 = await Runner.run(
        starting_agent=agent,
        input="Make sure the customer for order O-9 was notified.",
        context=ctx,
        hooks=hooks,
    )
    print("agent:", result2.final_output)
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
    asyncio.run(main())
