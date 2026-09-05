# Embed CONTINUUM in Codex

Copy-paste hooks that give any Codex-class session crash recovery and recoverable side effects. The resume contract is the same substrate as the Claude Code recipe, but wiring differs because Codex gates its hook engine behind a feature flag and only traverses hooks for shell tool calls.

This recipe pairs with instant-detection (#394) the same way the Claude Code guide does: checkpoint writes `.continuum/resume.json`, SessionStart reads it without opening SQLite. This doc does not duplicate that code, it shows how to consume it under Codex.

## Prerequisites

- `continuum-agent` installed and `continuum --version` on PATH
- Codex installed (`codex --version`)
- A project directory tracked by Codex

## Enable the hook engine

Codex hooks are off by default. Without this line hooks are silent no-ops.

Add to `~/.codex/config.toml` (create if missing):

```toml
[features]
codex_hooks = true
```

Then restart Codex.

Verify the hook engine sees the flag:

```bash
continuum hooks install codex --with-gate
```

If the flag is absent the install prints:

```
note: 'codex_hooks' was not found in ~/.codex/config.toml; add '[features]
codex_hooks = true', then restart Codex.
```

That notice is expected, it is not a failure.

## Two-minute setup

```bash
continuum start my-task --goal "Ship feature X with dataset v3"

# Wires SessionStart + PostToolUse for Codex. Add --with-gate to enforce claims.
continuum hooks install codex --with-gate

cat .codex/hooks.json | python -m json.tool
```

Expected entries in `.codex/hooks.json`:

- `SessionStart` → `continuum briefing` (instant banner via `.continuum/resume.json` fast path)
- `PostToolUse` on `^Bash$|^shell$` → `continuum observe` (file evidence)
- `PreToolUse` on `^Bash$|^shell$` → `continuum gate` when `--with-gate` was used

Manual verification without starting Codex:

```bash
continuum briefing --json | python -m json.tool
continuum resume my-task --json | python -m json.tool
```

As with Claude Code, `briefing` exits 0 with no output when no interrupted run exists (checked via `.continuum/resume.json` before any DB open), so cold starts stay fast.

## What each hook does

### SessionStart → `continuum briefing`

Same behavior as the Claude Code recipe: banner when `.continuum/resume.json` exists, full `RecoveryEngine.assess` otherwise. The banner before compaction looks like:

```
Interrupted run my-task – resume pending
  run: continuum resume my-task --json
```

Example `resume --json` contract while safe:

```json
{
  "run_id": "my-task",
  "mode": "resume",
  "safe": true,
  "contract": {
    "recovery_status": "safe_to_resume",
    "verified": ["goal", "progress"],
    "reason": "all state verified against the environment"
  },
  "human_steps": []
}
```

Gate on `safe` or on exit code: only `resume` exits 0. `continuum resume my-task && ./start-agent.sh` will not launch onto stale or uncertain state.

### PostToolUse → `continuum observe`

Codex hooks only traverse shell-like tool calls. The matcher is `^Bash$|^shell$` (see `src/continuum/clienthooks.py:CLIENT_PROFILES["codex"]`, pinned to this page by `tests/test_cli_precompact.py` so the profile and the guide cannot drift apart). That means `apply_patch` and MCP tools do not fire `observe` directly.

Recommended pattern: do file writes through a shell call so they are observed. Both of these work:

```bash
# direct shell write (observed because tool_name is Bash/shell)
cat > src/feature.py << 'PY'
...
PY

# or wrap apply_patch in a Bash echo so the hook still sees it
```

If you cannot route through Bash, the durability alternative is `continuum observe` via the adapter's `checkpoint_node` or via `monitored_commands` in `.continuum/config` (not covered here). The constraint is Codex's, not CONTINUUM's, so this doc states it explicitly rather than hiding it.

### PreToolUse gate (optional)

With `--with-gate`, Codex shell calls are denied before they fire when unclaimed:

```
[!!] deny: slack.notify is gated and has no claim for key notify:O-9
```

Register gated types in `.continuum/gate.json`:

```json
{
  "slack.notify": {
    "key_template": "notify:{order_id}"
  }
}
```

Claim before firing, via the generic adapter or MCP. The gate message teaches the protocol, turning wiring from discipline into enforcement.

### PreCompact (add manually)

Codex does not yet expose a PreCompact-equivalent event. Mirror the Claude Code PreCompact pattern with a periodic checkpoint you control:

```bash
# append to your agent loop or as a stop hook
continuum checkpoint my-task --reason "periodic" || true
continuum resume my-task --json > .continuum/precompact-resume.json
```

Or use the tiny glue script:

```bash
CONTINUUM_RUN_ID=my-task ./examples/hooks/continuum-precompact.sh
```

When Codex adds a compaction hook, add a matching entry under that event name pointing at the same two commands. The shape is forward-compatible.

## Copy-paste `.codex/hooks.json` template

If you prefer to hand-edit rather than running `hooks install`:

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
        "matcher": "^Bash$|^shell$",
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
        "matcher": "^Bash$|^shell$",
        "hooks": [
          {
            "type": "command",
            "command": "continuum gate"
          }
        ]
      }
    ]
  }
}
```

Replace `continuum` with the absolute path from `which continuum` if the hook's PATH does not include your virtualenv. `hooks install` bakes that path automatically.

## Constraint verification

Same as the Claude Code guide: pin digests, not text, via `CONSTRAINT_PINNED` events, and verify drift on resume.

Pin at run start:

```python
import hashlib
from continuum.events import EventType
from continuum.models import ConstraintPinned
from continuum.storage import SQLiteStorage
store = SQLiteStorage("continuum.db")
sha = hashlib.sha256("No direct DB writes without review".encode()).hexdigest()
store.append_event("my-task", EventType.CONSTRAINT_PINNED, ConstraintPinned(constraint_id="no-unreviewed-db", sha256=sha).model_dump())
```

Check drift on resume:

```bash
continuum resume my-task --pinning '{"prompt_sha256":"abc...","tool_schema_sha256":"def..."}' --json | python -m json.tool | grep pinning_drift -A3
```

Non-empty drift is surfaced as informational lines in the resume text and as `pinning_drift` in JSON. It does not block resume, it is the verification layer.

## Hard-kill recovery test

Same contract, same proof as the Claude Code guide. Reproduce with a fresh DB:

```bash
rm -f /tmp/embed-codex-demo.db /tmp/embed-codex-demo.db-wal /tmp/embed-codex-demo.db-shm
continuum --db /tmp/embed-codex-demo.db start hardkill-codex --goal "Demo task for Codex embed test"

