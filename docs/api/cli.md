# CLI

The `continuum` command is the command-line surface, also usable in scripts. Exit
codes are a safety contract: only a verified-safe run exits `0`, so
`continuum resume "$RUN" && ./start-agent.sh` cannot launch onto stale state.

```bash
continuum <command> [args]                    # storage defaults to ./continuum.db
continuum --db <url-or-path> <command>        # storage URL or path (default: continuum.db)
continuum --json <command>                    # machine-readable output
```

## Commands

| Command | Purpose |
|---------|---------|
| `init` | Initialize storage for a new database. |
| `runs` | List runs and their status. |
| `start <run_id> --goal <text>` | Create a run with a goal. Mutates storage. |
| `inspect <run_id>` | Show a run's goal, progress, and metadata. |
| `status <run_id>` | Show run status. |
| `history <run_id>` | List checkpoints for a run. |
| `events <run_id>` | Dump the raw event log for a run. |
| `diff <run_id> <from_version> <to_version>` | Compare the environment/state between two versions. |
| `validate <run_id>` | Check state against the current environment. |
| `resume [run_id]` | Assess and describe how the run may resume. Omit `run_id` to resume the most recently active run. Prints the run's `goal` so you know what to continue. |
| `confirm <run_id>` | Confirm a human-approved recovery step. |
| `complete <run_id>` | Close a run as done. Mutates storage. |
| `budget <run_id>` | Retry-budget usage per action type. |
| `tree <run_id> [--limit <n>]` | Show a parent run and its children. `--limit` shows only the newest `n` children. |
| `fork <run_id> --reason <text>` | Approve a divergent continuation as a child run. Mutates storage. |
| `compact <run_id>` | Archive the pre-anchor log prefix. Mutates storage. |
| `checkpoint <run_id>` | Force a state checkpoint. |
| `observe` | Record one observed tool completion. Mutates storage. |
| `gateway` | Run the enforcing HTTP proxy for registered upstreams. Mutates storage. |
| `briefing` | Session-start context: active run, progress, next steps. Read-only. |
| `gate` | Decide whether a tool call may proceed (pre-tool-use hook). Read-only. |
| `hooks` | Manage host-side observation hooks. |
| `verify <run_id>` | Re-audit the event chain for tampering. |
| `reconcile <run_id>` | Settle uncertain actions with registered probes. Mutates storage. |
| `actions <run_id>` | List recorded side effects and flag uncertain outcomes. |
| `show-contract <run_id>` | Print the recovery contract for the run. |
| `replay <run_id>` | Replay events and verify the stored state version. |
| `benchmark [--total <n>]` | Run the CONTINUUM-Bench harness (default: 200 documents per run). |
| `attest-keygen` | Generate an Ed25519 signer key pair (PEM files). |
| `attest <run_id>` | Sign a run's event chain into an attestation document. |
| `attest-verify <run_id> --attest <file>` | Verify a signed attestation against the live chain. |
| `serve` | Run the Tier 0 newline-delimited JSON sidecar (no MCP dependency). |
| `dashboard` | Serve the dashboard (presentation over run data). |

## Examples

```bash
# Is it safe to continue?
continuum resume run_42

# What changed since the last checkpoint?
continuum diff run_42 1 2

# Prove the chain is untampered, then capture a signed attestation
continuum verify run_42
continuum attest run_42 --key signer.pem --out run_42.attest.json
continuum attest-verify run_42 --attest run_42.attest.json

# Same data, machine-readable: --json goes before the command, not after it
continuum --json runs | jq '.runs[] | {run_id, status}'
continuum --json resume run_42 | jq '{safe, mode}'

# Just the newest few children of a wide family
continuum tree run_42 --limit 5
```

`tree --limit <n>` truncates the printed child list to the newest `n` children
and says how many it hid, so a short tree is never mistaken for a small family.
The truncation is display-only: the family safety roll-up behind `resume` still
reads every child, so hiding one cannot turn a blocked family into a safe one.
With `--json`, `children_total` and `children_hidden` report the full count
alongside the truncated `children` list. A `--limit` below `1` is refused rather
than clamped (issue #321).

`--db` (storage URL or path, default `continuum.db`) and `--json`
(machine-readable output) are global flags, so they go before the command:
`continuum --json runs`, not `continuum runs --json`, which is rejected as an
unrecognised argument. Most commands emit JSON with it, and
`continuum <command> --help` lists that command's own flags. Colour is
TTY-aware and respects `NO_COLOR`; piped output is byte-identical to uncoloured
output.

`resume --json` and `validate --json` include a `constraint_pins` block
with per-pin status (`present`, `absent`, `unverifiable`), grace deadline, and
flagged set. Flagged pins render prominently in human text as `[!!]` lines
coloured on TTY and plain when piped, byte-identical modulo colour (issue #419).

## hooks

`continuum hooks install` writes host-side observation hooks into agent
settings files (for example `.claude/settings.json` or `.gemini/settings.json`).
The installed `observe` command is baked in at install time and may take one of
two shapes:

- **`continuum` on PATH**: an absolute path to the resolved executable, for
  example `/usr/local/bin/continuum observe`.
- **Editable / interpreter-only installs**: `/path/to/python -m continuum.cli observe`
  when no `continuum` executable is found on PATH.

If you pass `--db`, that path is baked into the command too (for example
`continuum --db /abs/path.db observe`), because hook processes run with the
project root as cwd and the default database path would otherwise be ambiguous.

To see what was installed, inspect the settings file after install:

```bash
continuum hooks install claude-code --db /tmp/test.db
cat .claude/settings.json
```

If the command looks unexpected after moving a virtualenv, re-run the
same install command for that host, including the original `--db` value
when one was used (for example `continuum hooks install claude-code --db /tmp/test.db`);
it rewrites the baked command path without changing the database target.
