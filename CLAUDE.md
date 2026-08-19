# CONTINUUM durable agent

This project ships a CONTINUUM MCP server (`continuum-mcp`, registered in
`.mcp.json` and enabled in `.claude/settings.local.json`). Use it so your work
survives being interrupted: a run killed mid-task can be resumed by a brand-new
session with no memory of the old `run_id` and no manual confirmation.

## On session start, detect and ask

Your very first action, before any other work, is to call `continuum_resume`
with **no `run_id`** to check for an interrupted run.

- If a run is returned and `progress.completed >= progress.total`: tell the user
  the saved task is already complete, and ask whether to start a new one.
- If a run is returned with `mode == "repair"` (the environment genuinely
  drifted): report the blocker and ask how to proceed.
- If a run is returned and it is still in progress: **stop and ask the user**.
  Surface what you found — `run_id`, the progress (`completed/total`), and a
  one-line summary of the task (read `CONTINUUM_TASK.md`, or the run's `goal`) —
  then ask:

  > I found an unfinished task saved in CONTINUUM — run `<run_id>` at
  > `<completed>/<total>`: "<task summary>". Resume it, or start a new task?

  Then **wait** for the answer.
  - If the user says resume / yes: if `mode == "request_human"` (almost always
    self-reported progress not yet confirmed), call `continuum_confirm(run_id)`
    first, then read `CONTINUUM_TASK.md` and **continue automatically from the
    recorded progress**. Tell the user you are resuming at `<completed>/<total>`.
  - If the user says new / no: overwrite `CONTINUUM_TASK.md` and **start a fresh
    run** (record_progress with `completed=0`).
- If `no_active_run`: proceed with whatever the user asked. If they gave you a
  task, start a fresh run (overwrite `CONTINUUM_TASK.md` with it first).

This is what makes a kill-and-reopen recoverable in milliseconds: the new session
detects the interrupted run on its first tool call and offers to continue.

## Persist the task so a restart can recover it

Whenever you are given a task, write (or overwrite) `CONTINUUM_TASK.md` in this
directory with that task spec first, then execute it. This way a restarted
session that resumes can read `CONTINUUM_TASK.md` to recover what remains to be
done — an explicit task from the user always overrides the file. Use a single
stable `run_id` (e.g. `guide`) for the whole task.

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
