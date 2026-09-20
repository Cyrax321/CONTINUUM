### Run the proof yourself

These scripts are the primary evidence, verified end to end rather than
described:

```bash
python examples/crash_recovery_agent.py   # real process kill, real side effect
python examples/context_compaction.py     # transcript lost, checkpoint survives
python examples/model_switch.py           # Model A dies, Model B resumes safely
python scripts/mcp_smoke.py               # real subprocess, real JSON-RPC traffic
```

`crash_recovery_agent.py` starts a run, performs an external side effect, then
terminates the process with `os._exit(9)` - no cleanup, no flush - while the
dataset it depends on changes underneath it. It restarts, detects the change,
refuses to resume until the uncertain side effect is reconciled, and finishes
with the work not repeated and the side effect not duplicated.

`context_compaction.py` simulates a long-running agent whose context window
fills up and is compacted - the full conversation history is discarded. The
semantic checkpoint survives. It measures the actual compression: full
transcript size versus the bounded recovery context, and confirms the task can
continue correctly from the checkpoint alone.

`model_switch.py` runs a task under Model A, checkpoints (including
model-specific assumptions), then simulates Model A becoming unavailable. Model
B attempts to resume. CONTINUUM flags the model-specific state as needing
revalidation - it does not silently assume the switch is safe.

`mcp_smoke.py` drives the MCP server as a subprocess over stdio and prints every
JSON-RPC frame as it crosses the wire. It asserts, rather than reports, that the
same action intercepted twice returns `proceed: false` with the prior result.
By default it spawns the installed `continuum-mcp` console script (PATH
resolution being the most common real-world failure, issue #839), falling back
to `python -m continuum.mcp` against the worktree source when the script is not
on PATH; `--entry-point module` or `--entry-point console-script` forces either
form. Its output also states the observed wire framing of the response frames,
`\r\n` on Windows or `\n` elsewhere (see the MCP doc's troubleshooting section).

Both exit non-zero if their guarantees fail.

The `e2e-autonomy-test/` kit (issue #6) takes the proof one level further. It
scripts a real invoice-batch task, a hard-kill mid-run, and a fresh resume
session, then scores the outbox, ledger, and event chain out of band. It still
requires a human to drive the two Claude Code sessions, so it verifies the
server and toolkit, not yet an autonomous agent. See its README.

The kit has now run against a real Claude Code session. Run 1 scored 7/7
mechanics (exactly-once side effects, ledger, projected progress, event
chain, recovery gate). The autonomy half is demonstrated too: an agent used
`continuum_record_progress`, `continuum_intercept_action`, `continuum_complete_action`, and `continuum_resume`
unprompted, refused to re-send invoices it verified as already sent, and
surfaced the `request_human` verdict instead of overriding it.

One defect surfaced and is fixed: `continuum_intercept_action` originally
hashed the caller's raw `arguments`, so two sessions describing the same
operation with different argument formatting (relative vs absolute path)
computed different idempotency keys and the second session was told
`proceed: true` for an invoice that was already sent. The tool now accepts a
stable `key` (derived from the resource identity, e.g. `invoice:INV-001`),
which makes deduplication immune to argument formatting. The regression test
in `tests/test_mcp_server.py` mirrors the e2e failure: intercept and complete
with `key="invoice:INV-001"` and relative path arguments, then intercept again
with the same key and absolute path arguments, and assert `proceed: false`.

