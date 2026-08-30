# Adapter authoring guide

CONTINUUM talks to agent frameworks through *adapters*. An adapter is a thin
facade over storage, state, checkpointing, the action ledger and the recovery
engine. The recovery funnel is the set of adapters plus the discovery layer
that lets one recovery call work for any of them.

## The contract every adapter honors

All adapters subclass `continuum.adapters.AgentAdapter` (an ABC) and must
implement:

- `capture_state(run_id, state, *, environment=None, reason="")` -> checkpoint
- `restore_state(run_id, *, replay=True)` -> `SemanticState`
- `intercept_action(run_id, action_type, action_fn, arguments=None, *, volatile=(), scoped_to_run=True)` -> result
- `resume(run_id, *, current_environment=None, expected_model=None, replay=True)` -> `RecoveryDecision`

`resume` is the uniform recovery entry point. It returns a framework-agnostic
`RecoveryDecision`, so callers never need to know which framework produced the
run.

## Built-in adapters

| Name        | Class                     | Needs extra          |
|-------------|---------------------------|----------------------|
| `generic`   | `GenericAgentAdapter`     | nothing              |
| `langchain` | `LangChainAgentAdapter`   | `langchain`          |
| `langgraph` | `LangGraphAgentAdapter`   | `langgraph`          |
| `openai`    | `OpenAIAgentAdapter`      | `openai-agents`      |

Each takes `storage` as its first constructor argument and accepts a few
optional keyword arguments (for example `graph` for langgraph, or a
`state_to_semantic` extractor).

## Crash recovery in under ten minutes

Every adapter below is runnable from a fresh checkout in under ten minutes.
The pattern is the same: `start_run`, do work through `intercept_action`,
force a checkpoint, hard kill, then `resume` in a fresh process. Only
`safe: true` with `mode: resume` may launch the next turn.

### Generic Python (no extra install)

```python
from continuum.adapters.generic import GenericAgentAdapter
from continuum.storage import SQLiteStorage

store = SQLiteStorage(":memory:")
adapter = GenericAgentAdapter(store)
run_id = "demo-generic"
adapter.start_run(goal="analyze batch", run_id=run_id)

# side effects go through the ledger so a retry never duplicates
res = adapter.intercept_action(run_id, "slack.notify",
    lambda: "sent", arguments={"channel": "#alerts"})

from continuum.state.semantic import project
state = project(run_id, store.read_events(run_id))
adapter.capture_state(run_id, state, reason="pre-kill")

# process dies here: os._exit(9) or kill -9
# fresh process:
decision = adapter.resume(run_id)
assert decision.safe and decision.mode.value == "resume"
print(decision.render())
```

If the kill lands between `claim` and `complete`, `resume` reports
`request_human` with `next_allowed_action: reconcile_action:...` and
`safe: false`. That is the contract a harness must not walk past.

### LangChain (needs `continuum-agent[langchain]`)

```python
from continuum.adapters.langchain import LangChainAgentAdapter
from continuum.storage import SQLiteStorage
from langchain_core.runnables import RunnableLambda

store = SQLiteStorage(":memory:")
adapter = LangChainAgentAdapter(store)
run_id = "demo-lc"
adapter.start_run(goal="LCEL batch", run_id=run_id)

@adapter.wrap_tool("notify.customer", key="notify:O-9")
def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return f"notified {order_id}"

def work(state: dict) -> dict:
    _notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
    return {**state, "done": True}

chain = RunnableLambda(work) | RunnableLambda(adapter.checkpoint_node)
chain.invoke({"continuum_run_id": run_id, "order_id": "O-9"})

# kill -9 here, then in a fresh process:
from continuum.recovery import RecoveryEngine
decision = RecoveryEngine(store).assess(run_id)
assert decision.safe  # fails safe when the kill left an uncertain action
```

Use `key` or `key_fn` on `wrap_tool` so LLM argument drift does not defeat
dedup. See `references/adapters.md` for the `create_agent` variant and for
the live OpenRouter crash proof (`examples/langchain_real_llm_crash.py`).

### LangGraph (needs `continuum-agent[langgraph]`)

