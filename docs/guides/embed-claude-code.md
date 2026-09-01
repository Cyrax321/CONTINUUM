# Embed CONTINUUM in Claude Code

Copy-paste hooks that give any Claude Code session crash recovery, instant resume, and constraint-aware gating. No prompt file needed, no Python required in the harness.

This recipe pairs with instant-detection (#394): that work writes `.continuum/resume.json` on every checkpoint so a SessionStart hook can surface an interrupted run without opening the database. This guide consumes that file, it does not duplicate it.

## Prerequisites

- `continuum-agent` installed (`pip install continuum-agent` or `uv pip install -e ".[dev]"` from a clone)
- `continuum --version` works on your PATH
- A project directory where Claude Code stores settings in `.claude/settings.json`

## Two-minute setup

```bash
# 1. Create a run. The goal is what the agent will continue after a crash.
continuum start my-task --goal "Summarize quarterly reports from dataset v3"

# 2. Wire the harness. This installs SessionStart + PostToolUse + PreCompact hooks.
#    Add --with-gate if you use an allowlist for side effects (see Gate section).
continuum hooks install claude-code --with-gate

# 3. Check what was written
cat .claude/settings.json | python -m json.tool
```

Expected hooks installed:

- `SessionStart` → `continuum briefing` (injects active-run context, uses `.continuum/resume.json` fast path)
- `PostToolUse` on `Write|Edit|MultiEdit|NotebookEdit` → `continuum observe` (captures file writes as hash-chained evidence, outside model control)
- `PreCompact` → `continuum precompact` (checkpoints before the transcript is compacted; pass `--no-precompact` to skip it)
- `PreToolUse` on `*` → `continuum gate` when `--with-gate` was passed (denies unclaimed side effects before they fire)

Verify without starting Claude Code:

```bash
continuum briefing --json | python -m json.tool
```

With no active run you get a silent exit (no DB open, no latency). With an interrupted run you get a banner similar to:

```json
{
  "active_run": "my-task",
  "mode": "resume",
  "safe": true,
  "context": "Interrupted run my-task – resume pending\n  run: continuum resume my-task --json\n\nCONTINUUM active run: my-task\ngoal: Summarize quarterly reports from dataset v3\nprogress: 3/10 completed\nrecovery: resume (safe=True)",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Interrupted run my-task – resume pending\n  run: continuum resume my-task --json\n\nCONTINUUM active run: my-task\ngoal: Summarize quarterly reports from dataset v3\nprogress: 3/10 completed\nrecovery: resume (safe=True)"
  }
}
```

## What each hook does

### SessionStart → `continuum briefing`

The hook is not `continuum resume`. It is `continuum briefing`, which composes:

- `.continuum/resume.json` banner when present (written at checkpoint time, read without SQLite)
- `RecoveryEngine.assess(run_id)` which folds validation, ledger, and checkpoint signals and picks the most cautious mode
- `human_steps` derived from the plan and reconciler registry
- last-session reasoning summary and disk-checked post-checkpoint observations

Fast path keeps cold starts under a second: if `.continuum/resume.json` does not exist the hook exits 0 with no output and never opens the DB. That file is the coordination point with #394.

To consume the JSON resume contract directly inside an agent turn, use:

```bash
continuum resume my-task --json | python -m json.tool
```

Key fields:

```json
{
  "run_id": "my-task",
  "mode": "resume",
  "safe": true,
  "next_allowed_action": null,
  "contract": {
    "recovery_status": "safe_to_resume",
    "verified": ["goal", "progress"],
    "invalidated": [],
    "reason": "all state verified against the environment",
    "integrity_hash": "5cdda5..."
  },
  "human_steps": [],
  "pinning_drift": [],
  "post_checkpoint_observations": []
}
```

When `safe` is false, `mode` is one of `repair_and_resume`, `request_human`, `rollback`, `wait`, or `abort`, and `human_steps` contains executable commands. Gate on `safe` or on exit code: only a verified-safe run exits 0, so `continuum resume my-task && ./start-agent.sh` will not launch onto stale state.

### PreCompact → checkpoint and constraint verification

Claude Code fires `PreCompact` before context compaction. Use it to force a checkpoint and re-validate so compaction does not discard unverified reasoning.

`continuum hooks install claude-code` wires this for you: the `PreCompact` entry runs `continuum precompact`, which resolves the active run itself, seals a checkpoint with trigger `context_pressure`, and writes both snapshots below. Pass `--no-precompact` to leave the event alone, and `continuum hooks remove claude-code` takes it out again with the rest.

```bash
continuum precompact --json   # what the hook runs; safe to try by hand
```

It never fails its host: with no active run it exits 0 with nothing sealed, and a snapshot it cannot write is reported in `failures` while the checkpoint stands.

If you want to pin one run instead of following the active one, the hand-written form still works. Copy-paste snippet for `.claude/settings.json` (add alongside the installed hooks, do not replace them):

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "continuum checkpoint my-task --reason \"pre-compact\" || true; continuum resume my-task --json > .continuum/precompact-resume.json; continuum verify my-task --json > .continuum/precompact-verify.json"
          }
        ]
      }
    ]
  }
}
```

Or use the tiny glue script shipped with this repo:

```bash
# examples/hooks/continuum-precompact.sh — same two commands, kept tiny
CONTINUUM_RUN_ID=my-task ./examples/hooks/continuum-precompact.sh
```

Both use the same empty matcher as the installer, so an entry you pasted before this was automated is repointed on the next `hooks install` rather than left to fire twice.

What this gives:

- a sealed checkpoint at the compaction boundary, written to the hash-chained log
- `.continuum/precompact-resume.json` with the recovery decision as of that checkpoint (inspect `contract.verified` and `contract.invalidated`)
- `.continuum/precompact-verify.json` proving the chain is intact up to the checkpoint

Constraint verification: if your run pins constraints by digest (see below), `resume --json` surfaces `pinning_drift` when the current environment pins differ from the recorded set. A non-empty drift does not block resume, it is informational. Check it in PreCompact:

```bash
continuum resume my-task --pinning '{"prompt_sha256":"abc...","tool_schema_sha256":"def..."}' --json | python -c "import json,sys; j=json.load(sys.stdin); print(j['pinning_drift'])"
```

If you need to record constraint pins at run start (hash-only, plaintext never stored):

```python
import hashlib
from continuum.events import EventType
from continuum.models import ConstraintPinned
from continuum.storage import SQLiteStorage

