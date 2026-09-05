# Claude Code hooks: SessionStart + PreCompact

Copy-paste recipes for wiring CONTINUUM into Claude Code's session lifecycle.

## What you get

- **SessionStart** injects the active run's recovery contract before the first model turn.
- **PreCompact** seals a checkpoint and verifies pinned constraints survive compaction.

SessionStart is read-only; PreCompact writes a checkpoint. Neither one fails its
host when there is nothing to act on: `briefing` exits 0 with no output at all
(it reads `.continuum/resume.json` before it would open SQLite), and `precompact`
exits 0 after printing `CONTINUUM: no active run; nothing to checkpoint before
compaction.`

## Install (one command)

```bash
continuum hooks install claude-code
```

This writes three entries to `.claude/settings.json`:

- `PostToolUse` on `Write|Edit|MultiEdit|NotebookEdit` → `continuum observe`
- `SessionStart` → `continuum briefing` (instant detection via `.continuum/resume.json`)
- `PreCompact` → `continuum precompact` (checkpoint at the compaction boundary; `--no-precompact` skips it and takes out an entry an earlier install wrote)

Verify:

```bash
cat .claude/settings.json | python -m json.tool
```

## Replace PreCompact with a read-only briefing (copy-paste)

`hooks install` already wires PreCompact to `continuum precompact`. Use this
instead if you want the compaction boundary to report without writing:

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
