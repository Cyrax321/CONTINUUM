# CONTINUUM: Project Document

**Verifiable Semantic Recovery for Long-Running AI Agents**

- **Version:** 0.1.2
- **License:** Apache 2.0
- **Language:** Python 3.11+
- **Repository:** https://github.com/Cyrax321/CONTINUUM.git
- **PyPI:** `pip install continuum-agent`
- **Website:** https://continuum-nu-six.vercel.app/

---

## Table of Contents

1. [Overview](#overview)
2. [Core Invariant](#core-invariant)
3. [Architecture](#architecture)
4. [Five Integration Seams](#five-integration-seams)
5. [Complete Feature List](#complete-feature-list)
6. [Core Data Layer](#core-data-layer)
7. [Action Ledger](#action-ledger)
8. [Recovery Engine](#recovery-engine)
9. [Checkpointing](#checkpointing)
10. [Gate and Enforcement](#gate-and-enforcement)
11. [MCP Server](#mcp-server)
12. [CLI](#cli)
13. [Dashboard](#dashboard)
14. [Framework Adapters](#framework-adapters)
15. [Security](#security)
16. [Storage Architecture](#storage-architecture)
17. [Benchmarks and Testing](#benchmarks-and-testing)
18. [Installation](#installation)
19. [Quick Start](#quick-start)
20. [Usage Examples](#usage-examples)
21. [Roadmap](#roadmap)
22. [Limitations](#limitations)
23. [What CONTINUUM Is Not](#what-continuum-is-not)
24. [Related Work](#related-work)
25. [Contributors](#contributors)

---

## Overview

CONTINUUM is a verifiable semantic recovery layer for long-running AI agents. It provides:

- **Semantic checkpoints** (not conversation dumps): a compact, versioned representation of what the agent needs to continue
- **Idempotent action ledger**: refuses duplicate external side effects; surfaces uncertain ones for reconciliation
- **Environment revalidation**: every checkpoint component verified against the current environment before resume
- **Provenance-aware state**: every fact traces to its origin; agent-reported progress is never self-certifying
- **Deny-by-default MCP server**: 13 tools over stdio with authorization

The core abstraction: `semantic state + environment validation + action reconciliation = safe recovery`.

### The Problem It Solves

Modern AI agents run long tasks (hundreds of LLM calls, tool invocations, file and database writes). When they crash, the usual response is to replay everything from scratch, which duplicates work, duplicates side effects, wastes tokens, and loses decisions.

CONTINUUM asks: can an agent resume from a compact semantic representation of its task state while independently verifying that state is still valid in the current environment?

---

## Core Invariant

> **Every fact carries its origin, and trust is earned, never assumed.**

An agent that runs for weeks must not lose work when its context is lost, and it must not waste tokens, cost, or fire a tool twice.

---

## Architecture

### System at a Glance

```
Claude Code / Gemini CLI / Codex / LangGraph / LangChain / OpenAI SDK / Any HTTP / Any OTel app
                        |
                   5 integration seams
                        |
              One durable hash-chained log
         (provenance-tagged, exactly-once)
                        |
         Recovery + Dashboard + CLI + TUI
```

Any harness plugs into the same hash-chained log. The same run can be written by Claude Code, resumed by LangGraph, inspected by the CLI, and approved on the dashboard. No framework cooperation is required.

### Three Guarantees

1. **No self-certification.** Agent reported state is `EXTERNAL_AGENT` and degrades to `REQUIRES_REVIEW` until a human `REVIEW_CONFIRMED`. Only trusted writers produce `DETERMINISTIC` state.
2. **Side effects require claims.** Every external effect is claimed in an idempotent ledger before it fires. Unclaimed effects are blocked at the boundary, duplicates are refused, uncertain outcomes raise for reconciliation.
3. **Recovery verifies against reality.** Resume checks file digests, dependency versions, and model identity before saying safe. Staleness propagates `dependency -> evidence -> finding -> decision` plus `PlanStep.depends_on` so only affected steps repair.

### Recovery Decision Tree

```
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

The engine takes the **maximum** severity across all signals (order-independent, deterministic). Safety never loses to convenience.

---

## Five Integration Seams

| Seam | How to Connect | What It Gives You |
|:--|:--|:--|
| **1. In-process** | `GenericAgentAdapter.intercept_action(...)` and `wrap_tool(key_fn=...)` on LangChain, LangGraph, OpenAI Agents SDK | Python frameworks, trusted writes |
| **2. MCP server** | `continuum-mcp` 13 tools over stdio | Any MCP-capable client, 3 read-only + 10 mutating, allowlist `CONTINUUM_MCP_MUTATING_CLIENTS` |
| **3. CLI lifecycle hooks** | `continuum hooks install claude-code --with-gate` (also `gemini` and `codex`) | Coding CLIs: `SessionStart` briefing, `PostToolUse` observe, `PreToolUse` gate; no CLAUDE.md needed |
| **4. Enforcing HTTP gateway** | `continuum gateway --port 8765` with `.continuum/gateway.json` | Any language, any outbound HTTP must have a claim, gateway settles from real status code |
| **5. OpenTelemetry bridge** | `make_span_processor(storage)` | Any traced app, spans become `TOOL_COMPLETED` evidence |

Thin hook surfaces for CrewAI, AutoGen, Pydantic AI live in `adapters/thin.py` with no SDK required.

---

## Complete Feature List

### Core Data Layer

| Feature | Module | Details |
|:--|:--|:--|
| Hash-chained event log | `events.py` | 52 event types, per-run sequencing, `verify()` with `trusted_through`, tamper evidence |
| Semantic state projection | `state/semantic.py` | Pure fold over event prefix, reproducible, prefix-closed |
| State validation | `state/validator.py` | Staleness propagation: `dependency -> evidence -> finding -> decision` |
| State diffing | `state/diff.py` | `diff_states()`, `render_diff()` |
| Versioning | `state/versioning.py` | `state_fingerprint()`, `canonical_state_json()` |
| Provenance tracking | `provenance/graph.py`, `provenance_map.py` | `Origin` enum (DETERMINISTIC, HUMAN, LLM, EXTERNAL_AGENT, IMPORTED, EXTERNAL_MONITOR), `Provenance` model |
| Constraint pins | `models.py` | SHA-256 pinned constraints, never stored as plaintext |

### Action Ledger

| Feature | Module | Details |
|:--|:--|:--|
| Idempotent claim/complete | `actions/ledger.py` | Two-phase protocol, `UnknownSideEffect` raised for uncertain outcomes |
| Idempotency key derivation | `actions/idempotency.py` | Stable key + path canonicalization + token-based identity fallback |
| Action index | `storage/actionindex.py` | Cross-run idempotency lookups (indexed reads, not full scans) |
| Retry budgets | `budgets.py` | Per-action-type attempt caps enforced at claim time |
| Consumed grants | `actions/grants.py` | Single-use authority tracking, `GRANT_DENIED` on reuse |
| Authority reconciliation | `actions/authority.py` | External probe validation of consumed authorities |
| Compensating transactions | `actions/ledger.py` | `compensate()` method, `ACTION_COMPENSATED` events |
| Reconciler probes | `reconcilers.py` | `.continuum/reconcilers.json` registry for automatic settlement |

### Recovery Engine

| Feature | Module | Details |
|:--|:--|:--|
| Recovery engine | `recovery/engine.py` | Reduces validation + ledger + checkpoint signals to one `RecoveryMode` |
| Sealed contract | `recovery/contract.py` | Deterministic, integrity-sealed, versioned (`CONTRACT_VERSION=1`) |
| Repair planning | `recovery/planner.py` | `plan_repairs()` generates executable repair steps |
| Family rollup | `recovery/family.py` | Parent/child hierarchy, worst-state composition |
| Fork semantics | `recovery/fork.py` | Divergent continuations branch into child runs |
| Informed retry | `recovery/summary.py` | Engine-authored failure summaries injected into resumes |
| Briefing curation | `recovery/briefing_curation.py` | Provenance-labeled resume context with quarantine |
| Risk policy | `recovery/risk.py` | `RISK_OBSERVED` ingestion, risk-to-mode mapping |
| Liveness watchdog | `liveness.py` | `continuum watch`, silence detection |
| Recovery ledger | `recovery/ledger.py` | Append-only hash-chained audit of recovery decisions |
| Webhook notifications | `recovery/notify.py` | HMAC-SHA256 signed webhook delivery |
| Trajectory reports | `analysis/trajectory_report.py` | Deterministic reports from archived history |
| Attempt lessons | `recovery/` (via `ATTEMPT_LESSON` events) | Durable falsification lessons from failed attempts |

### Checkpointing

| Feature | Module | Details |
|:--|:--|:--|
| Policy-driven checkpoints | `checkpoint/policy.py` | Manual, interval, event, semantic, context-pressure, hybrid policies |
| Checkpoint manager | `checkpoint/manager.py` | Create/restore with integrity sealing, `RECOVERY` anchors |
| Recovery context | `checkpoint/context.py` | Bounded recovery context with protected sections |
| Atomic rewind | `checkpoint/rewind.py` | Content-addressed snapshot store, dual-state rewind |
| Log compaction | `storage/compaction.py` | Pre-anchor prefix archived, live log bounded |

### Gate and Enforcement

| Feature | Module | Details |
|:--|:--|:--|
| Pre-tool-use gate | `gate.py` | Allow/deny against ledger claims, `.continuum/gate.json` config |
| Enforcing HTTP gateway | `gateway.py` | Claim-before-fire proxy, route prefix enforcement, fail-closed |
| Replay guard | `replayguard.py` | `evaluate`, `protected_call`, `langgraph_protected_node` |
| Observation hooks | `hooks.py` | Auto-checkpoint hooks, file-derived progress, async variants |

### MCP Server (13 Tools)

| Feature | Module | Details |
|:--|:--|:--|
| MCP server | `mcp/server.py` | 13 tools: `record_progress`, `checkpoint`, `validate`, `resume`, `intercept_action`, `complete_action`, `fail_action`, `reconcile_action`, `list_actions`, `confirm`, `compensate_action`, `record_plan`, `providers` |
| Authorization | `mcp/authz.py` | Deny-by-default, 4-tier policy resolution, token auth (`CONTINUUM_MCP_TOKEN`), per-caller secrets |
| MCP install/remove | `mcp/install.py` | Cross-platform registration in `.mcp.json` |
| MCP doctor | `mcp/doctor.py` | Diagnostic commands |

### CLI (49 Commands)

| Feature | Module | Details |
|:--|:--|:--|
| CLI | `cli/main.py` | 49 argparse commands, exit codes as safety contract |
| Exit codes | `cli/exitcodes.py` | Only verified-safe runs exit 0 |
| TUI | `tui/app.py` | Terminal UI with tree view, recovery view |

### Dashboard

| Feature | Module | Details |
|:--|:--|:--|
| Web dashboard | `dashboard/app.py` | `continuum dashboard` command |
| HITL surface | `dashboard/hitl.py` | Confirm/reconcile/complete buttons with audit parity |

### Adapters (9 class adapters + thin hooks)

| Adapter | Module | Status |
|:--|:--|:--|
| Generic Python | `adapters/generic.py` | Production-ready |
| Filesystem sandbox | `adapters/filesystem.py` | Production-ready |
| Python in-process | `adapters/python_inproc.py` | Production-ready |
| Container (Docker) | `adapters/container.py` | Guarded skip |
| Browser (Playwright) | `adapters/browser.py` | Guarded skip |
| Kubernetes | `adapters/kubernetes.py` | Guarded skip |
| OpenAI Agents SDK | `adapters/openai.py` | Experimental, live-model tested |
| LangGraph | `adapters/langgraph.py` | Experimental, live-model tested |
| LangChain | `adapters/langchain.py` | Experimental, live-model tested |
| CrewAI (thin) | `adapters/thin.py` | SDK-free hooks |
| AutoGen (thin) | `adapters/thin.py` | SDK-free hooks |
| Pydantic AI (thin) | `adapters/thin.py` | SDK-free hooks |
| LangGraph store | `adapters/langgraph_store.py` | `BaseCheckpointSaver` implementation |

### Security

| Feature | Module | Details |
|:--|:--|:--|
| Ed25519 attestation | `security/attestation.py` | `continuum attest` signs chain head |
| Hashing | `security/hashing.py` | `stable_hash()`, `make_id()`, `canonical_sanitize()` |
| Trust gate | `security/trust_gate.py` | Trust boundary enforcement |
| Lineage tokens | `security/lineage.py` | Token-based lineage verification |
| Constraint validation | `security/constraints.py` | Constraint ID/charset validation |

### Benchmarks

| Feature | Module | Details |
|:--|:--|:--|
| CONTINUUM-Bench | `benchmark/`, `benchmarks/` | 5 crash scenarios + argument drift + 14-scenario recovery suite + 7 fault injection |
| Horizon benchmark | `benchmarks/horizon/` | 5 scenarios, published accuracy 0.8 |
| Nightly bench CI | `.github/workflows/bench-nightly.yml` | Automated nightly runs with `--publish` |

---

## Core Data Layer

### Event Log (`events.py`)

- Append-only, hash-chained log with 52 event types
- Per-run sequencing with `UNIQUE(run_id, sequence)` constraint
- `verify()` reports `trusted_through` so a partially tampered run can still be recovered up to its last good event
- `Event.source` records who asserted a fact, captured at write time and included in `content()` so it is signed

### State Projection (`state/semantic.py`)

- Pure fold over an event prefix
- Reproducible and prefix-closed
- The projector propagates `event.source` instead of hardcoding origin
- `Goal` and `Progress` carry provenance

### State Validation (`state/validator.py`)

- Checks state against the current environment
- Staleness propagates `dependency -> evidence -> finding -> decision`
- `PlanStep.depends_on` cascades taint to a fixpoint
- Self-certified components marked `REQUIRES_REVIEW`

### Provenance

- `Origin` enum: `DETERMINISTIC`, `HUMAN`, `LLM`, `EXTERNAL_AGENT`, `IMPORTED`, `EXTERNAL_MONITOR`
- Progress is cumulative, so the weakest contributor wins: a trusted event appended after an agent's self-report does not launder the running total

---

## Action Ledger

### Two-Phase Protocol

1. `continuum_intercept_action` claims the ledger entry, answers "may I?"
2. The caller performs the effect
3. `continuum_complete_action` records the outcome

Between steps 1 and 3 the ledger holds a `STARTED` record. A caller that crashes or never reports back leaves the action uncertain, and recovery refuses to resume until it is reconciled. This is intended: an unreported effect is indistinguishable from a completed one.

### Idempotency

- Stable key derivation: `invoice:INV-001` makes two attempts the same action regardless of argument shape
- Path canonicalization: normalizes path-like arguments before hashing
- Token-based identity fallback: recognizes already-recorded actions by shared identity tokens when no explicit key is provided
- Cross-run action index for indexed reads, not full-log scans

### Retry Budgets (`budgets.py`)

- Per-action-type attempt caps enforced at claim time
- Agents see remaining attempts

### Consumed Grants (`actions/grants.py`)

- Single-use authority references marked spent at terminal status
- Reuse after restore is refused (`GRANT_DENIED`)
- Defends the checkpoint-restore path against Authority Resurrection

---

## Recovery Engine

### Seven Recovery Modes

`RESUME`, `REPAIR_AND_RESUME`, `REPLAN`, `WAIT`, `REQUEST_HUMAN`, `ROLLBACK`, `ABORT`

The engine reduces validation, ledger, and checkpoint signals to one `RecoveryMode`. Takes the **maximum** on a severity ordering, so the most cautious signal wins regardless of evaluation order.

### Sealed Contract (`recovery/contract.py`)

- Deterministic, integrity-sealed, versioned (`CONTRACT_VERSION=1`)
- Contains: recovery status and `safe`, verified/invalidated components, executable `human_steps` (exact shell to run), post-checkpoint observations disk-checked, pinning drift, family aggregation

### Repair Planning (`recovery/planner.py`)

- `plan_repairs()` generates executable repair steps
- Marks reconcile steps `requires_human` when strict mode is on

### Family Rollup (`recovery/family.py`)

- Parent/child hierarchy
- Parent resume composes family worst state
- Uncertain child blocks parent

### Fork Semantics (`recovery/fork.py`)

- Divergent continuations branch into child runs with fresh authority

### Informed Retry (`recovery/summary.py`)

- Engine-authored failure summaries injected into post-recovery resumes
- Not raw history

### Briefing Curation (`recovery/briefing_curation.py`)

- Provenance-labeled resume context with quarantine
- Stale/invalidated evidence quarantined with reasons

### Recovery Ledger (`recovery/ledger.py`)

- Append-only, hash-chained audit of recovery decisions
- `verify` reports the last trusted index (tamper-evident)
- `compact` drops old entries while preserving anchors and re-sealing the chain
- `record_gate`/`pending_gate` persist human-in-the-loop decisions
- `requires_human` enforces an attempt budget
- `reconcile` detects state-vs-ledger drift
- Optional `LeaseCoordinator` for cross-process safety

### Webhook Notifications (`recovery/notify.py`)

- HMAC-SHA256 signed webhook delivery

---

## Checkpointing

### Policies (`checkpoint/policy.py`)

- Manual
- Interval
- Event
- Semantic
- Context-pressure
- Hybrid

### Manager (`checkpoint/manager.py`)

- Create/restore with integrity sealing
- `RECOVERY` anchors (`checkpoint_on_recovery`)
- `last_recovery_anchor` lookup
- `prune` (keeps newest + anchors)
- `Storage.delete_checkpoint` (SQLite + Postgres)
- `StateCheckpoint.reason` is stored

### Recovery Context (`checkpoint/context.py`)

- Bounded recovery context with protected sections
- Never-dropped sections: `CURRENT GOAL`, `VERIFIED PROGRESS`, `STALE STATE, DO NOT RELY ON`

### Atomic Rewind (`checkpoint/rewind.py`)

- Content-addressed snapshot store
- Dual-state rewind

### Log Compaction (`storage/compaction.py`)

- Pre-anchor prefix archived verbatim
- Live log bounded for month-long runs

---

## Gate and Enforcement

### Pre-Tool-Use Gate (`gate.py`)

- Allow/deny against ledger claims
- `.continuum/gate.json` config
- Deny messages teach the claim protocol

### Enforcing HTTP Gateway (`gateway.py`)

- Claim-before-fire proxy
- Route prefix enforcement
- Fail-closed
- Unknown host is denied fail closed, not an open relay

### Replay Guard (`replayguard.py`)

- `evaluate`, `protected_call`, `langgraph_protected_node`
- Closes ACRFence replay hazard

### Observation Hooks (`hooks.py`)

- Auto-checkpoint hooks
- File-derived progress
- Async variants
- Every file a coding CLI writes becomes digest-verified evidence, outside model control

---

## MCP Server

### 13 Tools

| Tool | Type | Description |
|:--|:--|:--|
| `record_progress` | Mutating | Record agent progress |
| `checkpoint` | Mutating | Force a checkpoint |
| `validate` | Read-only | Validate state (read-only) |
| `resume` | Read-only | Recovery decision + contract (read-only) |
| `intercept_action` | Mutating | Claim a side effect before it fires |
| `complete_action` | Mutating | Record the outcome of a side effect |
| `fail_action` | Mutating | Mark a side effect as failed |
| `reconcile_action` | Mutating | Settle an uncertain side effect |
| `list_actions` | Read-only | List external side effects (read-only) |
| `confirm` | Mutating | Confirm self-reported state (requires `CONTINUUM_MCP_CONFIRM_TOKEN`) |
| `compensate_action` | Mutating | Compensate a completed action |
| `record_plan` | Mutating | Record a plan |
| `providers` | Read-only | List environment providers |

### Authorization (`mcp/authz.py`)

- Deny-by-default
- 4-tier policy resolution:
  1. Explicit `policy=` argument
  2. `CONTINUUM_MCP_MUTATING_CLIENTS` (or alias `CONTINUUM_MCP_ALLOW`)
  3. `.continuum/mcp-policy.json`
  4. Deny
- Token auth: `CONTINUUM_MCP_TOKEN` (shared secret in `_meta.authToken`)
- Per-caller secrets: `CONTINUUM_MCP_CLIENT_TOKENS` (`name:secret` pairs)
- Confirm token: `CONTINUUM_MCP_CONFIRM_TOKEN` (separate secret for confirming self-reported state)
- Read-only tools are not gated

### MCP Install/Remove (`mcp/install.py`)

- Cross-platform registration in `.mcp.json`

### MCP Doctor (`mcp/doctor.py`)

- Diagnostic commands

---

## CLI

### 49 Commands

```bash
continuum runs                                   # list runs
continuum inspect <run_id>                       # semantic state
continuum validate <run_id> --env dataset=v4     # validate, read-only
continuum resume <run_id> --env dataset=v4       # recovery decision + contract + next steps
continuum checkpoint <run_id>                    # force a checkpoint, mutating
continuum actions <run_id>                       # external side effects
continuum reconcile <run_id>                     # settle uncertain effects with probes
continuum complete <run_id>                      # close a run as done, from the keyboard
continuum verify <run_id>                        # re-audit the event hash chain
continuum budget <run_id>                        # retry-budget usage per action type
continuum compact <run_id>                       # archive pre-anchor log prefix
continuum tree <parent_run_id>                   # show parent + children with recovery states
continuum attest <run_id> --key signer.pem       # sign the chain head for an external verifier
continuum start <run_id> --goal "..."            # start a new run
continuum hooks install claude-code --with-gate  # install lifecycle hooks
continuum gateway --port 8765                    # enforcing HTTP proxy
continuum dashboard                              # web dashboard
continuum benchmark                              # run benchmark suite
continuum watch                                  # liveness watchdog
continuum briefing                               # session-start context injection
continuum history                                # checkpoint history
continuum replay                                 # replay events
continuum restore                               # restore to a checkpoint
continuum health                                 # health check
continuum providers                              # list environment providers
continuum confirm <run_id>                       # confirm self-reported state
continuum serve                                  # sidecar (stdio or HTTP)
continuum --json <command>                       # JSON output for any command
```

### Exit Codes as Safety Contract

- Only a verified-safe run exits `0`
- `continuum resume "$RUN" && ./start-agent.sh` cannot launch onto stale state
- Every other mode maps to a distinct non-zero code
- A mode nobody has classified falls through to `UNSAFE` rather than `OK`

### TUI (`tui/app.py`)

- Terminal UI with tree view and recovery view

---

## Dashboard

### Web Dashboard (`dashboard/app.py`)

- `continuum dashboard` command
- Run inspection
- Recovery state visualization

### HITL Surface (`dashboard/hitl.py`)

- Confirm/reconcile/complete buttons
- Audit parity to the CLI
- Prefix trust advisory
- Pins

---

## Framework Adapters

### Production-Ready

| Adapter | Class | Module |
|:--|:--|:--|
| Generic Python | `GenericAgentAdapter` | `adapters/generic.py` |
| Filesystem sandbox | `FilesystemSandboxAdapter` | `adapters/filesystem.py` |
| Python in-process | `PythonInProcAdapter` | `adapters/python_inproc.py` |

### Guarded Skip (when dependency absent)

| Adapter | Class | Module |
|:--|:--|:--|
| Container | `ContainerAdapter` | `adapters/container.py` |
| Browser | `BrowserAdapter` | `adapters/browser.py` |
| Kubernetes | `KubernetesAdapter` | `adapters/kubernetes.py` |

### Experimental (live-model tested)

| Adapter | Class | Module |
|:--|:--|:--|
| OpenAI Agents SDK | `OpenAIAgentAdapter` | `adapters/openai.py` |
| LangGraph | `LangGraphAgentAdapter` | `adapters/langgraph.py` |
| LangChain | `LangChainAgentAdapter` | `adapters/langchain.py` |

### Thin Hooks (SDK-free)

| Framework | Entry Point | Module |
|:--|:--|:--|
| CrewAI | `install_crewai_hooks(storage, run_id)` | `adapters/thin.py` |
| AutoGen | `wrap_autogen_tool(tool, storage, run_id)` | `adapters/thin.py` |
| Pydantic AI | `Agent(capabilities=[wrap_pydantic_ai_hooks(storage, run_id)])` | `adapters/thin.py` |

### LangGraph Store

- `make_continuum_checkpointer(storage)` implements LangGraph's `BaseCheckpointSaver` over CONTINUUM's storage
- Every put lands in the same hash-chained, provenance-tagged event log

---

## Security

### Ed25519 Attestation (`security/attestation.py`)

- `continuum attest` signs a run's chain head with Ed25519
- External verifier can prove history was unaltered as of a known key

### Hashing (`security/hashing.py`)

- `stable_hash()`, `make_id()`, `canonical_sanitize()`

### Trust Gate (`security/trust_gate.py`)

- Trust boundary enforcement

### Lineage Tokens (`security/lineage.py`)

- Token-based lineage verification

### Constraint Validation (`security/constraints.py`)

- Constraint ID/charset validation

### Self-Certification Fix

- Agent reaching the MCP server cannot fabricate progress and have CONTINUUM confirm it was safe to resume
- `Event.source` records who asserted a fact, captured at write time and included in `content()` so it is signed
- The projector propagates `event.source` instead of hardcoding
- Everything written through MCP is tagged `Origin.EXTERNAL_AGENT`
- Confirming self-reported state requires a separate secret (`CONTINUUM_MCP_CONFIRM_TOKEN`)

---

## Storage Architecture

### Schema v6

SQLite is primary (WAL mode, `synchronous=FULL`, `IMMEDIATE` transactions). Postgres is CI-tested.

| Table | Purpose |
|:--|:--|
| `events` | Hash-chained append-only log (52 event types) |
| `runs` | Run metadata with `parent_run_id` for multi-agent |
| `versions` | SemanticState snapshots per checkpoint |
| `checkpoints` | Sealed checkpoint records with `RECOVERY` anchors |
| `action_index` | Cross-run idempotency projection (schema v3+): indexed reads, not full scans |
| `events_archive` | Compacted prefix storage (schema v5+): `continuum compact` bounds live log for weeks |
| `lg_checkpoints` / `lg_writes` | LangGraph native persistence (schema v4+): `make_continuum_checkpointer(storage)` |

### Self-Healing

- Hard-killed servers recover from orphaned SQLite `-wal`/`-shm` sidecars via single-retry cleanup at startup

---

## Benchmarks and Testing

### Test Suite

- ~2,864 tests collected (~2,825 passing, ~34 skipped) on Python 3.11, 3.12, 3.13
- 132 source modules, 187 test files
- Unit, `hypothesis` property-based, concurrency, adversarial tests
- CI enforces: ruff, ruff format, mypy strict, pytest

### CONTINUUM-Bench

- 5 crash scenarios + argument drift + 14-scenario recovery suite + 7 fault injection
- `continuum benchmark` prints measured numbers

### Horizon Benchmark (generated 2026-09-30)

- 5 scenarios, 4 passed, 1 failed
- Accuracy: 0.8 | Unnecessary escalation: 0.2 | Repair precision: 0.8
- Duplicate side effects: 0 | Duplicate work: 0.0 | Compression: 0.138
- Fault-injection: 7 scenarios, detection 1.0, unsafe 0.0

### E2E Autonomy Test

- 3 full Claude Code sessions (Opus 4.8), each hard-killed mid-batch
- All scored 7/7 on mechanics checks
- Agents autonomously called `record_progress`, routed sends through `intercept_action -> write -> complete_action`, called `resume` before acting, and refused to re-send invoices

### Live Adapter Tests (gpt-4o-mini via OpenRouter)

| Adapter | Soft resume (exactly-once) | Hard crash (resume blocked) |
|:--|:--|:--|
| LangChain | PASS - 1 side effect, resume safe | PASS - request_human, 1 uncertain |
| OpenAI SDK | PASS - 1 side effect, request_human | PASS - request_human, 1 uncertain |
| LangGraph | PASS - 1 side effect, resume safe | PASS - request_human, 1 uncertain |

### Benchmark Comparison

- CONTINUUM: 0 duplicate side effects, 0 duplicate work
- Naive replay: 50 duplicates for 50 actions attempted twice

### Nightly Bench CI

- `.github/workflows/bench-nightly.yml` runs automated nightly benchmarks with `--publish`

---

## Installation

### From PyPI

```bash
pip install continuum-agent
```

### With Extras

```bash
pip install continuum-agent[mcp]        # MCP server
pip install continuum-agent[otel]       # OpenTelemetry bridge
pip install continuum-agent[langgraph]  # LangGraph adapter
pip install continuum-agent[openai]     # OpenAI Agents SDK adapter
pip install continuum-agent[langchain]  # LangChain adapter
pip install continuum-agent[attest]     # Ed25519 attestation
pip install continuum-agent[postgres]   # Postgres backend
```

### With Homebrew

```bash
brew install wized2/continuum/continuum
```

### With Docker

```bash
docker run --rm ghcr.io/cyrax321/continuum
```

### From Source

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

---

## Quick Start

### Wire a Coding Agent in Two Minutes

```bash
continuum start my-task --goal "What the agent should do"
continuum hooks install claude-code --with-gate   # also: gemini, codex
```

From then on every file the agent writes is captured as hash-chained evidence, its session starts with an automatic status briefing, unclaimed side effects registered in `.continuum/gate.json` are refused before they fire, and a fresh session after any crash resumes with executable next steps. No CLAUDE.md required.

### Minimal Library Example

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="Analyze 10,000 documents"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "Analyze 10,000 documents", "total": 10_000})

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})

# After a crash, a new process picks up exactly where it stopped:
state = project("run_4821", store.read_events("run_4821"))
print(state.progress.completed)            # already done, not repeated
print(store.verify_events("run_4821").ok)  # True, chain intact after the crash
```

### MCP Server

```bash
uv pip install -e ".[mcp]"
CONTINUUM_MCP_MUTATING_CLIENTS=your-client-name continuum-mcp
```

---

## Usage Examples

### Crash Recovery Demo

```bash
python examples/crash_recovery_agent.py   # real process kill, real side effect
python examples/context_compaction.py     # transcript lost, checkpoint survives
python examples/model_switch.py           # Model A dies, Model B resumes safely
python scripts/mcp_smoke.py               # real subprocess, real JSON-RPC traffic
```

### E2E Autonomy Test

The `e2e-autonomy-test/` kit scripts a real invoice-batch task, a hard-kill mid-run, and a fresh resume session, then scores the outbox, ledger, and event chain out of band.

### Resuming Agent- or MCP-Reported Runs

State reported over MCP, or through the OpenAI adapter, carries `Origin.EXTERNAL_AGENT` provenance and resolves to `request_human` until confirmed. LangGraph and LangChain runs use `Origin.DETERMINISTIC` and resume directly.

```bash
continuum confirm <run_id>   # records REVIEW_CONFIRMED, then re-assesses
continuum resume <run_id>    # now reports RESUME
```

Over MCP the equivalent is the `continuum_confirm` tool followed by `continuum_resume`.

---

## Roadmap

| Phase | Component | Status |
|:--|:--|:--|
| 1-11 | Data models, semantic state, persistence, checkpointing, validation, action ledger, recovery engine, CLI, crash-recovery examples, environment snapshots/diffs, framework adapters | Complete |
| 12 | Benchmark suite (CONTINUUM-Bench) | Complete |
| 13 | Cloud API (FastAPI + PostgreSQL) | Partial: PostgreSQL storage backend and HTTP sidecar transport shipped; hosted multi-tenant service not started |
| 14 | Dashboard | Complete |
| 15+ | Enforced durability: observation hooks, gate, session briefing, reconciler probes, enforcing gateway, OTel bridge, action index, executable guidance, multi-client installers, semantic replay detection, version pinning, retry budgets, log compaction, HITL surface, fork semantics, informed retry, multi-agent aggregation | Complete |
| Next | Months-scale durability plane: milestone-anchored plans, structured attempt memory, atomic dual-state rewind, public recovery-correctness benchmark | Planned |

### Beyond the Original Plan

- MCP server, MCP authorization and caller-authentication layers
- Provenance and anti-self-certification
- Community files
- Schema versioning with forward migrations
- Bounded recovery context
- Consumed-grant tracking
- Ed25519 event-chain attestation
- Native LangGraph checkpointer
- Wheel artifacts on every push to `main`

---

## Limitations

- Gate does not see inside shell commands (Bash/curl bypass structured tool claims)
- Postgres backend is CI-tested but not battle-tested in production
- One level of multi-agent hierarchy in v1
- Large payload offloading not yet implemented
- Weeks-scale benchmark with token cost table not yet complete
- Framework adapters remain experimental (prefer `GenericAgentAdapter` for production)
- Agent/MCP runs need explicit confirm before auto-resume (by design)
- The verdict is advisory, not enforced: `RecoveryDecision.permits()` reports what the contract allows; nothing in the library intervenes if a caller ignores a `False` and acts anyway

---

## What CONTINUUM Is Not

| Not This | This Instead |
|:--|:--|
| An LLM | A reliability layer for agents that use LLMs |
| An agent framework | A recovery layer that plugs into any framework |
| A vector database | Structured semantic state, not embeddings |
| A RAG system | Verified checkpoints, not retrieval-augmented memory |
| A workflow engine | A recovery layer, not an orchestrator |

---

## Related Work

CONTINUUM sits at the overlap of durable execution, idempotent side-effect tracking, and crash recovery for LLM agents. The closest neighbors are:

- Machine-checked resume contracts (Khan 2026)
- Agentic transaction processing with constraint-gated admission (Mnemosyne 2026)
- Checkpoint-rollback attack analysis (ACRFence 2026)
- Design-level prompt-injection defense (CaMeL 2025)

---

## Contributors

CONTINUUM was created by **Anandhu P Shaji** ([@Cyrax321](https://github.com/Cyrax321)) and is maintained by the original creator. It is open source under the Apache-2.0 license.

36+ community contributors credited in [AUTHORS.md](AUTHORS.md) and [graphs/contributors](https://github.com/Cyrax321/CONTINUUM/graphs/contributors).

Published in 6 languages: English, Chinese, Spanish, Japanese, Portuguese, Korean.

---

## License

Apache 2.0 - see [LICENSE](LICENSE).