python - << 'PY'
from continuum.models import Origin
from continuum.events import EventType
from continuum.storage import SQLiteStorage
from continuum.actions.ledger import ActionLedger
db="/tmp/embed-codex-demo.db"
store=SQLiteStorage(db)
for i in range(3):
    store.append_event("hardkill-codex", EventType.WORK_COMPLETED, {"doc": i}, source=Origin.HUMAN)
ledger=ActionLedger(store, "hardkill-codex")
outcome=ledger.claim("slack.notify", {"channel": "#alerts"})
print(f"claimed {outcome.key} fresh={outcome.fresh}")
# do not complete -> hard kill before ledger complete
PY

# SIGKILL simulation: no cleanup runs, ledger stays STARTED

continuum --db /tmp/embed-codex-demo.db resume hardkill-codex --json | python -m json.tool
echo "exit code: $?"
```

Expected (real output from this repo, ids vary per run):

```json
{
  "contract": {
    "next_allowed_action": "reconcile_action:action_... ",
    "reason": "1 external side effect(s) have unknown outcomes; at least one repair needs a person",
    "recovery_status": "requires_human"
  },
  "human_steps": [
    "check whether 'slack.notify' actually reached the outside world, then call continuum_reconcile_action(run_id=hardkill-codex, action_key=..., occurred=true|false)"
  ],
  "mode": "request_human",
  "safe": false
}
```

Exit code 20 (`REQUIRES_HUMAN`). After `continuum reconcile` or manual reconcile settling the claim, a fresh `continuum resume hardkill-codex --json` reports `resume` with `safe: true` and exit 0.

Clean-run case (no uncertain actions) stays `resume` / `safe: true` and exit 0, identical to the Claude Code path.

Real `os._exit(9)` verified in this repo, same as the Claude Code proof.

## Ten-minute integration metric

Measured from a fresh `git clone`:

1. `uv pip install -e ".[dev]"` (about 40s)
2. `continuum start my-task --goal "trial"` (1s)
3. `continuum hooks install codex --with-gate` (1s)
4. Do work, claim an action, checkpoint (any adapter or raw events)
5. `kill -9` (instant)
6. `continuum resume my-task --json` shows `request_human` with reconciliate step when uncertain, `resume` when clean (under 1s)

Gap list: Codex file writes via `apply_patch` are not hook-traversed and therefore not observed unless routed through `Bash`/`shell`. That is a Codex hook scope limit, not a CONTINUUM missing glue, and the workaround (write via shell) is copy-paste. No gap for Bash-mediated runs. If a future Codex release expands hook traversal, this line becomes stale and should be deleted.

## Troubleshooting

- Hooks silent: confirm `[features] codex_hooks = true` is in `~/.codex/config.toml` and Codex was restarted. `hooks install` surfaces this as a `note:` line.
- Hook writes stale path after moving venv: re-run `continuum hooks install codex` (reports `updated`).
- `observe` never fires for `apply_patch`: expected, see matcher note above. Route that write through Bash/shell or capture via adapter `checkpoint_node`.
- `briefing` always empty: expected when no run has been checkpointed yet and `.continuum/resume.json` does not exist. After `continuum checkpoint my-task` or adapter `capture_state`, `resume.json` appears and SessionStart injects.

## See also

- `docs/guides/embed-claude-code.md` for the Claude Code equivalent
- `docs/guides/bring-your-own-dashboard.md` to render the resume contract under any UI
- `src/continuum/clienthooks.py` for the `CLIENT_PROFILES` matcher and settings paths
