# Framework Adapters

CONTINUUM plugs into agent frameworks without becoming one. The adapter layer
(`src/continuum/adapters/`) is a thin, optional facade over the core
primitives: storage, the append-only event log, the action ledger,
checkpointing, and the recovery engine. Each adapter records progress and side
effects through the ledger and routes external effects through the two-phase
intercept/complete protocol.

The adapters do not replace a framework's own persistence (LangGraph
checkpointers, LangChain memory, OpenAI session state). They add a second,
semantic layer: exactly-once side effects, durable checkpoints, and a recovery
verdict that is safe to act on.

## Installation

Adapters are optional installs so the core stays standard-library-only.

```bash
pip install "continuum-agent[openai]"     # OpenAI Agents SDK
pip install "continuum-agent[langgraph]"  # LangGraph
pip install "continuum-agent[langchain]"  # LangChain (LCEL + create_agent)
```

The `dev` extra installs all of them so mypy can type-check the adapter modules
and the integration tests can run in CI.

## The adapter base

`GenericAgentAdapter` is the shared facade. The framework adapters extend it:

- `capture_state` / `restore_state` - durable semantic checkpoints.
- `intercept_action` - idempotent external side effects via the action ledger.
- `resume` - recovery assessment (`RESUME`, `REQUEST_HUMAN`, `ABORT`, ...).
- `start_run` - creates the run and records `RUN_STARTED` as the log's first event.

`start_run` records `RUN_STARTED` itself. Without that event, projection,
replay, and restore fail with "the log never recorded RUN_STARTED", and a
non-empty log whose first event is not `RUN_STARTED` is refused rather than
silently misordered.

## Generic Python

The in-process facade for hand-rolled loops. It writes trusted
(`Origin.DETERMINISTIC`) state.

```python
from continuum.adapters import GenericAgentAdapter

adapter = GenericAgentAdapter(store)
adapter.start_run(goal="analyze documents", run_id="r1")
```

## OpenAI Agents SDK

Wraps `function_tool` and `RunHooks`. Importing the adapter requires
`openai-agents` to be installed. State is reported with
`Origin.EXTERNAL_AGENT` provenance (see Provenance below).

## LangGraph

Wraps a `StateGraph`. Add `checkpoint_node` as a graph node and wrap tools with
`wrap_tool`.

```python
from continuum.adapters import LangGraphAgentAdapter
from langgraph.graph import StateGraph, START, END

adapter = LangGraphAgentAdapter(store)
adapter.start_run(goal="process orders", run_id="lg1")


@adapter.wrap_tool("notify.customer")
def notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return send_email(order_id)


def checkpoint(state: dict) -> dict:
    return adapter.checkpoint_node(state)


builder = StateGraph(MyState)
builder.add_node("work", work)
builder.add_node("checkpoint", checkpoint)
builder.add_edge(START, "work")
builder.add_edge("work", "checkpoint")
builder.add_edge("checkpoint", END)
graph = builder.compile()
graph.invoke({"continuum_run_id": "lg1", "order_id": "O-1"})
```

