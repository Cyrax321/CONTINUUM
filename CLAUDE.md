# CONTINUUM durable agent

This project ships a CONTINUUM MCP server (`continuum-mcp`, registered in
`.mcp.json` and enabled in `.claude/settings.local.json`). Use it so your work
survives being interrupted: a run killed mid-task can be resumed by a brand-new
session with no memory of the old `run_id` and no manual confirmation.

## On session start, decide new task vs resume

Before doing anything else, call `continuum_resume` with **no `run_id`** to learn
the active run. Then act on the user's intent **this session**:

- If the user's first message **describes new work to do** (a task): it
  supersedes any interrupted run. Overwrite `CONTINUUM_TASK.md` with that task,
  then **start a fresh run** (record_progress with `completed=0`) and execute it,
  checkpointing as you go. Do **not** continue the interrupted run.
- If the user's first message only asks to **resume / continue / recover** (no new
  task described): continue the active run —
  - if `progress.completed >= progress.total`: finished — tell the user and stop.
  - if `mode == "resume"`: read `CONTINUUM_TASK.md` to recover the spec, then
    **continue automatically from the recorded progress** — do not stop to ask.
    Tell the user you are resuming run `<run_id>` at `<completed>/<total>`.
  - if `mode == "request_human"` (almost always self-reported progress not yet
    confirmed): you are the operator resuming your own run, so call
    `continuum_confirm(run_id)`, then `continuum_resume` again and continue.
  - if `mode == "repair"` (the environment genuinely drifted): report the blocker
    and stop.
- If `no_active_run` and the user gave a task: start fresh as above.

This is what makes a kill-and-reopen recoverable in milliseconds and hands-free:
the new session detects and continues the old run on its very first tool call.

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
