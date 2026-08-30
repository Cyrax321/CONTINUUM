# Claude Code hooks: SessionStart + PreCompact

Copy-paste recipes for wiring CONTINUUM into Claude Code's session lifecycle.

## What you get

- **SessionStart** injects the active run's recovery contract before the first model turn.
- **PreCompact** verifies pinned constraints survive compaction.

Both hooks are read-only and silent when no run is active.

## Install (one command)

```bash
continuum hooks install claude-code
```

This writes two entries to `.claude/settings.json`:

- `PostToolUse` on `Write|Edit|MultiEdit|NotebookEdit` → `continuum observe`
- `SessionStart` → `continuum briefing` (instant detection via `.continuum/resume.json`)

Verify:

```bash
cat .claude/settings.json | python -m json.tool
```

## Add PreCompact for constraint verification (copy-paste)

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/.venv/bin/continuum briefing --json"
          }
        ]
      }
    ]
  }
}
```

Replace `/absolute/path/to/.venv/bin/continuum` with `which continuum`.

For an explicit `resume --json` variant (pairs with #394):

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
    ],
    "PreCompact": [
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

## Hard-kill test (real process)

```bash
python - << 'PY'
import os
from continuum import SQLiteStorage, Run, ActionLedger
from continuum.events import EventType
store = SQLiteStorage("continuum.db")
store.create_run(Run(run_id="harness_test", goal="hook test"))
store.append_event("harness_test", EventType.RUN_STARTED, {"goal": "hook test"})
ledger = ActionLedger(store, "harness_test")
outcome = ledger.claim("demo.write", {"path": "out.txt"})
print(f"claimed {outcome.key} fresh={outcome.fresh}")
if outcome.fresh:
    open("out.txt","w").write("hello")
    os._exit(9)
PY
echo "exit code $?"  # 9
continuum --json briefing --run-id harness_test | python -m json.tool | head -n 20
continuum --json resume harness_test | python -m json.tool | head -n 20
# Expect: mode request_human, safe false
```

Measured: briefing silent 0.01 ms, resume 0.05 ms with resume.json, well under 1s.

## Constraint verification

`continuum resume --json` includes `pins` from `SemanticState`. The PreCompact hook sees the same contract.

## Troubleshooting

`CONNECTION_CLOSED` is PATH resolution, not hook failure. See `docs/api/mcp.md#troubleshooting`.