`checkpoint_node` reads `continuum_run_id` from state, projects the current
semantic state from the event log (a fresh run with no events falls back to the
state dict; see issue #46), and writes a checkpoint. `wrap_tool` makes the side
effect idempotent across graph invocations. `assess_graph_recovery(run_id)`
returns a `RecoveryDecision` before re-invoking the graph after a crash.

## LangChain

Drops `checkpoint_node` into an LCEL `RunnableLambda` pipeline, and supports the
`langchain.agents.create_agent` tool-calling loop.

### LCEL pipeline

```python
from continuum.adapters import LangChainAgentAdapter
from langchain_core.runnables import RunnableLambda

adapter = LangChainAgentAdapter(store)
adapter.start_run(goal="process orders", run_id="lc1")


@adapter.wrap_tool("notify.customer")
def notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return send_email(order_id)


def work(state: dict) -> dict:
    notify(order_id=state["order_id"], continuum_run_id=state["continuum_run_id"])
    return {**state, "done": True}


chain = RunnableLambda(work) | RunnableLambda(adapter.checkpoint_node)
chain.invoke({"continuum_run_id": "lc1", "order_id": "O-1"})
```

LangChain `RunnableLambda` replaces state rather than merging it (unlike a
LangGraph node), so each step must return the full state dict, including
`continuum_run_id`, for downstream nodes to find it.

### Real agent (create_agent)

Wire a wrapped LangChain `Tool` and a checkpoint callback into a real agent:

```python
from langchain.agents import create_agent
from langchain_core.tools import Tool
from langchain_core.callbacks import BaseCallbackHandler

adapter.start_run(goal="process orders", run_id="lc1")


@adapter.wrap_tool("notify.customer")
def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return send_email(order_id)


def notify_tool(order_id: str) -> str:
    return _notify(order_id=order_id, continuum_run_id="lc1")


tool = Tool(name="notify", func=notify_tool, description="Notify a customer")


class Checkpointer(BaseCallbackHandler):
    def on_tool_end(self, output, **kwargs):
        adapter.checkpoint_node({"continuum_run_id": "lc1", "goal": "process orders"})


agent = create_agent(llm, [tool])
agent.invoke(
    {"messages": [("user", "notify the customer")]},
    config={"callbacks": [Checkpointer()]},
)
```

When the agent calls the tool more than once in a run, `wrap_tool` collapses
the calls into a single external side effect, provided the argument-hash key is
stable across calls.

### Explicit idempotency key (real-LLM safe)

Argument-hash dedup assumes identical arguments mean the same operation. An LLM
does not honour that assumption: it can stuff a generated sentence into a
parameter, rename the parameter, or otherwise render the same logical operation
with different argument text between calls. When the argument shape drifts, the
hash differs and dedup silently fails, re-firing the side effect.

Pass an explicit, Stripe-style key to identify the operation by its resource
identity instead of its argument bytes:

```python
@adapter.wrap_tool("notify.customer", key="notify:O-9")
def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return send_email(order_id)
```

Or derive it per call with `key_fn` when the identity lives in an argument:

```python
@adapter.wrap_tool(
    "notify.customer",
    key_fn=lambda *a, **k: f"notify:{str(k.get('order_id', '')).strip()}",
)
def _notify(order_id: str, *, continuum_run_id: str = "") -> str:
    return send_email(order_id)
```

Both forward to `ActionLedger.claim(key=...)`, so two calls that share the action
type and key collapse to one external side effect regardless of argument drift.
`key` and `key_fn` are mutually exclusive; `key_fn` is called with the tool's
`(*args, **kwargs)`.

`LangGraphAgentAdapter.wrap_tool` and `OpenAIAgentAdapter.wrap_function_tool`
expose the same `key` / `key_fn` parameters, so the pattern applies uniformly
across all three framework adapters.

## Exactly-once side effects and recovery

- External side effects are claimed in the action ledger before execution and
  completed after. A second call with the same run-scoped arguments returns the
  cached result without re-executing the underlying function.
- When the caller cannot guarantee stable argument text (an LLM-driven tool),
  pass an explicit `key` or `key_fn` to `wrap_tool` so dedup keys on resource
  identity rather than the argument hash.
- `checkpoint_node` persists a `STATE_CHECKPOINTED` event and a restorable
  semantic state.
- `assess_graph_recovery` / `assess_langchain_recovery` (or `resume`) validates
  the state and returns a `RecoveryDecision`.

### Provenance and the human gate

A run started through the LangGraph or LangChain adapter is recorded with
`Origin.DETERMINISTIC` provenance, because the adapter is the orchestrator
starting the run on CONTINUUM's behalf. A consistent such run resumes
(`RESUME`) without a human in the loop.

State reported over MCP, or through the OpenAI adapter, uses
`Origin.EXTERNAL_AGENT` provenance, which the validator marks `REQUIRES_REVIEW`.
That is intentional: an agent must not validate its own unverified work. The
consequence is that such runs resolve to `request_human` on `continuum resume`
until a human has eyeballed them.

## Integration tests

The adapter-specific end-to-end tests live in:

- `tests/test_integration_langgraph.py` - checkpoint durability, exactly-once
  side effect across duplicate runs, and a crash after the checkpoint that does
  not duplicate the side effect on resume.
- `tests/test_integration_langchain.py` - LCEL pipeline checkpoint and
  exactly-once side effect, and the same crash-after-checkpoint resume.
- `tests/test_integration_langchain_agent.py` - a real `create_agent`
  tool-calling loop (offline, driven by a scripted fake model) proving the
  side effect fires once even when the agent repeats the tool call, and stays
  once across separate agent invocations.
- `tests/test_integration_langchain.py::TestLangChainArchitecture::test_explicit_key_deduplicates_against_argument_drift`
  and `test_key_fn_derives_key_from_call_arguments` - prove an explicit
  `key` / `key_fn` collapses repeated calls even when the argument text drifts
  between invocations.

Each test imports its framework with `pytest.importorskip`, so it is collected
but skipped when the optional dependency is not installed.

### Real-LLM harness

`examples/langchain_real_llm.py` drives the LangChain adapter against a live
OpenAI-compatible model through OpenRouter (`ChatOpenAI` with
`base_url="https://openrouter.ai/api/v1"`). It runs a real `create_agent`
tool-calling loop over the same CONTINUUM run twice (a first pass plus a resume)
and asserts the wrapped external side effect fires exactly once. The model is
selected via `OPENROUTER_MODEL` (default `openai/gpt-4o-mini`); the key is read
from `OPENROUTER_API_KEY` and never written to disk. Run it with:

```bash
OPENROUTER_API_KEY=sk-or-... OPENROUTER_MODEL=openai/gpt-4o-mini \
  python examples/langchain_real_llm.py
```

It exercises the exact gap the scripted tests cannot: a live LLM produces
drifting argument text, and the stable `key` is what keeps the side effect
idempotent. See STATUS.md (Real-LLM framework adapter test, 2026-08-15) for the
recorded output and what it establishes.

`examples/openai_real_llm.py` and `examples/langgraph_real_llm.py` run the same
dual-invocation contract (first pass plus resume, exactly-once side effect) for the
OpenAI Agents SDK and LangGraph adapters respectively, using the same OpenRouter
setup. The OpenAI harness must use `OpenAIChatCompletionsModel` over the chat
completions endpoint, because OpenRouter does not fully support the Agents SDK's
Responses API. `examples/multitool_real_llm.py` is the richer live demo: a single
model prompt orchestrates three tools (lookup, notify, open ticket) through the
LangGraph adapter, each side effect wrapped with a *fixed* idempotency key, proving
exactly-once survives the model's argument drift across a soft resume.

`examples/langchain_real_llm_crash.py` drives the hard-crash contract with a live
model: the `crash` subcommand lets the agent call the wrapped tool (which performs a
real outbox write) and then hard-exits the process (`os._exit(137)`) before the
ledger records completion; the `resume` subcommand runs a fresh process that calls
`RecoveryEngine.assess`, which must report `request_human` / `safe=False` with one
uncertain action and an outbox that still holds exactly one entry. This shows a live
model's side effect is left uncertain on a mid-side-effect kill and is never
duplicated on reconciliation. `examples/openai_real_llm_crash.py` and
`examples/langgraph_real_llm_crash.py` drive the identical contract for the OpenAI
Agents SDK and LangGraph adapters (the OpenAI one uses `OpenAIChatCompletionsModel`
over the chat completions endpoint, as its soft-resume sibling does). All three
adapters' hard-crash paths are now verified against a live model. Same env vars
apply:

```bash
OPENROUTER_API_KEY=sk-or-... python examples/langchain_real_llm_crash.py crash
OPENROUTER_API_KEY=sk-or-... python examples/langchain_real_llm_crash.py resume
```

