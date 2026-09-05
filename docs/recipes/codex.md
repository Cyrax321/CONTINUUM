# Codex hooks: SessionStart + PreCompact (Bash-only)

Copy-paste recipes for wiring CONTINUUM into Codex.

## What you get

- **SessionStart** injects the recovery contract. `continuum hooks install codex` writes it.
- **PreCompact** re-verifies constraints. Codex publishes no compaction event, so this one is copy-paste (see below) and reuses SessionStart.

Both are read-only. The installed `briefing` entry exits 0 with no output when no run is active.

## Enable Codex hooks (one-time)

```toml
# ~/.codex/config.toml
[features]
codex_hooks = true
```

Restart Codex.

## Install (one command)

```bash
continuum hooks install codex
```

This writes to `.codex/hooks.json`:

- `PostToolUse` on `^Bash$|^shell$` → `continuum observe` (Bash-only today)
- `SessionStart` → `continuum briefing`

Verify:

```bash
cat .codex/hooks.json | python -m json.tool
```

## Add PreCompact / resume variant (copy-paste)

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/continuum resume --json"
          }
        ]
      }
    ]
  }
}
```

```bash
continuum --json resume | python -m json.tool | grep -q "pins"
```

## Hard-kill test (real process)

Same test as Claude Code, but Bash-only:

```bash
python - << 'PY'
import os
from continuum import SQLiteStorage, Run, ActionLedger
from continuum.events import EventType
store = SQLiteStorage("continuum.db")
store.create_run(Run(run_id="codex_test", goal="hook test"))
store.append_event("codex_test", EventType.RUN_STARTED, {"goal": "hook test"})
ledger = ActionLedger(store, "codex_test")
outcome = ledger.claim("demo.write", {"path": "out.txt"})
print(f"claimed {outcome.key} fresh={outcome.fresh}")
if outcome.fresh:
    open("out.txt","w").write("hello")
    os._exit(9)
PY
echo "exit $?"  # 9
continuum --json briefing --run-id codex_test | python -m json.tool | head -n 20
continuum --json resume codex_test | python -m json.tool | head -n 20
```

Measured: briefing silent 0.01 ms, resume 0.05 ms.

## Limitations (honest)

- **Bash-only today.** Codex hooks fire for `Bash`/`shell` only. `apply_patch` and MCP tools do not traverse them.
- **No dedicated PreCompact event.** Reuse SessionStart or run `continuum --json resume` manually.
- **Feature flag.** Without `[features] codex_hooks = true`, hooks are no-ops.

## Troubleshooting

If `cat .codex/hooks.json` is missing, you forgot the flag or restart.
