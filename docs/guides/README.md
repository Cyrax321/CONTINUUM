# CONTINUUM Guides

Task guides. Each one starts from a problem you will actually hit and ends with
a mechanism that is already shipped. Read the one that matches what broke.

The API surface is in [docs/api/README.md](../api/README.md), worked examples in
[docs/recipes/README.md](../recipes/README.md), and the mechanism behind one of
these in [docs/concepts/README.md](../concepts/README.md).

## Enforcement and review

- [Enforcement](enforcement.md) - make the advisory recovery verdict binding, and the three
  seams (hooks, gate, gateway) that can refuse a call the library only advises against.
- [Webhook out](webhooks.md) - push a delivery when a run parks on `request_human`, so the
  human does not have to be polling for it.
- [Escalation policy](escalation-policy.md) - budget reviewer attention across a weeks-long run
  instead of interrupting a human for every decision the ledger cannot settle.

## Reconciling side effects

- [Evidence-backed reconciliation](evidence-reconciliation.md) - settle an action left
  STARTED by a crash from observation of the outside world, not from what the agent remembers.
- [Reconciler plugins](reconciler-plugins.md) - write and register your own probes so an
  uncertain effect settles without a human.

## Validity, risk, and silence

- [Validation rules](validation-rules.md) - teach the validator what "stale" means in your
  domain, and let the answer propagate along the dependency graph.
- [Risk policy](risk-policy.md) - map external risk signals (latency anomalies, judge flags,
  rollout guards) onto recovery modes.
- [Liveness watch](liveness-watch.md) - tell a resting run apart from a wedged one by declaring
  how long each phase may stay quiet.
- [Authority probes](authority-probes.md) - keep a consumed single-use authority blocked until
  an operator-configured command confirms the world moved on.

## State and memory

- [Constraint pinning](constraint-pinning.md) - make session-start standing constraints
  first-class, verifiable events so they survive briefing and compaction.
- [Memory mutation governance](memory_governance.md) - treat writes to an external memory store
  as claimed, provenance-stamped, tenant-scoped side effects.
- [Bring your own dashboard](bring-your-own-dashboard.md) - sit CONTINUUM under your own control
  plane and render run state, gate launches, and prove history without trusting the UI.

## Wiring it into a harness

- [Embed CONTINUUM in Claude Code](embed-claude-code.md) - copy-paste SessionStart and
  PreToolUse hooks for crash recovery, instant resume, and constraint-aware gating.
- [Embed CONTINUUM in Codex](embed-codex.md) - the same resume contract under Codex's
  feature-flagged hook engine.
