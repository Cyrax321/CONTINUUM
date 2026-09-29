## API

### Python - available now

```python
from continuum import EventType, Run, SQLiteStorage, diff_states, project

store = SQLiteStorage("sqlite:///agent.db")
store.create_run(Run(run_id="run_4821", goal="Analyze these documents"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "...", "total": 100})

state = project("run_4821", store.read_events("run_4821"))  # fold events into state
store.put_version(state, reason="milestone")  # versioned history
store.verify_events("run_4821")  # audit the chain
diff_states(previous, state)  # what changed, semantically
```

### Python - the adapter API (Phase 7)

`SQLiteStorage` doubles as the MCP server adapter; `Run` carries the agent-facing surface. Instead of
a `run.record_action(type="github.create_issue", ...)` convenience method that does not exist, the
contract is: the adapter records the side effect with `store.append_event`, using the
event types in `EventType`, and the ledger deduplicates on recovery. There is no
`runtime.start` / `runtime.resume` object yet; that arrives with the non-MCP runtime binding, which
is out of scope for 0.1.0.

