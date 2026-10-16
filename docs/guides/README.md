# Operator Guides

Step-by-step guides for the surfaces an operator configures and the seams an
agent plugs into. Each guide names the file it touches and stays focused on one
mechanism; the reference material in `docs/api/` documents the same surface
programmatically.

## Guides

- [Authority probes](authority-probes.md) - re-validating a consumed single-use
  credential with an operator-configured command so `continuum reconcile
  --authority` settles the block from the real world
- [Bring your own dashboard](bring-your-own-dashboard.md) - consuming the sealed
  resume contract, the content-addressed evidence stream, and chain attestation
  from your own control plane or UI
- [Constraint pinning](constraint-pinning.md) - recording standing session-start
  constraints as hash-only, verifiable events that survive compaction
- [Embed CONTINUUM in Claude Code](embed-claude-code.md) - copy-paste hooks that
  give a Claude Code session crash recovery, instant resume, and optional
  side-effect gating, no Python required
- [Embed CONTINUUM in Codex](embed-codex.md) - the same resume substrate wired
  under Codex, including the `codex_hooks` feature flag its engine needs
- [Enforcement](enforcement.md) - which boundaries make the advisory recovery
  verdict binding (the CLI exit code, the gate, the gateway) and which do not
- [Escalation policy](escalation-policy.md) - budgeting human attention by
  scoring, buffering, and capping `REQUIRES_REVIEW` interruptions in
  `.continuum/escalation.json`
- [Evidence-backed reconciliation](evidence-reconciliation.md) - settling an
  action left `STARTED` by a crash with the built-in probes, the ledger
  contradiction check, and eBPF collector output
- [Liveness watch](liveness-watch.md) - hearing silence as a signal: cadence
  contracts in `.continuum/liveness.json` evaluated by `continuum watch`
- [Memory mutation governance](memory_governance.md) - governing external
  memory-store writes as claimed, provenance-stamped, tenant-scoped, and
  erasable side effects
- [Reconciler plugins](reconciler-plugins.md) - in-process `ActionReconciler`
  plugins that settle uncertain side effects from an OTel span store, a
  provider API, a filesystem outbox, or a dead-letter table
- [Risk policy](risk-policy.md) - mapping external risk-signal names to recovery
  modes in `.continuum/risk-policy.json` so the engine weighs them at assess
  time
- [Validation rules](validation-rules.md) - registering domain staleness rules
  the environment cannot express, without patching `StateValidator`
- [Webhooks](webhooks.md) - pushing the `request_human` verdict out to
  configured receivers so a blocked run does not sit unnoticed

Each guide states its own status. The escalation policy, for example, is
implemented and tested but the engine does not read it yet (issues #1410 and
#1411 are its consumers), so installing one changes no behaviour today.
