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

## Registration

Claude Code discovers the server from the project's `.mcp.json`, which declares
it by bare command name:

```json
{
  "mcpServers": {
    "continuum-mcp": {
      "command": "continuum-mcp",
      "args": ["--db", "continuum.db"],
      "env": { "CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code" }
    }
  }
}
```

The bare name is deliberate, an absolute path in a committed file would be wrong
on every machine except the one that wrote it, but it means the client resolves
`continuum-mcp` against the `PATH` it inherited. Registration therefore succeeds
only when the environment CONTINUUM was installed into is on that `PATH`. When it
is not, the client cannot start the server at all; see
[Troubleshooting](#troubleshooting).

Registration makes the tools below available to the agent. It does not by itself
instrument anything: state is recorded when the agent calls the tools, and,
because a voluntary call can always be missed, when the `PostToolUse` hook
installed by `continuum hooks install claude-code` records a file write outside
the model's control.

## Tools

All tools take a `run_id`. Read-only tools stay open to every caller; mutating
tools are gated by the allowlist (see Security).

| Tool | Kind | Purpose |
|------|------|---------|
| `continuum_record_progress` | mutate | Record goal progress (completed/total). |
| `continuum_checkpoint` | mutate | Force a state checkpoint. |
| `continuum_record_summary` | mutate | Record where reasoning stands (plan stack, decisions, open questions, working set) so a fresh session inherits the plan instead of guessing. Capped at 4096 serialized characters; informational only, never moves mode or safety. |
| `continuum_record_plan` | mutate | Upsert plan units by `id` so a resumed session knows the exact remaining work; one call carries a single unit or the whole plan, and units absent from it keep their recorded status. Units carry `id`, `title`, `status` (`pending`, `working`, `done`, `blocked`) and optional `depends_on`. |
| `continuum_intercept_action` | mutate | Claim a side effect; returns whether to proceed. |
| `continuum_complete_action` | mutate | Record a side effect succeeded. |
| `continuum_fail_action` | mutate | Record a side effect did not happen. |
| `continuum_reconcile_action` | mutate | Resolve an uncertain side effect from outside evidence. |
| `continuum_confirm` | mutate | Confirm a human-approved recovery step. |
| `continuum_validate` | read | Check state against the current environment. |
| `continuum_resume` | read | Assess and describe how the run may resume. Omit `run_id` to target the most recently active (interrupted) run. Returns the run's `goal` so a resumed session knows what to continue. |
| `continuum_list_actions` | read | List recorded side effects and their outcomes. |

Twelve tools: three read-only, nine mutating.

Read-only responses `continuum_resume` and `continuum_validate` include a
`constraint_pins` block: per-pin status (`present`, `absent`, `unverifiable`), grace deadline, and flagged set derived from reconstruction accounting (hash-tagged markers in the recovery context, issue #419). The CLI renders flagged pins prominently with TTY-aware colour while piped output stays byte-identical modulo colour codes. No gating changes live here; strict escalation remains in the accounting layer.

## build_server

`continuum.mcp.server.build_server(database=None, *, policy=None, auth=None) -> tuple[Server, Storage]`

Construct the MCP server and its storage. `policy` defaults to `load_policy()`
(allowlist from env or `.continuum/mcp-policy.json`); `auth` defaults to
`load_auth()` (shared secret or per-client tokens from env). Call `server.run(transport=...)`
to serve.

## main

`continuum.mcp.server.main(argv=None)` is the `continuum-mcp` console entry
point. It parses `--db` and `--transport` and runs the server.

## Troubleshooting

### `CONNECTION_CLOSED`

```
❯ /mcp
⎿ Failed to reconnect to continuum-mcp: CONNECTION_CLOSED
```

`claude mcp list` reports the same condition as `✘ Failed to connect`.

The wording describes a server that started and then died. Usually it is a server
that never started: the client could not resolve `continuum-mcp` on the `PATH` it
inherited, and a spawn that fails surfaces as a transport that closed.

No CONTINUUM code runs in this failure. The executable is never located, so the
server cannot detect the condition, report it, or recover from it, the whole
diagnosis has to happen on the client side, which is why it is documented here
rather than handled in code.

This is not a Windows-specific defect, but it shows up there most often. A console
script installs as `.venv\Scripts\continuum-mcp.exe`, and that directory joins
`PATH` only inside an activated virtual environment, so a client started from
anywhere else, a desktop launcher, a fresh terminal, an IDE, inherits a `PATH`
without it. The same failure occurs on macOS and Linux whenever the client is
launched outside the environment holding the install; an already-activated shell
is what usually hides it.

#### Confirm the cause

Ask whether the name resolves in the environment the client was launched from:

```
which continuum-mcp        # macOS, Linux
where.exe continuum-mcp    # Windows (PowerShell)
```

Finding nothing is the diagnosis. Then confirm the server itself is healthy by
running it through its full path:

```
/path/to/.venv/bin/continuum-mcp --help                 # macOS, Linux
& "C:\path\to\.venv\Scripts\continuum-mcp.exe" --help   # Windows (PowerShell)
```

Usage text and exit 0 mean the server is sound and only resolution failed.

#### Remedy 1, launch the client from the installed environment

Activate the environment first, so the client inherits a `PATH` that contains the
install:

```
source .venv/bin/activate        # macOS, Linux
.\.venv\Scripts\Activate.ps1     # Windows (PowerShell)

claude                           # then start the client from that shell
```

Nothing is written to the repository and `.mcp.json` resolves as intended.

#### Remedy 2, pin the absolute path in the local scope

When the client is not launched from a shell, a desktop app, or an IDE, register
the resolved path instead:

```
claude mcp add continuum-mcp --scope local --env CONTINUUM_MCP_MUTATING_CLIENTS=claude-code -- /absolute/path/to/.venv/bin/continuum-mcp --db continuum.db
```

It is one line on purpose: a backslash continuation would not survive
PowerShell. Everything after `--` is the command and its arguments, so on
Windows that tail becomes the `.exe` under `Scripts`:

```
-- "C:\path\to\.venv\Scripts\continuum-mcp.exe" --db continuum.db
```

Keep the `--env` flag: mutating tools deny unlisted callers by default, so an
entry without it connects but exposes only the three read-only tools.

`--scope local` writes to `~/.claude.json` under this project's entry. That file
is per-user and never committed, so an absolute path there is correct and affects
nobody else. Local scope takes precedence over project scope, so this entry wins
over `.mcp.json`.

Claude Code will then report a conflicting-scopes diagnostic naming both
endpoints. That is expected, both definitions exist, and the local one is the one
in use. Do not resolve it with `claude mcp remove continuum-mcp -s project`, which
edits the committed `.mcp.json` and unregisters the server for everyone who clones
the repository. Leave the diagnostic in place, or drop the local entry with
`-s local` once the environment is on `PATH`.
