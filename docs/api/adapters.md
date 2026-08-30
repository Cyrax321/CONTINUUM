# Adapters

Adapters wrap an agent loop or framework so that checkpointing, side-effect
interception, and resume happen through CONTINUUM without you reimplementing
them. All adapters share the public surface of `GenericAgentAdapter`.

```python
from continuum.adapters import GenericAgentAdapter
from continuum.storage import SQLiteStorage

adapter = GenericAgentAdapter(SQLiteStorage("continuum.db"))
```

## GenericAgentAdapter

`continuum.adapters.GenericAgentAdapter(storage, *, engine=None)`

The concrete adapter for standard Python agent loops. Construct it with a
`Storage` implementation; it owns a `CheckpointManager` and a `RecoveryEngine`.

### `start_run(goal, *, run_id=None, metadata=None) -> Run`

Create and initialize a new task run. Pass a stable `run_id` to make the run
resumable across processes.

### `capture_state(run_id, state, *, environment=None, reason="") -> StateCheckpoint`

Create and store a semantic state checkpoint for a run. When `environment` is
given, the pinned resources are declared as run dependencies so that a later
environment drift is detected on resume (this is what makes
`resume()` report unsafe after the world moved).

### `restore_state(run_id, *, replay=True) -> SemanticState`

Restore the latest semantic state for a run, optionally replaying events
recorded after the checkpoint.

### `intercept_action(run_id, action_type, action_fn, arguments=None, *, volatile=(), scoped_to_run=True, on_unknown=None, key=None) -> Any`

Intercept and safely execute an external side effect. The effect is claimed in
the ledger first; if it is already known to have happened, the recorded outcome
is returned instead of running `action_fn` again. `volatile` names arguments
that must not participate in identity. `key` supplies a stable idempotency key
directly. `on_unknown` is called when the ledger cannot decide the outcome.

### `resume(run_id, *, current_environment=None, expected_model=None, replay=True) -> RecoveryDecision`

Assess recovery safety and return a `RecoveryDecision` for the run, without
changing anything. The decision's `mode` is one of `RESUME`, `REPLAY`,
`REQUEST_HUMAN`, or `ABORT`.

## LangGraphAgentAdapter

`continuum.adapters.LangGraphAgentAdapter(storage, *, engine=None)`

Subclass of `GenericAgentAdapter` for LangGraph `StateGraph` workflows. Adds:

### `revalidate_environment(run_id, *, current_environment=None, expected_model=None) -> RecoveryDecision`

Re-assess recovery against the current environment without forcing a new
checkpoint. Returns the same `RecoveryDecision` shape as `resume()`; use it to
confirm that an existing checkpointer's run is still safe to continue after the
environment changed (issue #25).

## OpenAIAgentAdapter

`continuum.adapters.OpenAIAgentAdapter(storage, *, engine=None)`

Subclass of `GenericAgentAdapter` for the OpenAI Agents SDK. Wraps
`function_tool` so tool arguments are bound and idempotency is preserved, and
exposes `ContinuumContext` to tools.

### `ContinuumContext`

Passed to OpenAI tools; carries `run_id`, `storage`, and the adapter so a tool
can capture state or intercept its own side effects.

## LangChainAgentAdapter

`continuum.adapters.LangChainAgentAdapter(storage, *, engine=None)`

Subclass of `GenericAgentAdapter` wrapping LCEL runnable pipelines and the
`langchain.agents.create_agent` tool-calling loop.

## Crash recovery in under ten minutes

Each adapter recovers the same way. The generic path needs no extra
install; the three framework adapters need their optional extra. Total time
from a fresh checkout with a warm pip cache is under two minutes; a cold
install stays inside ten.

```python
from continuum.adapters.generic import GenericAgentAdapter
from continuum.storage import SQLiteStorage
store = SQLiteStorage(":memory:")
adapter = GenericAgentAdapter(store)
run_id = "demo"
adapter.start_run(goal="trial", run_id=run_id)
res = adapter.intercept_action(run_id, "slack.notify", lambda: "sent", arguments={"channel": "#x"})
from continuum.state.semantic import project
state = project(run_id, store.read_events(run_id))
adapter.capture_state(run_id, state, reason="pre-kill")
# kill -9 here, then in a fresh process:
decision = adapter.resume(run_id)
assert decision.safe and decision.mode.value == "resume"
```

If the kill lands between claim and complete, `decision.mode` is
`request_human` with `next_allowed_action: reconcile_action:...` and
`safe` is false. LangChain and LangGraph use the same `wrap_tool`
with `key` or `key_fn` so LLM argument drift does not defeat dedup;
OpenAI uses `wrap_function_tool` and `ContinuumContext`. Live hard-kill proofs
exist per adapter: `examples/crash_recovery_agent.py` (generic), `examples/langchain_real_llm_crash.py`, `examples/langgraph_real_llm_crash.py`, and `examples/openai_real_llm_crash.py` each drive a real kill with `os._exit(137)` and assert the contract blocks resume.

## Availability flags

`langgraph_available`, `openai_agents_available`, and `langchain_available` are
booleans reflecting whether the optional dependency is importable. Importing an
adapter whose dependency is missing raises at construction, not at import.
