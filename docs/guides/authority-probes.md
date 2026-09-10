# Authority probes: re-validating a consumed credential

A consumed single-use authority stays blocked until something outside the log
says otherwise. An authority probe is that something: an operator-configured
command that checks the real world (approval dashboard, credential issuer,
outbox) and prints a verdict. `continuum reconcile --authority <id>` runs the
probe and, on a clear answer, settles the block by appending
`AUTHORITY_RECONCILED`. This guide pairs with the registry reference in
`docs/api/cli.md` (the `reconcile` section documents the file shape, timeouts,
and the bool-timeout refusal); what follows is the authority half end to end.

## Prerequisites

- `continuum-agent` installed and a run to work in. The demo below uses a
  throwaway database so nothing touches your real runs.

## 1. Register the probe

Probes live in `reconcilers.json` (default `.continuum/reconcilers.json`,
override with `--config`). Authority entries are keyed by authority id, action
entries by action type; the two namespaces share one file:

```json
{
  "probes": {
    "approval-7": {"command": "/opt/probes/check-approval", "timeout": 10}
  }
}
```

When the probe runs, CONTINUUM writes the recorded consumption as JSON to its
stdin: `authority_id`, `consumer_run_id`, `via_action_id`, `consumed_at`, and
`sequence`. A probe that needs no context can ignore stdin entirely. For this
walkthrough any command printing a verdict will do:

> The command string runs through the platform shell (`cmd.exe /c` on Windows,
> `/bin/sh -c` elsewhere), so its syntax is shell-family-specific: a registry
> written on one platform fails on the other, and the authority then stays
> blocked, fail-closed, until the probe is fixed. Prefer an executable plus
> arguments both shells resolve identically, or keep one registry per platform.

```bash
mkdir -p /tmp/probe-demo
export DB=/tmp/probe-demo/g.db
continuum --db $DB start probe-demo --goal "demo approvals"
```

In production your integration records the consumption when it spends the
credential; here we record one directly so there is something to probe:

```bash
python - <<'EOF'
from continuum.actions.authority import record_authority_consumed
from continuum.storage import SQLiteStorage
with SQLiteStorage("/tmp/probe-demo/g.db") as store:
    record_authority_consumed(store, "probe-demo", "approval-7", via_action_id="action_first")
EOF
```

Write that config to disk where the later commands expect it:

```bash
cat > reconcilers.json <<'EOF'
{"probes": {"approval-7": {"command": "echo valid=true", "timeout": 10}}}
EOF
```

## 2. Read the three verdicts

The probe's final stdout line decides. `valid=true` (or JSON `{"valid": true}`)
unblocks, `valid=false` keeps the block, anything else (including empty output)
is unknown and keeps the block, and a crashing or timing-out probe is an error
rather than an answer. Nothing about the verdict is guessed.

Dry-run first to see the answer without writing:

```bash
continuum --db $DB reconcile probe-demo --authority approval-7 \
  --config reconcilers.json --dry-run
```

```text
authority 'approval-7' still valid, reconciled and unblocked
detail:
dry run: nothing was written
```

Then for real (exit code 0):

```bash
continuum --db $DB reconcile probe-demo --authority approval-7 \
  --config reconcilers.json
```

```text
authority 'approval-7' still valid, reconciled and unblocked
detail:
```

A probe that answers `valid=false` keeps the block and exits 20:

```text
authority 'approval-7' not valid, remains blocked
detail:
```

A probe that cannot tell (`echo maybe`) also exits 20, saying so explicitly:

```text
authority 'approval-7' probe could not determine validity
detail: authority probe could not determine validity from output 'maybe'
```

Machine readers get the same report as JSON (`settled` tells whether the log
moved):

```json
{
  "authority_id": "approval-7",
  "detail": "",
  "dry_run": false,
  "run_id": "probe-demo",
  "settled": true,
  "valid": true
}
```

## 3. What unblocking writes

A `true` verdict appends one `AUTHORITY_RECONCILED` event carrying the id, the
verdict, and the probe detail, and clears the consumed mark: the next
`collect_consumed_authorities` fold no longer contains the id, so the gate
stops refusing it for prior consumption. A `false` or unknown verdict writes
nothing. Reconciliation never invents validity; it only records what the probe
said, with the probe's words in the payload for audit.

## Rules worth knowing

- Exit code is 0 only when the authority is confirmed still valid. Every other
  outcome exits 20 (`REQUIRES_HUMAN`), including dry runs that answered
  anything but true.
- Probes run with a timeout in seconds (default 10); a timeout is an error,
  not an unknown, and settles nothing.
- The gate keeps refusing the id until the reconciled event lands; restart
  semantics do not resurrect (pinned by `tests/test_authority_probe.py`).
