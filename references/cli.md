### CLI

Standard library only - a recovery tool must not fail to import when you most need it.
For the complete command reference with detailed options and tables, see [docs/api/cli.md](../docs/api/cli.md).

```bash
continuum init                                   # create storage
continuum runs                                   # list runs
continuum start <run_id> --goal "..."            # create a run with a goal. mutates
continuum inspect <run_id> [--version 17]        # semantic state, now or at a version
continuum status <run_id>                        # show run status
continuum history <run_id>                       # version and checkpoint history
continuum events <run_id> [--after N --upto M]   # raw event log
continuum diff <run_id> <from> <to>              # semantic diff between versions
continuum validate <run_id> --env dataset=v4     # validate. read-only
continuum resume <run_id> --env dataset=v4       # recovery decision + contract
continuum checkpoint <run_id>                    # force a checkpoint. mutates
continuum verify <run_id>                        # re-audit the event hash chain
continuum actions <run_id>                       # external side effects
continuum show-contract <run_id>                 # the machine-readable contract
continuum replay <run_id> [--upto N]             # re-derive state from events
continuum reconcile <run_id> [--dry-run]         # settle uncertain effects with probes
continuum confirm <run_id>                       # human blessing for self-reported state
continuum complete <run_id> [--summary "..."]    # close a run as done. mutates
continuum budget <run_id>                        # retry-budget usage per action type
continuum tree <run_id> [--limit N]              # show parent run and child runs
continuum fork <run_id> --reason "..."           # approve divergent child continuation. mutates
continuum merge <run_id> --reason "..."          # merge child run into parent at anchor. mutates
continuum restore <run_id> --reason "..."        # restore run to anchor checkpoint. mutates
continuum compact <run_id>                       # archive pre-anchor log prefix. mutates
continuum precompact <run_id>                    # checkpoint before context compaction. mutates
continuum rewind <run_id> --to <checkpoint>      # revert workspace and projection [--force] [--dry-run]
continuum observe                                # record one tool completion (hook). mutates
continuum gate                                   # pre-tool-use verdict: allow or deny
continuum briefing                               # session-start context injection
continuum gateway --port 8765                    # enforcing proxy for registered upstreams. mutates
continuum hooks install <client> [--with-gate]   # wire a coding CLI (claude-code, gemini, codex)
continuum health <run_id>                        # advisory prefix-trust health check
continuum impact <run_id> --evidence <id>        # downstream impact of an evidence item
continuum provenance <run_id>                    # show provenance DAG
continuum record-plan <run_id> --plan-id <id>    # record structured plan upsert. mutates
continuum export-evidence <run_id>               # export evidence as JSON lines
continuum forget --tenant <id> [--dry-run]       # tombstone memory records for a tenant. mutates
continuum watch <run_id>                         # watch run for liveness breach
continuum serve                                  # run JSON sidecar over stdio
continuum dashboard [--port 8080]                # serve web dashboard
continuum tui [--refresh N]                      # full-screen terminal dashboard
continuum attest-keygen                          # generate Ed25519 signer key pair
continuum attest <run_id>                        # sign run event-chain attestation
continuum attest-verify <run_id> --attest <file> # verify signed attestation against live chain
continuum benchmark [--total N]                  # run CONTINUUM-Bench harness
```

Every command accepts `--json`. Read-only commands (`inspect`, `status`, `history`, `events`,
`diff`, `validate`, `resume`, `verify`, `actions`, `show-contract`, `replay`, `budget`, `tree`,
`gate`, `briefing`, `health`, `impact`, `provenance`, `export-evidence`, `watch`) never write, so
they are safe against a live database while an agent is mid-run. Mutating commands (`start`,
`checkpoint`, `confirm`, `complete`, `fork`, `merge`, `restore`, `compact`, `precompact`, `rewind`,
`observe`, `reconcile`, `gateway`, `record-plan`, `forget`) say so in their help.

#### Registries are executable configuration

`.continuum/gate.json`, `.continuum/reconcilers.json` and `.continuum/gateway.json` reference
commands that run with your user privileges, exactly like CI pipelines or Makefiles. Treat a
cloned repository's registries the way you treat its test scripts: read them before you let an
agent loose.

#### Exit codes are a safety contract

```bash
continuum resume "$RUN" && ./start-agent.sh
```

That line must never launch an agent onto stale state, so **only a verified-safe run exits 0**:

| Code | Meaning |
|:--|:--|
| `0` | verified safe to resume |
| `10` | recoverable, but repairs are required first |
| `20` | a human must decide (typically an unreconciled side effect) |
| `30` | not safe to resume |
| `2` / `3` / `4` | not found / integrity failure / not implemented |

A recovery mode nobody has classified falls through to *unsafe*, never to `0`.

```text
$ continuum resume run_4821 --env dataset=v4

Recovery decision: REQUEST_HUMAN
  because 1 external side effect(s) have unknown outcomes

Repairs required:
  1. [auto]  reconcile_action action_cda6e307 - github.create_issue was interrupted
  2. [auto]  revalidate_dependency dataset - v3 -> v4
  3. [auto]  rederive_evidence paper_128 - source 'dataset' changed
  4. [auto]  rederive_finding finding_17 - rests on changed evidence: paper_128

Next permitted action: reconcile_action:action_cda6e307...
$ echo $?
20
```

### State Diff

```bash
continuum diff checkpoint_a checkpoint_b
```

```diff
+ New finding: finding_81
~ Dataset version: v3 -> v4
- Decision #7 invalidated
+ Pending task: re-run experiment
```

---

