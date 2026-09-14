## Quick Start

One primary path, from PyPI (the brackets are quoted because unquoted brackets
are a glob in zsh):

```bash
pip install "continuum-agent[mcp]"   # library + CLI + MCP server
continuum --help                     # the CLI entrypoint
continuum-mcp --help                 # the MCP server entrypoint
```

Registration with an MCP host, for example the project `.mcp.json` Claude Code
reads, is documented in [docs/api/mcp.md](../docs/api/mcp.md#registration); the
committed `.mcp.json` at the repo root is this repository's own registration.

Contributors and pre-release users work from a clone instead:

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM
uv venv
uv pip install -e ".[dev]"           # library, CLI, and test tooling
```

Two entrypoints are installed either way: `continuum` (the CLI) and
`continuum-mcp` (the MCP server). The core library depends only on `pydantic`;
the `mcp` extra is required solely for the server (`[dev]` already includes it,
so contributors do not install it twice).

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

