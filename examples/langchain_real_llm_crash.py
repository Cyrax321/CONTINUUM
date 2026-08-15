"""Real-LLM crash-and-resume harness for the CONTINUUM LangChain adapter.

The other real-LLM harnesses prove exactly-once across a *soft* resume (a second
agent invocation). This one proves the harder contract: a hard crash between
``intercept_action`` (claim) and ``complete`` leaves the side effect uncertain, and
CONTINUUM refuses to resume until it is reconciled, instead of letting the agent
silently re-fire it.

It drives a live OpenRouter model. The agent is asked to notify the customer; the
wrapped tool performs the real side effect (appends to an outbox file) and, when
run in ``crash`` mode, immediately ``SIGKILL``s the process before the ledger
records completion. A second process (``resume`` mode) opens the same database and
asks CONTINUUM to assess the run.

Expected outcome: ``mode=request_human``, ``safe=False``, one uncertain action, and
the outbox still containing exactly one line (the effect that happened, never
duplicated).

Usage:

    OPENROUTER_API_KEY=sk-or-... python examples/langchain_real_llm_crash.py crash
    OPENROUTER_API_KEY=sk-or-... python examples/langchain_real_llm_crash.py resume
"""

from __future__ import annotations

import os
import sys
import tempfile

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import Tool
from langchain_openai import ChatOpenAI

from continuum.adapters.langchain import LangChainAgentAdapter
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage

DB = os.path.join(tempfile.gettempdir(), "continuum-crash-openrouter.db")
OUTBOX = os.path.join(tempfile.gettempdir(), "continuum-crash-openrouter-outbox.txt")
RUN_ID = "lc_crash_openrouter_1"
KEY = "notify:O-9"


class _CheckpointHandler(BaseCallbackHandler):
    def __init__(self, adapter: LangChainAgentAdapter, run_id: str, goal: str) -> None:
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


def _build_tool(adapter: LangChainAgentAdapter, run_id: str) -> Tool:
    @adapter.wrap_tool("notify.customer", key=KEY)
    def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
        # Real side effect: append to the outbox. This is what must not happen twice.
        with open(OUTBOX, "a") as fh:
            fh.write(f"notified {order_id}\n")
        # Simulate a hard crash mid-side-effect: the process dies before the ledger
        # records completion, so the action is left uncertain. os._exit is the
        # canonical hard kill (no cleanup, no signal handler) so the crash is
        # deterministic regardless of how the agent loop schedules the tool call.
        if os.environ.get("CRASH"):
            os._exit(137)
        return f"notified {order_id}"

    def notify_tool(order_id: str) -> str:
        return _notify(order_id=order_id, continuum_run_id=run_id)

    return Tool(name="notify", func=notify_tool, description="Notify a customer about their order")


def _llm() -> ChatOpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
        max_retries=2,
    )


def crash_mode() -> None:
    for path in (DB, OUTBOX):
        if os.path.exists(path):
            os.remove(path)

    store = SQLiteStorage(DB)
    adapter = LangChainAgentAdapter(store)
    adapter.start_run(goal="Notify the customer about order O-9", run_id=RUN_ID)

    tool = _build_tool(adapter, RUN_ID)
    agent = create_agent(_llm(), [tool])
    handler = _CheckpointHandler(adapter, RUN_ID, "Notify the customer about order O-9")

    os.environ["CRASH"] = "1"
    print("== Crash mode: agent will notify, then the process is SIGKILLed mid-side-effect ==")
    agent.invoke(
        {"messages": [("user", "Please notify the customer about order O-9.")]},
        config={"callbacks": [handler]},
    )
    # The process is killed inside the tool, so this line should never print.
    print("CRASH mode finished without a kill (unexpected).")


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
        raise SystemExit("usage: langchain_real_llm_crash.py [crash|resume]")
    if sys.argv[1] == "crash":
        crash_mode()
    else:
        resume_mode()


if __name__ == "__main__":
    main()
