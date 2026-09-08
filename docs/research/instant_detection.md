# Instant Autonomous Detection

> **Shipped.** Both halves landed and issue #83 is closed. The `SessionStart` briefing hook runs on session start (#394), and `.continuum/resume.json` is precomputed on every checkpoint in `src/continuum/checkpoint/manager.py` (#394 fast path). This note is kept for historical context.

A Claude Code session only acts after the user sends a message, so detection costs a user message plus a model inference plus an MCP round trip. There is no autonomous session start trigger today.

## Proposal

- **SessionStart hook.** Add a `SessionStart` hook that runs `continuum resume --json` out of band before the first model turn. The hook is a small shell script, not a model call, so it adds tens of milliseconds, not seconds. If the resume reports an interrupted run, the hook injects a pre rendered prompt of the form: `Interrupted run run_123 found. Recovery decision is REQUEST_HUMAN with next action reconcile_action:xyz. Should I reconcile or show the contract`.

- **Wrapper script.** For clients without hooks, provide `continuum-resume-banner` that prints the same banner to stdout. The user aliases `claude` to run the banner first, so the information is visible before the first message.

- **Precomputed banner.** The MCP server can write a `.continuum/resume.json` file on every checkpoint. The hook reads the file without starting Python, so detection is instant even when the server is cold.

No implementation is done here. This is a design note for issue 83, with no external claims.

Reproduce the cost by timing a fresh session: user message, model inference, `continuum_resume` tool call, and the formatted question, versus the hook path which is a single `continuum resume --json` subprocess.

## Risks

A hook that runs on every session start must be fast and must not block when no run exists. The hook should exit 0 with no output when `continuum resume` reports no active run.

## Alternatives

- Keep the current user message trigger. Simple but keeps the latency tax.
- Dedicated lightweight server. More to maintain than a hook plus a file.
