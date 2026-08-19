# MCP server

CONTINUUM ships an MCP server so any MCP client (for example Claude Code) can use
its durability layer without embedding Python. The server is `continuum-mcp`,
built from `continuum.mcp.server.build_server`.

```bash
# stdio (the transport Claude Code uses)
continuum-mcp --db continuum.db

# or over the network
continuum-mcp --transport sse
continuum-mcp --transport streamable-http
```

With the project's `.mcp.json` present, Claude Code registers the server
automatically and every agent action is checkpointed, validated, and recorded as
it happens.

## Tools

All tools take a `run_id`. Read-only tools stay open to every caller; mutating
tools are gated by the allowlist (see Security).

| Tool | Kind | Purpose |
|------|------|---------|
| `continuum_record_progress` | mutate | Record goal progress (completed/total). |
| `continuum_checkpoint` | mutate | Force a state checkpoint. |
| `continuum_intercept_action` | mutate | Claim a side effect; returns whether to proceed. |
| `continuum_complete_action` | mutate | Record a side effect succeeded. |
| `continuum_fail_action` | mutate | Record a side effect did not happen. |
| `continuum_reconcile_action` | mutate | Resolve an uncertain side effect from outside evidence. |
| `continuum_confirm` | mutate | Confirm a human-approved recovery step. |
| `continuum_validate` | read | Check state against the current environment. |
| `continuum_resume` | read | Assess and describe how the run may resume. Omit `run_id` to target the most recently active (interrupted) run. |
| `continuum_list_actions` | read | List recorded side effects and their outcomes. |

## build_server

`continuum.mcp.server.build_server(database=None, *, policy=None, auth=None) -> tuple[Server, Storage]`

Construct the MCP server and its storage. `policy` defaults to `load_policy()`
(allowlist from env or `.continuum/mcp-policy.json`); `auth` defaults to
`load_auth()` (shared secret or per-client tokens from env). Call `server.run(transport=...)`
to serve.

## main

`continuum.mcp.server.main(argv=None)` is the `continuum-mcp` console entry
point. It parses `--db` and `--transport` and runs the server.
