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

## While working, record every step (cheap, no extra files)

After each meaningful unit of work, call:

- `continuum_record_progress(run_id, completed, total, goal="<the task>")`
- `continuum_checkpoint(run_id)`

That is all the durable state CONTINUUM needs. Never spawn exploration or write
side files to "track" the task — the goal string and the progress counter are
enough for a resumed session to pick up the next unit.