store = SQLiteStorage("continuum.db")
sha = hashlib.sha256("Do not send customer data to external APIs".encode()).hexdigest()
store.append_event("my-task", EventType.CONSTRAINT_PINNED, ConstraintPinned(constraint_id="no-external-egress", sha256=sha).model_dump())
```

Retract similarly with `CONSTRAINT_RETRACTED`. Pins survive compaction and appear under `SemanticState.pins`.

### PostToolUse → `continuum observe`

Already installed by `hooks install`. It records each file write as a `TOOL_COMPLETED` event with path, bytes, and sha256 of what is actually on disk. Enables `contract.post_checkpoint_observations` which lists files changed since the last checkpoint, disk-checked at assess time.

### PreToolUse gate (optional)

With `--with-gate`, every tool call checks `.continuum/gate.json`:

```json
{
  "slack.notify": {
    "key_template": "notify:{order_id}"
  }
}
```

Unclaimed effects are denied before they fire (exit 2, message fed back to the model):

```
[!!] deny: slack.notify is gated and has no claim for key notify:O-9; call continuum_intercept_action first
```

The agent must claim via `GenericAgentAdapter.intercept_action` or via MCP `continuum_intercept_action`, then perform the effect, then `continuum_complete_action`. The gate turns wiring cost from "remember to be durable" into "you cannot fire without being durable".

## Copy-paste `.claude/settings.json` template

If you prefer to hand-edit instead of running `hooks install`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "continuum briefing --json"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "continuum observe"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "continuum gate"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "continuum checkpoint my-task --reason \"pre-compact\" || true; continuum resume my-task --json > .continuum/precompact-resume.json"
          }
        ]
      }
    ]
  }
}
```

Fix absolute paths if `continuum` is not on the hook's PATH: `hooks install` bakes the resolved path. Manual edits should use `which continuum` output or fallback `python -m continuum.cli observe`.

## Hard-kill recovery test (the regression proof)

Every recipe is tested with a real hard kill. Reproduce in under two minutes:

