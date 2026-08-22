# CONTINUUM durable agent

This project ships a CONTINUUM MCP server (`continuum-mcp`, in `.mcp.json`). Use
it so your work survives interruption: a run killed mid-task can be resumed by a
new session with no memorized id and no manual confirmation. That is the only
extra step — keep everything else normal and fast.

## On session start, detect and ask

Your **first action** is to call `continuum_resume` with **no `run_id`**.

- If an in-progress run is returned: show its `run_id`, progress
  (`completed/total`) and `goal`, then **ask the user**:

  > I found an unfinished task in CONTINUUM — run `<run_id>` at `<c>/<t>`:
  > "<goal>". Resume it, or start a new task?

  Then wait.
  - **resume**: if `mode == "request_human"`, call `continuum_confirm(run_id)`
    first, then continue from the recorded progress.
  - **new**: start a fresh run.
- If `no_active_run`: just do what the user asked.

Do **not** read or write any task file — the task is the run's `goal`, which
`continuum_resume` returns, so a resumed session already knows what to continue.

For instant detection without waiting for a user message, install
`scripts/session_start_resume.sh` as a SessionStart hook. It runs
`continuum resume --json` out of band and injects the banner before the model
starts, so no inference is spent on detection.

When `continuum_resume` returns `tail_evidence`, use that tail to match
style for the next section. Do not re read the whole file unless the
validator reports the file as stale. The file stays as ground truth for
content, the tail is just a cache for style.

## While working, progress is recorded automatically

Durability is handled by an auto checkpoint hook that calls
`CheckpointManager.maybe_checkpoint` after each file write. You do not need to
call `continuum_checkpoint` after every unit. Just do the work; the hook will
checkpoint when the policy says the state meaningfully changed. If you finish a
major milestone you may call `continuum_checkpoint` explicitly, but it is not
required per section.

Record progress with `continuum_record_progress` as you complete units. That is
all the durable state CONTINUUM needs. Never spawn exploration or write side
files to track the task.
