# Unscripted autonomous agent test (issue #6)

This directory holds a reproducible harness for issue #6: an *unscripted* run of
an independent LLM agent (Claude Code, the same CLI already wired to this project
via `.mcp.json`) against the `continuum-mcp` server, with no step-by-step
instructions. We record the tool-call sequence so we can verify the agent
autonomously checkpoints, validates, and resumes.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-...      # required; the agent needs a real LLM
bash benchmarks/unscripted_resume.sh
```

The script:

1. Creates a fresh `continuum.db` and an MCP-connected Claude Code session.
2. Phase 1: hands the agent `task.md` (a goal, not a script) and lets it drive
   the `continuum_*` MCP tools however it sees fit.
3. Phase 2: opens a *fresh* session on the same `RUN_ID` and asks it to resume,
   checking whether it calls `continuum_resume` before acting.
4. Records both the agent's tool calls (`run1.stream.jsonl`, `run2.stream.jsonl`)
   and CONTINUUM's own view of what happened (`agent_trail.txt`, captured from
   the event log, which is the ground-truth record).

The deliverable for #6 is the captured sequence plus the deviations noted in the
issue, not a passing unit test.
