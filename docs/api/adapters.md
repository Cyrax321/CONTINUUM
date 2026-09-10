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

## BrowserAdapter

`continuum.adapters.BrowserAdapter(storage, *, engine=None)`

A `GenericAgentAdapter` subclass for browser automation driven via Playwright.

### `navigate(run_id, url, *, dep_scope=None) -> AdapterResult`

Navigates to `url` using Playwright Chromium in a headless context, extracts page
content, and records the step through the `ActionLedger` under action name
`"browser.navigate"`.

```python
from continuum.adapters import BrowserAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage

storage = SQLiteStorage(":memory:")
storage.create_run(Run(run_id="run_1", goal="browse"))
adapter = BrowserAdapter(storage)
if adapter.available():
    result = adapter.navigate("run_1", "https://example.com")
```

**The easy-to-get-wrong part:** Requires the optional `playwright` package and its
browser binaries. The adapter imports Playwright lazily at navigation time so
importing `BrowserAdapter` is always safe, but `navigate()` raises `RuntimeError`
when Playwright is absent. Use `BrowserAdapter.available()` to check readiness.
Covered in `tests/test_environment_adapters.py`.

## ContainerAdapter

`continuum.adapters.ContainerAdapter(storage, image, *, engine=None)`

A `GenericAgentAdapter` subclass that runs isolated shell commands inside a
Docker container.

### `run_in_container(run_id, command, *, dep_scope=None) -> AdapterResult`

Executes `command` inside the configured container image via `docker run --rm`,
returning an `AdapterResult` with captured stdout and recording the execution in
the `ActionLedger` under action name `"container"`.

```python
from continuum.adapters import ContainerAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage

storage = SQLiteStorage(":memory:")
storage.create_run(Run(run_id="run_1", goal="container-task"))
adapter = ContainerAdapter(storage, image="alpine:latest")
if adapter.available():
    result = adapter.run_in_container("run_1", "echo hi")
```

**The easy-to-get-wrong part:** `image` is bound at adapter construction time rather
than per call. Requires the `docker` CLI on `PATH` and an active Docker daemon.
When `docker` is missing, `run_in_container()` raises `RuntimeError`. Check
availability with `ContainerAdapter.available()`. Covered in
`tests/test_environment_adapters.py`.

## KubernetesAdapter

`continuum.adapters.KubernetesAdapter(storage, *, namespace="default", engine=None)`

A `GenericAgentAdapter` subclass for executing one-shot batch jobs on a Kubernetes
cluster.

### `run_job(run_id, image, command, *, dep_scope=None) -> AdapterResult`

Launches a one-shot pod job in the specified `namespace` using `kubectl run`,
captures output, and records the step in the `ActionLedger` under action name
`"k8s.job"`.

```python
from continuum.adapters import KubernetesAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage

storage = SQLiteStorage(":memory:")
storage.create_run(Run(run_id="run_1", goal="k8s-task"))
adapter = KubernetesAdapter(storage, namespace="default")
if adapter.available():
    result = adapter.run_job("run_1", "alpine:latest", "echo hi")
```

**The easy-to-get-wrong part:** Unlike `ContainerAdapter`, `KubernetesAdapter` takes
the container `image` as a parameter to `run_job()` rather than at initialization.
It requires both `kubectl` on `PATH` and the `kubernetes` Python package. If either
is unavailable, `run_job()` raises `RuntimeError`. Check with
`KubernetesAdapter.available()`. Covered in `tests/test_environment_adapters.py`.

## FilesystemSandboxAdapter

`continuum.adapters.FilesystemSandboxAdapter(storage, sandbox_dir, *, engine=None)`

A `GenericAgentAdapter` subclass providing a local filesystem sandbox for shell
actions without external container or browser dependencies.

### `run_shell(run_id, command, *, dep_scope=None) -> AdapterResult`

Executes `command` with `shell=True` and `cwd=sandbox_dir`, recording the command
as an idempotent `ActionLedger` action (`action_type="shell"`).

```python
import tempfile
from continuum.adapters import FilesystemSandboxAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage

with tempfile.TemporaryDirectory() as sandbox:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="sandbox-task"))
    adapter = FilesystemSandboxAdapter(storage, sandbox)
    result = adapter.run_shell("run_1", "echo 'hello from sandbox'")
    assert result.status == "completed"
```

**The easy-to-get-wrong part:** The sandbox directory is created automatically on
adapter initialization (`mkdir(parents=True, exist_ok=True)`). Because executions
record through the `ActionLedger`, identical shell commands with matching
arguments within the same run are deduplicated on replay. Covered in
`tests/test_filesystem_adapter.py`.

## PythonInProcAdapter

`continuum.adapters.python_inproc.PythonInProcAdapter(storage, workdir, *, engine=None)`

A `GenericAgentAdapter` subclass (in `continuum.adapters.python_inproc`) that
executes Python snippets in a dedicated working directory using the host
interpreter. Also exported from `continuum.adapters`.

### `run_python(run_id, code, *, dep_scope=None) -> AdapterResult`

Executes `code` in a subprocess using `sys.executable` with `cwd=workdir`,
recording the execution in the `ActionLedger` under action name `"python"`.

```python
import tempfile
from continuum.adapters import PythonInProcAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage

with tempfile.TemporaryDirectory() as workdir:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="python-task"))
    adapter = PythonInProcAdapter(storage, workdir)
    result = adapter.run_python("run_1", "print(1 + 1)")
    assert result.status == "completed"
```

**The easy-to-get-wrong part:** Code executes in an isolated subprocess using
`sys.executable`, not in the calling process's global scope. Like
`FilesystemSandboxAdapter`, the working directory is created automatically at
initialization. Covered in `tests/test_environment_adapters.py`.

## AdapterRegistry

`continuum.adapters.AdapterRegistry()`

Registry and discovery mechanism mapping adapter names to lazy factories.
Enables framework discovery while preventing premature imports of optional
dependencies.

### Functions

- `register_adapter(name, factory)`: Register a callable factory `() -> type`
  returning an adapter class.
- `get_adapter(name) -> type`: Look up and invoke the registered factory for
  `name`, raising `ValueError` for unknown names.
- `list_adapters() -> list[str]`: List sorted names of all registered adapters.
- `recover(name, run_id, storage, ...) -> RecoveryDecision`: Look up the adapter
  by name, instantiate it with `storage`, and invoke its `resume()` method.

```python
from continuum.adapters import AdapterRegistry, get_adapter, list_adapters
from continuum.adapters.generic import GenericAgentAdapter

# Built-in registered adapters: "generic", "langchain", "langgraph", "openai"
names = list_adapters()
assert "generic" in names

adapter_cls = get_adapter("generic")
assert adapter_cls is GenericAgentAdapter

# Custom registration uses a lazy factory
registry = AdapterRegistry()
registry.register("custom", lambda: GenericAgentAdapter)
assert registry.get("custom") is GenericAgentAdapter
```

**The easy-to-get-wrong part:** `register_adapter` expects a zero-argument callable
factory (`Callable[[], type]`) that returns the adapter class, not an instance or
the bare class directly. The four built-in factories (`generic`, `langchain`,
`langgraph`, `openai`) resolve their modules lazily only when requested. Covered in
`tests/test_adapters_registry.py`.

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
