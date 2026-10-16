# Research Notes

Design notes and measurement studies written against the CONTINUUM codebase.
Each note states what it measured or proposed and what landed; the ones whose
proposal shipped point back to the mechanism that exists today.

| Note | What it covers | Status |
|:--|:--|:--|
| [Long-horizon gaps](long_horizon_gaps.md) | What weeks-long autonomy requires, from the METR time-horizon data and the self-conditioning literature, mapped against what CONTINUUM ships and lacks | Research note |
| [Months-scale web synthesis](WEB_SYNTHESIS.md) | Consolidation of the 2026-08-24 live arXiv sweep with the prior notes, on what it takes to run agents for weeks and months | Synthesis (2026-08-24) |
| [Auto-resume architecture](auto_resume_architecture.md) | The auto-resume handoff over MCP: optional `run_id`, `get_active_run`, and the open work on the session-start split | Core shipped, handoff via hooks |
| [Instant autonomous detection](instant_detection.md) | Detecting an interrupted run before the first model turn: SessionStart hook plus a precomputed `.continuum/resume.json` | Shipped (#83, #394) |
| [Task context](task_context.md) | Storing a structured plan alongside the run so a resumed session does not re-explore: the `PLAN_UPSERT` event and the plan projection | Proposal implemented (`SemanticState.plan`) |
| [Cross-agent recovery portability](cross_agent_portability.md) | The minimal portable subset of `RecoveryContract` so a contract sealed by one framework can be verified by another | Design spec |
| [Latency budget](latency_budget.md) | Measured support points (source-graph build, 100-dependency assess, full suite) and the budget they imply for recovery in the agent loop | Measured |
| [Human-gate minimization](human_gate_minimization.md) | Which of the 13 recovery-correctness scenarios truly need a human, grouped by their engine decision | Analysis of `phase6` runs |
| [Policy learning](policy_learning.md) | Whether ledger history can improve the recovery policy, and why it stays advisory | Findings; no auto-policy |
| [Confirm tax](confirm_tax.md) | Three designs for removing the extra `continuum_confirm` turn on every resume of a self-certified run plus a recommendation | Proposal, not implemented |
| [Token floor](token_floor.md) | Cutting the per-session token floor by slimming tool schemas for resume-only sessions | Proposal, not implemented |
