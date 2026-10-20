# CONTINUUM Research Notes

Working notes, briefs, and studies behind the mechanisms that ship in the
package. Each one states the question, the options weighed, and whether the
answer became code. Read these when you want to know why a mechanism is shaped
the way it is, or before proposing a different one.

Guides that show the shipped mechanism in use are in
[docs/guides/README.md](../guides/README.md).

## Start here

- [Months-scale agent reliability: live web synthesis](WEB_SYNTHESIS.md) - the 2026-08-24
  arXiv sweep, consolidated with the nine notes below and the current implementation.
- [Long-horizon runs: what weeks-long autonomy needs](long_horizon_gaps.md) - the research read
  against what CONTINUUM ships and what it still lacks.

## Resume and recovery

- [Auto resume integration: architecture brief](auto_resume_architecture.md) - how the resume
  handoff fits together, written for an engineer with no prior context.
- [Instant autonomous detection](instant_detection.md) - shipped; kept for the history of how
  session-start detection and `.continuum/resume.json` arrived.
- [Recovery latency budget](latency_budget.md) - the derived budget, from measured support
  points, for keeping recovery off the critical path of the agent loop.

## Human review

- [Human gate minimization study](human_gate_minimization.md) - which Phase 6 scenarios truly
  need a human, and which do not.
- [Confirm tax on every resume](confirm_tax.md) - the `continuum_confirm` round trip a
  self-certified MCP run pays, and the options for removing it.

## Portability and policy

- [Cross agent recovery portability](cross_agent_portability.md) - the minimal portable subset
  of `RecoveryContract` that one framework can seal and another can verify.
- [Recovery policy learning from ledger history](policy_learning.md) - whether past decisions in
  the ledger can improve the hand-coded recovery policy.

## Cost

- [Reduce per session token floor](token_floor.md) - why a short resume check pays for the
  system prompt and all thirteen MCP tool schemas.
- [Durable lossless task context](task_context.md) - why a free-text goal plus counters is
  lossy when a resumed session has to re-explore.