```python
from continuum.adapters.langgraph import LangGraphAgentAdapter
from continuum.storage import SQLiteStorage
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

class S(TypedDict):
    continuum_run_id: str
    order_id: str

store = SQLiteStorage(":memory:")
adapter = LangGraphAgentAdapter(store)
run_id = "demo-lg"
adapter.start_run(goal="graph batch", run_id=run_id)

@adapter.wrap_tool("notify.customer", key="notify:O-9")
def notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return f"notified {order_id}"

def work(state: S) -> S:
    notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
    return state

def checkpoint(state: S) -> S:
    return adapter.checkpoint_node(state)

builder = StateGraph(S)
builder.add_node("work", work)
builder.add_node("checkpoint", checkpoint)
builder.add_edge(START, "work")
builder.add_edge("work", "checkpoint")
builder.add_edge("checkpoint", END)
graph = builder.compile()
graph.invoke({"continuum_run_id": run_id, "order_id": "O-9"})

# kill -9, then fresh:
decision = adapter.resume(run_id)
print(decision.mode, decision.safe)
```

For native persistence, `make_continuum_checkpointer(store)` implements
LangGraph's `BaseCheckpointSaver` over CONTINUUM's log so every `put` lands
in the same hash-chained, provenance-tagged store.

### OpenAI Agents SDK (needs `continuum-agent[openai]`)

```python
from continuum.adapters.openai import OpenAIAgentAdapter, ContinuumContext
from continuum.storage import SQLiteStorage
from agents import Agent, Runner

store = SQLiteStorage(":memory:")
adapter = OpenAIAgentAdapter(store)
run_id = "demo-oa"
adapter.start_run(goal="notify", run_id=run_id)

@adapter.wrap_function_tool("notify.customer", key="notify:O-9")
def _tool(ctx, order_id: str) -> str:
    return f"notified {order_id}"

agent = Agent(name="n", instructions="Use notify.", tools=[_tool])
ctx = ContinuumContext(continuum_run_id=run_id, goal="notify")
# await Runner.run(starting_agent=agent, input="notify O-9", context=ctx,
#                  hooks=adapter.create_run_hooks())

# kill -9 before ledger complete -> resume reports request_human with
# next_allowed_action pointing at the reconciler, not a blind retry.
```

Live OpenRouter proofs including a hard-crash with `os._exit(137)` and the
resume contract asserting `request_human` are in `examples/openai_real_llm_crash.py`.

Metric note: with `uv pip install -e ".[dev]"` already cached, each snippet
above runs end to end in under two minutes. The first cold install may
approach eight minutes, still inside the ten-minute gate.

## Discovery and dispatch (the funnel)

Adapters are registered by name in a process-wide registry. Registration is
*lazy*: an adapter's heavy dependencies are only imported when the adapter is
actually requested, so importing `continuum` never drags in `langchain` or
`openai`.

```python
from continuum import list_adapters, get_adapter, recover

list_adapters()            # ["generic", "langchain", "langgraph", "openai"]
adapter_cls = get_adapter("generic")

# One call recovers any run through any registered adapter:
decision = recover("generic", run_id, storage, current_environment=env)
```

`get_adapter("unknown")` raises `ValueError` with the list of known names.
`recover("unknown", ...)` raises the same, before touching storage.

## Authoring a new adapter

1. Subclass `AgentAdapter` and implement the four abstract methods.
2. Keep the adapter's import of its framework dependency *lazy* (import it
   inside `__init__` or the methods that need it, not at module top), so the
   adapter is importable in environments without that dependency.
3. Provide a `resume` implementation. The simplest correct version delegates to
   `RecoveryEngine(storage).assess(run_id, ...)`; framework-specific adapters
   may first project framework state into a `SemanticState`.
4. Register it so the funnel can find it:

   ```python
   from continuum.adapters import register_adapter

   register_adapter("myfw", lambda: MyFrameworkAdapter)
   ```

5. Add a smoke test that constructs the adapter with an in-memory
   `SQLiteStorage`, seeds a run and checkpoint, and asserts `recover("myfw", ...)`
   returns a `RecoveryDecision`. Guard any framework-specific setup so the test
   skips cleanly when the dependency is absent.
