# CONTINUUM durable agent

This project ships a CONTINUUM MCP server (`continuum-mcp`, registered in
`.mcp.json` and enabled in `.claude/settings.local.json`). Use it so your work
survives being interrupted: a run that is killed mid-task can be resumed by a
brand-new session with no memory of the old `run_id`.

## On session start, resume first

Before doing anything else, call `continuum_resume` with **no `run_id`**. It
returns the most recently active (interrupted) run, or `mode: "no_active_run"`
when there is nothing to resume.

- If a run is returned, immediately tell the user:

  > I found an interrupted run `<run_id>` at `<completed>/<total>` progress
  > (`<rationale>`). Resume where you left off?

  Then **stop and wait** for the user to say yes. Do not invent new work. If they
  agree, continue that same `run_id` with the CONTINUUM tools below.
- If `no_active_run`, start a new run with `continuum_record_progress(run_id,
  completed=0, total=N, goal=...)` and proceed.

This is what makes a kill-and-reopen recoverable in milliseconds: the new session
detects the old run on its very first tool call.

## While working, record every step

After each meaningful unit of work:

- `continuum_record_progress(run_id, completed, total, goal=...)` — call often;
  it is cheap and makes progress durable.
- `continuum_checkpoint(run_id)` — call at meaningful milestones so a resumed
  session knows exactly where it stopped.

## External side effects go through the ledger

Before performing anything with effects outside this session (deploy, send,
write a file the user cares about), route it through
`continuum_intercept_action` and, once done, `continuum_complete_action` (or
`continuum_fail_action`). This is what guarantees a side effect is never
performed twice across a resume.

## When done

Record final progress with `completed == total`. The run then drops out of
`continuum_resume`'s active set and will not be offered for resume again.
