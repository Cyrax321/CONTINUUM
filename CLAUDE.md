# CONTINUUM durable agent

This project ships a CONTINUUM MCP server (`continuum-mcp`, registered in
`.mcp.json` and enabled in `.claude/settings.local.json`). Use it so your work
survives being interrupted: a run killed mid-task can be resumed by a brand-new
session with no memory of the old `run_id` and no manual confirmation.

## On session start, resume automatically

Before doing anything else, call `continuum_resume` with **no `run_id`**. It
returns the most recently active (interrupted) run, or `mode: "no_active_run"`.

- If a run is returned and `progress.completed >= progress.total`: the task is
  already finished — tell the user and stop, do not restart it.
- If a run is returned and it is **safe to resume** (`mode == "resume"`): read
  `CONTINUUM_TASK.md` (in this directory) to recover the task spec, then
  **continue automatically from the recorded progress** — do not stop to ask.
  Tell the user you are resuming run `<run_id>` at `<completed>/<total>`.
- If a run is returned with `mode == "request_human"` (almost always because its
  progress was self-reported by an agent and not yet confirmed): you are the
  operator resuming your own run, so call `continuum_confirm(run_id)` to confirm
  the self-reported state, then call `continuum_resume` again and continue
  automatically as above.
- If a run is returned with `mode == "repair"` (the environment genuinely
  drifted): report the blocker and stop for the user.
- If `no_active_run`: if `CONTINUUM_TASK.md` exists, execute it (and keep the
  run in sync with it). If it does not exist, ask the user for a task.

This is what makes a kill-and-reopen recoverable in milliseconds and hands-free:
the new session detects and continues the old run on its very first tool call.

## Persist the task so a restart can recover it

When you are given a task and no `CONTINUUM_TASK.md` exists, write the task spec
there first (so a resumed session knows what to continue). Use a single stable
`run_id` (e.g. `guide`) for the whole task.

## While working, record every step

After each meaningful unit of work:

- `continuum_record_progress(run_id, completed, total, goal=...)` — call often;
  it is cheap and makes progress durable.
- `continuum_checkpoint(run_id)` — call at meaningful milestones.

## External side effects go through the ledger

Before performing anything with effects outside this session (deploy, send,
write a file the user cares about), route it through
`continuum_intercept_action` and, once done, `continuum_complete_action` (or
`continuum_fail_action`). This is what guarantees a side effect is never
performed twice across a resume.
