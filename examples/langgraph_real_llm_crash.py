"""Real-LLM crash-and-resume harness for the CONTINUUM LangGraph adapter.

Companion to ``examples/langchain_real_llm_crash.py``. It drives a live OpenRouter
model through the LangGraph adapter (LangChain's ``create_agent`` over
``ChatOpenAI``) and proves the hard-crash contract: a crash between
``intercept_action`` (claim) and ``complete`` leaves the side effect uncertain, and
CONTINUUM refuses to resume until it is reconciled, rather than letting the agent
silently re-fire it.

Two processes share one SQLite file:

- ``crash`` mode: the agent is asked to notify the customer. The wrapped tool
  performs the real side effect (appends the order id to an outbox file) and then
  hard-exits the process with ``os._exit(137)`` before the ledger records
  completion, leaving an open ``ACTION_RECORDED`` at ``started``.
- ``resume`` mode: a fresh process opens the same database and asks CONTINUUM to
  ``assess`` the run.

Expected outcome: ``safe=False`` with one uncertain action, and the outbox still
containing exactly one line.

Usage:

    OPENROUTER_API_KEY=sk-or-... python examples/langgraph_real_llm_crash.py crash
    OPENROUTER_API_KEY=sk-or-... python examples/langgraph_real_llm_crash.py resume
"""

from __future__ import annotations

import os
import sys
import tempfile

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import Tool
from langchain_openai import ChatOpenAI

from continuum.adapters.langgraph import LangGraphAgentAdapter
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage

DB = os.path.join(tempfile.gettempdir(), "continuum-langgraph-crash-openrouter.db")
OUTBOX = os.path.join(tempfile.gettempdir(), "continuum-langgraph-crash-openrouter-outbox.txt")
RUN_ID = "lg_crash_openrouter_1"
KEY = "notify:O-9"


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


def crash_mode() -> None:
    for path in (DB, OUTBOX):
        if os.path.exists(path):
            os.remove(path)

    store = SQLiteStorage(DB)
    adapter = LangGraphAgentAdapter(store)
    goal = "Notify the customer about their shipped order O-9"
    adapter.start_run(goal=goal, run_id=RUN_ID)

    @adapter.wrap_tool("notify.customer", key=KEY)
    def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
        # Real side effect: append to the outbox. This is what must not happen twice.
        with open(OUTBOX, "a") as fh:
            fh.write(f"notified {order_id}\n")
        # Hard crash mid-side-effect: die before the ledger records completion.
        if os.environ.get("CRASH"):
            os._exit(137)
        return f"notified {order_id}"

    def notify_tool(order_id: str) -> str:
        return _notify(order_id=order_id, continuum_run_id=RUN_ID)

    tool = Tool(name="notify", func=notify_tool, description="Notify a customer about their order")

    llm = ChatOpenAI(
        model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
        max_retries=2,
    )
    agent = create_agent(llm, [tool])
    handler = _CheckpointHandler(adapter, RUN_ID, goal)

    os.environ["CRASH"] = "1"
    print("== Crash mode: agent will notify, then the process hard-exits mid-side-effect ==")
    agent.invoke(
        {"messages": [("user", "Please notify the customer about order O-9.")]},
        config={"callbacks": [handler]},
    )
    print("CRASH mode finished without an exit (unexpected).")


def resume_mode() -> None:
    store = SQLiteStorage(DB)
    decision = RecoveryEngine(store).assess(RUN_ID)

    print("\n== Resume mode: a fresh process assesses the crashed run ==")
    print("mode:", decision.mode.value)
    print("safe:", decision.safe)
    print("next_allowed_action:", decision.next_allowed_action)
    print("uncertain_actions:", len(decision.uncertain_actions))
    for action in decision.uncertain_actions:
        print(f"  - {action.action_type} status={action.status.value}")

    with open(OUTBOX) as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]
    print("outbox entries (must be exactly 1):", len(lines))
    for line in lines:
        print("  ", line)

    print("\nevent log:")
    for e in store.read_events(RUN_ID):
        print(f"  {e.sequence:>3} {e.type.value}")

    assert decision.safe is False, "crashed run must not be safe to resume"
    assert decision.mode.value == "request_human", "uncertain side effect must require a human"
    assert len(decision.uncertain_actions) == 1, "exactly one uncertain action expected"
    assert len(lines) == 1, "side effect must have fired once, never duplicated"
    print("\nOK: crash left an uncertain side effect; resume blocked, outbox not duplicated.")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("crash", "resume"):
        raise SystemExit("usage: langgraph_real_llm_crash.py [crash|resume]")
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY is not set")
    if sys.argv[1] == "crash":
        crash_mode()
    else:
        resume_mode()


if __name__ == "__main__":
    main()