```bash
# fresh DB for the demo
rm -f /tmp/embed-claude-demo.db /tmp/embed-claude-demo.db-wal /tmp/embed-claude-demo.db-shm
continuum --db /tmp/embed-claude-demo.db start hardkill-demo --goal "Demo task for embed test"

# simulate work: 3 units done + one claimed side effect left uncertain (as a hard kill would)
python - << 'PY'
from continuum.models import Run, Origin
from continuum.events import EventType
from continuum.storage import SQLiteStorage
from continuum.actions.ledger import ActionLedger
db="/tmp/embed-claude-demo.db"
store=SQLiteStorage(db)
for i in range(3):
    store.append_event("hardkill-demo", EventType.WORK_COMPLETED, {"doc": i}, source=Origin.HUMAN)
ledger=ActionLedger(store, "hardkill-demo")
outcome=ledger.claim("slack.notify", {"channel": "#alerts", "msg": "hello"})
print(f"claimed {outcome.key} fresh={outcome.fresh}")
# intentionally do not complete -> simulates os._exit(9) before ledger complete
PY

# simulate SIGKILL of the agent process
# (the ledger STILL holds the claim as STARTED; no cleanup ran)

# fresh session resumes (new process, same DB, no prompt needed)
continuum --db /tmp/embed-claude-demo.db resume hardkill-demo --json | python -m json.tool
echo "exit code: $?"
```

Expected contract (real output from this repo, ids vary per run):

```json
{
  "contract": {
    "next_allowed_action": "reconcile_action:action_efbdf7a796e5e36b792ca57f2eb3868e",
    "reason": "1 external side effect(s) have unknown outcomes; at least one repair needs a person",
    "recovery_status": "requires_human"
  },
  "human_steps": [
    "check whether 'slack.notify' actually reached the outside world, then call continuum_reconcile_action(run_id=hardkill-demo, action_key=..., occurred=true|false)"
  ],
  "mode": "request_human",
  "safe": false
}
```

Exit code is 20 (`REQUIRES_HUMAN`), not 0, so a guarded launch stops. Resolve by reconciling or, if the effect indeed never landed, retrying with a budget-aware claim, then `continuum resume` returns `resume` with `safe: true`.

With a clean run (no uncertain actions), the same `resume --json` reports:

```json
{
  "mode": "resume",
  "safe": true,
  "next_allowed_action": null,
  "human_steps": []
}
```

Exit code 0 means launch is safe.

Real hard-kill with `os._exit(9)` (subprocess, no cleanup) also verified:

```
CLAIMED da7942fd0ff97d66197da9ab8c6623e4aeb4db60489fb5857c6a95b067bcd2c9 fresh=True
exit code: 9
mode=request_human safe=False reason=1 external side effect(s) have unknown outcomes
```

## Ten-minute integration metric (fresh checkout)

A newcomer with only this guide should have crash recovery inside ten minutes. Steps timed against a fresh `git clone`:

1. `uv pip install -e ".[dev]"` (about 40s)
2. `continuum start my-task --goal "trial"` (1s)
3. `continuum hooks install claude-code` (1s)
4. Do any work (write a file, claim an action via adapter, checkpoint)
5. `kill -9` the agent (or `os._exit(9)` in the example) (instant)
6. New shell: `continuum resume my-task --json` shows correct mode and next steps (under 1s)

Gap list as of this doc (honest): LangChain/LangGraph/Codex adapters require their optional dependency (`pip install "continuum-agent[langchain]"` etc.) which adds install time but stays inside ten minutes on a warm cache. No gap found for generic adapter path.

## Troubleshooting

- Hook silent on SessionStart with no run: expected. `briefing` checks `.continuum/resume.json` before touching SQLite and exits 0 with no output when no interrupted run exists.
- Hook writes stale command path after moving a virtualenv: re-run `continuum hooks install claude-code` (reports `updated` and rewrites the command).
- `CONNECTION_CLOSED` from MCP: see `docs/api/mcp.md` troubleshooting; hook path issues, not CONTINUUM.
- PreCompact never fires: confirm Claude Code version supports PreCompact (add the entry manually as shown; `hooks install` manages SessionStart/PostToolUse/PreToolUse, PreCompact is an additive entry).
- Resume still `request_human` after reconcile: run `continuum actions my-task --json` to confirm no STARTED/UNKNOWN remains, then `continuum resume` again.

## See also

- `docs/guides/bring-your-own-dashboard.md` to consume the resume contract under any UI
- `docs/guides/embed-codex.md` for the Codex-class equivalent
- `docs/adapters_guide.md` for the recovery funnel across adapters
