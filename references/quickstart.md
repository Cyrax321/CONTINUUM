## Quick Start

> Published to PyPI as `continuum-agent` (`pip install continuum-agent`), or
> install from a clone:
>
> ```bash
> uv venv
> uv pip install -e ".[dev]"    # library, CLI, and test tooling
> uv pip install -e ".[mcp]"    # adds the MCP server (optional)
> ```
>
> Two entrypoints are installed: `continuum` (the CLI) and `continuum-mcp`
> (the MCP server). The core library depends only on `pydantic`; the `mcp`
> extra is required solely for the server.

What runs today (Phases 1–11): record events, project state, checkpoint, survive a crash, validate against the current environment, never duplicate an external side effect, decide how it is safe to resume, expose a stdio MCP server, and plug into agent frameworks.

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="Analyze 10,000 documents"))
store.append_event(
    "run_4821", EventType.RUN_STARTED, {"goal": "Analyze 10,000 documents", "total": 10_000}
)

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})
```

The process dies. A new one picks up exactly where it stopped:

```python
store = SQLiteStorage("agent.db")
state = project("run_4821", store.read_events("run_4821"))

print(state.progress.completed)  # 3421 - already done, not repeated
print(store.verify_events("run_4821").ok)  # True - chain intact after the crash

for i, doc in enumerate(documents[state.progress.completed :], state.progress.completed):
    ...
```

