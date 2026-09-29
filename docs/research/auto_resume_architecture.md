# Auto Resume Integration: Architecture Brief

This brief ties together the auto resume handoff and the concrete problems solved in issues 83 to 88. It is written so an engineer with no prior knowledge can understand the integration and the remaining open work.

## What auto resume is

An agent works on a long task via `continuum_record_progress` and `continuum_checkpoint`. The run stays in a non terminal state until completed. A fresh session calls `continuum_resume` with no `run_id`, which uses `Storage.get_active_run` to find the interrupted run, returns its `goal` and progress, and asks the user to resume or start new. This is wired via `CLAUDE.md` today, not via code changes to the client.

## Current state

- `continuum_resume` accepts optional `run_id` and returns `goal` so resume is self describing.
- `Storage.get_active_run` finds the latest non terminal run.
- Task lives in `Run.goal`, not in a separate file.
- `CLAUDE.md` is slimmed to detect and ask plus checkpoint only.
- Authorization allowlist includes `claude-code`.

These reduce avoidable overhead but do not change the fundamental cost structure.

## Problems and where they landed

- **P2 Instant detection** Issue 83. A session only acts after a user message, so detection costs a message plus inference plus tool call. Proposal is a `SessionStart` hook that runs `continuum --json resume` out of band and injects a pre rendered prompt, plus wrapper and precomputed banner paths. See `docs/research/instant_detection.md`.

- **P3 Confirm tax** Issue 84. Self certified runs return `request_human` until `REVIEW_CONFIRMED`. Proposal is scoped confirm that only clears `REQUIRES_REVIEW` due to `Origin.EXTERNAL_AGENT`, or same client auto confirm with an audit record. See `docs/research/confirm_tax.md`.

- **P4 Lossy task context** Issue 85. Free text goal plus counters is lossy for non sequential tasks. Proposal is a structured `PLAN_UPSERT` event and `SemanticState.plan` reusing `PlanStep` and `depends_on`. See `docs/research/task_context.md`.

- **P6 Checkpoint discipline** Issue 86. Checkpoint granularity depends on the model calling the tool. Solution is `make_auto_checkpoint_hook` in `src/continuum/hooks.py:1` wrapping `CheckpointManager.maybe_checkpoint` so a hook after each assistant turn can checkpoint when the policy says to. Tested in `tests/test_auto_checkpoint_hook.py:1`.

- **P7 Token floor** Issue 88. Every session pays for the system prompt and ten tool schemas. Proposal is a slim resume check tool subset and lazy exposure. See `docs/research/token_floor.md`.

- **P5 Cold start** Observed `continuum-mcp` reporting `ready: false` at session start. Remediation is to ensure the server is registered in `.mcp.json` and `.claude/settings.local.json` and to defer adapter imports. The server now recovers from orphaned WAL sidecars.

## What remains

- Implement `PLAN_UPSERT` and the scoped confirm flag as code, beyond the research notes.
- Wire the `SessionStart` hook in the project and measure the latency delta.
- Decide on the token floor slim subset and update `mcp/server.py` `tools/list`.

No implementation is done in this brief. It is a pointer to the sub issue docs and the hook that already ships.
