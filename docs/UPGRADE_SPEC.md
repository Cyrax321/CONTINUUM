# CONTINUUM Upgrade Spec:   Durable, Verifiable Agents for Weeks and Months

* Status: Draft, proposed for review
* Author: CONTINUUM maintainers, synthesis from live web sweep 2026-08-24
* Created: 2026-08-24
* Last updated: 2026-08-24
* Branch: `docs/upgrade-spec` targeting `main`
* Companion docs: `docs/research/WEB_SYNTHESIS.md` (live sweep), `docs/research/long_horizon_gaps.md` (gap audit), `STATUS.md` (verified vs believed), `docs/CONTINUUM_MASTER_PLAN.md` (phased plan), `docs/ARCHITECTURE_EVOLUTION.md` (north star), `references/related-work.md` and `citation-audit-2026-08-24.md`
* Open issues referenced: #213, #244, #254, #288, #289, #292, #293, #294, #295, #296, #297, #301, #302, #303, #304, #307, #308, #309, #312, #313, plus 30 `good first issue` and 9 `bug`
* Recent PRs referenced: #311 open, #310, #300, #287, #286, #277, #275, #272, #264, #253 merged

---

## 1. Summary

This spec proposes the next upgrade of CONTINUUM from a crash-safe recovery layer that answers `safe to resume` to a months-capable durability plane that answers `worth resuming` and `what remains exactly`. It is additive and backward compatible. No existing recovery semantics change. The proposal is to ship six layers in three milestones, plus a measurement extension that validates each against baselines no one currently benchmarks.

The layers are: 1) milestone-anchored plan via `PLAN_UPSERT` (issue 312), 2) structured attempt memory with falsification lessons (issue 313), 3) instant detection plus scoped confirm plus token floor, 4) atomic dual-state rewind, 5) sleep-time consolidation, 6) prefix trust monitor. All are derived from 2026 literature and from the gap audit that mapped that literature onto our shipped coverage.

This document records what is verified, what is believed, and what is planned. No numbers are invented. Every research claim cites a paper or a measured run.

## 2. Motivation

### 2.1 The ceiling

METR Time Horizon 1.1 (2026-01-29, update to arXiv:2503.14499) puts the 50 percent success horizon at 2 to 5 hours, doubling every 3 to 4 months since 2024 and every 7 months long term. Month-long tasks extrapolate to 2027 to 2031. Task success is the product of per-step reliabilities. At 99.9 percent per step over 100k steps the run still fails about 10 percent of the time, and per-step reliability degrades when the context contains prior errors (self-conditioning, arXiv:2509.09677).

Infrastructure that decomposes, verifies, makes every effect exactly once, recovers alignment on restart, watches drift between crashes, and never pays twice for the same mistake is therefore the multiplier on horizon. That is CONTINUUM's scope.

### 2.2 Why this upgrade now

Three things converged:

1. The literature crossed from anecdote to benchmark. HORIZON (arXiv:2604.11978, 3100 trajectories, 4 domains, judge kappa 0.84) names subplanning as the dominant long-horizon failure. AgentRewind (arXiv:2608.14380) proves aligned context plus environment checkpoints beat plan refinement alone, with environment rewind as the top ablation. ACRFence (arXiv:2603.20625v1) proves checkpoint restore without effect rollback yields 10 of 10 duplicate commits and defines the semantic rollback class.
2. New memory and trust work gives a shape for the missing pieces. Weighted Memory Tree (arXiv:2608.20631, 2026-08-21) shows retention-scored hierarchies beat naive summarization. Beyond Suspicious Steps (arXiv:2608.17718) shows prefix drift needs an explicit Role/Goal/Evidence monitor, not just local action checks.
3. Our own hardening closed the crash and dedup gaps. `e2e-autonomy-test` 7 of 7, MCP Inspector sequences A through C, live hard-kill harnesses for three adapters via OpenRouter, and the idempotency benchmark (0 duplicates for CONTINUUM vs 50 for naive) leave the remaining gaps as structured context, not correctness.

Leaving them unaddressed means every months-long run pays the `goal plus counters` tax: re-exploration on every resume, error tails that poison the next session, and no milestones for the validator to anchor on.

## 3. Background

### 3.1 What CONTINUUM is

A reliability layer for long-running AI agents, not an agent framework, not a memory system, not a workflow orchestrator. It treats a checkpoint as evidence of what was believed at time T, not proof that continuing is correct.

Core invariant: every fact carries its origin, and trust is earned, never assumed.

Five integration seams, all funneling into `GenericAgentAdapter`:

* Seam 1: in-process adapters (generic, LangChain, LangGraph, OpenAI)
* Seam 2: MCP server (11 tools over stdio, 3 read-only, 8 mutating)
* Seam 3: CLI lifecycle hooks (`continuum hooks install`, `continuum observe`, `continuum gate`, `continuum briefing`)
* Seam 4: enforcing HTTP gateway (`continuum gateway`)
* Seam 5: OpenTelemetry bridge (`make_span_processor`)

Three guarantees, enforced everywhere: no self-certification (`9738b9e`), side effects require claims (ledger plus gate), recovery decisions verify against reality before declaring safe.

### 3.2 Current codebase snapshot (2026-08-24)

Source of truth: `git log --oneline -10` and file inventory on `main` plus the branch `fix/mcp-idempotency-and-diagnostics` which carries the full-gate audit at `8013f6a`.

* Tracked files: about 204. Python files: about 118. Total LOC: about 30,300. Core `src/continuum`: about 60 files and 14,800 LOC.
* Test suite: 104 source files and 99 test files at the 2026-08-24 audit. `pytest` green: 1345 passed, 24 skipped, 0 failed. `ruff check` and `ruff format --check` pass on 216 files. `mypy src/continuum` passes in CI (local mypy versions skew).
* Schema: v6 with parent_run_id, action_index, events_archive, lg_checkpoints. Storage: SQLite primary, Postgres via `psycopg` optional (`[postgres]`), both tested in CI (Postgres via service container since PR 238).
* Event types: 34 plus interchange v1, hash-chained with `verify()` and `trusted_through`.
* Recovery: 7 modes (RESUME, REPAIR_AND_RESUME, REPLAN, WAIT, REQUEST_HUMAN, ROLLBACK, ABORT) plus FORK semantics where needed (286), max severity ordering, sealed `RecoveryContract` with `evidence` and `reason` (Phase 1).
* Distribution: GHCR Docker image (`docker run --rm ghcr.io/cyrax321/continuum` demos crash recovery), `.devcontainer` for Codespaces, `uvx`/`pipx run` from git, release wheels.

For the full module map see `README.md` Architecture and `references/architecture.md`. For the verified vs believed breakdown see `STATUS.md`.

### 3.3 What is shipped and verified

All rows below have tests and, where noted, live subprocess proofs:

* Hash-chained event log with tamper evidence (`events.py`)
* State projection as pure fold (`state/semantic.py`)
* Provenance by writer with `REQUIRES_REVIEW` until `REVIEW_CONFIRMED` (`9738b9e`)
* Independent environment revalidation with staleness propagation `dependency -> evidence -> finding -> decision` plus `PlanStep.depends_on` walking (Phase 2, `recovery/impact.py`)
* Idempotent ledger with `UnknownSideEffect` (never `AssumeOccurred`), stable `key`, drift recognition via canonicalization plus token fallback, cross-run dedup (`action_index`), and reconciliation probes
* Recovery engine with sealed contract and executable `human_steps` (guidance)
* Checkpoint policies plus recovery anchors (`checkpoint_on_recovery`, `last_recovery_anchor`, `prune` with anchor preservation)
* Health monitoring between crashes (`continuum health`, 5 detectors, advisory only)
* Distribution surfaces (Docker, Codespaces, Trusted Publishing opt-in)

Live proofs: `e2e-autonomy-test` 7 of 7, MCP Inspector CLI, Claude Code live MCP session, three real-LLM hard-kill harnesses (`examples/*_real_llm_crash.py` via OpenRouter `gpt-4o-mini`), and `continuum benchmark` issue 6 drift proof (0 duplicates).

## 4. Goals and non-goals

### 4.1 Goals

* G1. A resumed session can list exact remaining work without re-exploring the repo or re-asking the user.
* G2. A failed attempt's lesson survives a hard kill and is available as structured evidence to the next session, not as a raw error tail.
* G3. Resume detection costs tens of milliseconds, not a user message plus an inference plus a tool call.
* G4. A checkpoint restores both the projection and the workspace in one verifiable step where requested.
* G5. Idle time distills the compacted archive into auditable trajectory reports, not into a new memory subsystem.
* G6. Health between crashes surfaces drift and stall without ever moving safety, so callers can decide to repair or pause.

### 4.2 Non-goals

* N1. Do not invent checkpointing or durable execution. Reuse and judge, do not compete with Temporal, LangGraph durable execution, Dapr, or Diagrid. See `docs/CONTINUUM_MASTER_PLAN.md` section 1.2.
* N2. Do not build a semantic memory system, a vector database, or a RAG layer. That belongs to memory systems. Keep CONTINUUM harness-neutral.
* N3. Do not lead with attestation. Keep Ed25519 signing behind the `[attest]` extra, not on the core recovery path. Diagrid already ships this at scale.
* N4. Do not add `FORK` or new modes without a concrete scenario that needs divergent recovery.
* N5. Do not invent benchmark numbers. Every claim in this spec must be measured before it is published.

## 5. Inventory: open issues, open PRs, recent merges

### 5.1 Open issues by area (48 open as of 2026-08-24)

Grouped from `gh issue list --state open` and label counts (`good first issue` 30, `enhancement` 20, `research` 13, `documentation` 10, `bug` 9, `cli` 8, `state` 7, `security` 6, `mcp` 5, `help wanted` 5).

**Umbrella tracking:**

* #213 Roadmap: enforced durability at the harness boundary (umbrella, `enhancement` `research`). Phases 0a to 3. Several phases already shipped (observe 210, gate 217, reconcilers 218, health, index 216).
* #244 Open problems in agent durability: nine research-grounded directions (umbrella, `enhancement` `help wanted` `research`). Problem list maps to #288 through #304.
* #215 Publish to PyPI via Trusted Publishing (`enhancement`). Opt-in `PUBLISH_PYPI` var, needs final wire.

**Storage and scale:**

* #254 Large-payload offloading: blob storage for oversized event payloads (`enhancement` `state`).
* #239 shipped, #254 remains.

**Research novelty, not yet implemented:**

* #288 Claim-level provenance graph with staleness propagation (`enhancement` `research` `state`)
  - #551 caused_by payload on DECISION_CREATED and ACTION_RECORDED, validation against log ids, hash-covered, max 32, 1-128 chars, defaults to [] (Refs #551)
* #289 Authority lifecycle: consumed-credential tracking and resurrection prevention (`enhancement` `mcp` `security`, complements 239 grant work in 287)
* #292 Atomic dual-state rewind, context plus environment revert in one command (`enhancement` `research`)
* #293 Public recovery-correctness benchmark, fault-injection grading for any framework (`enhancement` `benchmark` `research`)
* #294 Provenance that survives compaction, write-path origin binding through summaries (`enhancement` `provenance` `security`)
* #295 Restore-point admissibility, commitment graph blocks unsafe resumes (`enhancement` `research`)
* #296 Attention-budgeted human gate, fatigue-aware escalation and batching (`enhancement` `research` `mcp`)
* #297 Token cost of durability, publish the measurement nobody has (`enhancement` `benchmark` `research`)
* #301 Constraint pinning, governance constraints that survive compaction (`enhancement` `research` `security`)
* #302 Liveness watchdog, silence as a first-class recovery signal (`enhancement` `research`)
* #303 Risk-informed recovery policy, map real-time FailureRisks to recovery modes (`enhancement` `research` `state`)
* #304 Memory-mutation governance, external memory writes as claimed, provenance-stamped (`enhancement` `provenance` `security`)

**Bugs to address as part of the upgrade:**

* #307 validate or resume without env reports pinned dependencies as unknown with an inverted diagnostic (`bug` `state`)
* #308 expected_model is silently inert over MCP, no MCP path ever records a model (`bug` `mcp`)
* #309 Retry budget is enforced before dedup and counts successes, so idempotency breaks at budget (`bug` `state` `mcp`) - fix open in PR 311
* #329 health detect_no_progress counting total events instead of sequence gap (`bug` `good first issue`)
* #323 gateway returns 400 on invalid JSON instead of silently ignoring body (`bug` `good first issue`)
* #320 SQLiteStorage.close idempotence (`bug` `good first issue`)
* #326 budget registry validation message for float max_attempts (`bug` `good first issue`)
* Plus #331, #336

**Long-horizon gaps, now filed professionally (2026-08-24):**

* #312 durable structured plan via `PLAN_UPSERT` for long-horizon recovery (`enhancement` `help wanted` `research` `state`) - Layer 1, spec-complete in issue body
* #313 structured attempt memory with falsification lessons for cross-session resume (`enhancement` `help wanted` `research` `state`) - Layer 2, spec-complete in issue body

**Remaining `good first issue` that affect the upgrade surface:**

* #266 em dash sweep (documentation), #267 adapters.md missing thin adapters, #271 mcp.md tool table audit, #280 docker publish smoke test, #281 Windows try-it launcher, #282 compose.yaml for Postgres tests, #283 notebook demo, plus #315 through #338 covering mypy overrides, doc drift, dashboard hardening, sidecar auth hint, verify truncation hint, tree limit, probe timeout, pagination hint, observe_command portability, env parsing, json flag discoverability, health typing, gate path, dashboard escaping, backoff message, pre-commit, help punctuation.

### 5.2 Open PR

* #311 `fix(mcp): stop the retry budget defeating idempotency, and name what is actually missing` (`documentation` `state` `mcp` `tests` `awaiting review`, 2026-08-24) - fixes 309 plus diagnostic name for 307 and 308. Must merge before Layer 1 because `PLAN_UPSERT` keys and pinning share the dedup plus budget path.

### 5.3 Recently merged (last 20, newest first)

* #310 ci: build and smoke-test a wheel artifact on every push to main
* #272 fix(dashboard): bind 127.0.0.1 by default, loopback hardening
* #299 docs: record 2026-08-24 full-gate audit, refresh README counts, add architecture section 19
* #300 feat(recovery): semantic similarity backends for the replay guard
* #287 feat(actions): consumed-grant tracking, deny Authority Resurrection (269)
* #275 Informed retry: engine-authored prior-attempt summaries on recovery surfaces (265)
* #277 feat(distribution): add Docker image, Codespaces, and git-install paths
* #286 Fork semantics: audited divergent continuations at the tool boundary (259)
* #260 fix(storage): harden compaction per the 253 review
* #274 docs: tighten README, add install and related-work references, strip em dashes
* #252 fix(windows): make hooks, the test suite and the smoke script portable

The earlier wave (253 compaction, 255 retry budgets, 257 dashboard HITL, 264 parent or child, plus 214 lazy imports, 216 action index, 217 gate, 218 reconcilers) closed the enforced durability roadmap 213.

## 6. Gap analysis (verified gaps vs shipped)

Method: map `docs/research/WEB_SYNTHESIS.md` live sweep plus `long_horizon_gaps.md` against the verified table in section 3.3. Only gaps with a reproduction are listed as open.

| Gap | Today | Evidence | Grade |
| --- | --- | --- | --- |
| Lossy task context: goal plus counters only | `continuum resume` returns goal text and counters, no per-unit status | `task_context.md` reproduction: create run with goal plus total, observe `state.plan` empty | Open, P1 |
| Structured attempt memory missing | Ledger records attempts, no falsification artifact for next session | `policy_learning.md` weekly report sketch, `recovery/summary.py` only renders | Open, P1 |
| Health was missing between crashes | Now shipped as `continuum health` 5 detectors, advisory only | `long_horizon_gaps.md` section 1 receipt, `health.py` | Closed |
| Env rewind not aligned with projection | Checkpoint restores projection, not workspace | `agentrewind` ablation, issue 292 | Open, P2 |
| Idle consolidation missing | Compacted archive is not distilled | `long_horizon_gaps.md` section 6, WMT plus sleep-time | Open, P3 |
| Resume taxes: instant detection, confirm tax, token floor | Each resume pays message plus inference plus tool schemas | `instant_detection.md`, `confirm_tax.md`, `token_floor.md` | Open, P2 |
| Throttled or misdiagnosed pins: 307 plus 308 plus budget ordering 309 | Unknown pins reported with inverted hint, expected_model inert, budget before dedup | PR 311 reproductions plus `gh issue view` bodies | Open, blocks P1 |

No additional correctness gaps block the months thesis beyond these. All are additive, not rewrites.

## 7. Detailed design: the six layers

Every layer is additive, backward compatible, and leaves `events.py verify()`, the sealed one-next-action `RecoveryContract`, the ledger semantics with no `AssumeOccurred`, max severity ordering, and deny-by-default posture unchanged.

### 7.1 Layer 1: Milestone-anchored plan via PLAN_UPSERT (P1) - Issue 312

**Goal:** G1. Resume lists exact remaining units without re-exploration.

**Changes:**

* `EventType.PLAN_UPSERT {plan_id, units: [{id, title, status, depends_on}]}` in `src/continuum/models.py` and `src/continuum/events.py`. Hash includes sorted units for determinism.
* `state/semantic.py project()`: merge by `plan_id:unit_id`, latest write wins, full history retained. `SemanticState.plan: list[PlanStep]` uses existing `PlanStep` model, no new model.
* New tool `continuum_record_plan` in `mcp/server.py` (mutating, allowlisted) and CLI `continuum record-plan <run_id> --file <json> --plan-id <id>`. Origin handling like `record_progress`: MCP writes become `EXTERNAL_AGENT` and `REQUIRES_REVIEW` until confirmed.
* `state/validator.py`: walk `depends_on` plus existing `dependency -> evidence -> finding -> decision`. Stale unit invalidates downstream. Cycle detection reports `CONFLICTED` with diagnostic.
* `state/diff.py`, `recovery/contract.py`, `recovery/guidance.py`, `cli/main.py inspect`, `continuum resume --json`, `interchange`: render plan and diff.

**Storage:** No migration. Events table stores typed events. Missing plan means `plan=[]`.

**Security:** Provenance unchanged. Plan units from MCP do not launder trust.

**Testing:** `tests/test_plan_upsert.py` with latest-wins, hash coverage, empty plan, stale propagation, MCP provenance, interchange round-trip, CLI round-trip, deterministic merge property. Bench: crash mid-plan, resume with zero duplicate work for completed units.

**Alternatives rejected:** free text goal (lossy), out-of-band memory store (loses durability and audit), snapshot row (loses hash chain).

### 7.2 Layer 2: Structured attempt memory with falsification lessons (P1) - Issue 313

**Goal:** G2. Next session inherits what was falsified, not the raw error tail.

**Changes:**

* New `EventType.ATTEMPT_LESSON {attempt_id, falsified, env_delta, scar_action_ids, next_avoid, source_evidence, created_at}` bounded at 512 chars per field, 2KB total. Origin `DETERMINISTIC`, derived deterministically from `RecoveryDecision.rationale` plus ledger, not from LLM.
* `models.py AttemptLesson` and `SemanticState.attempt_lessons: list[AttemptLesson]` sorted by creation.
* Derivation helper `recovery/summary.py build_attempt_lesson(decision, ledger_entries, uncertain_actions)` pure.
* Lifecycle: emitted on `RUN_FORKED` or `REPAIR_AND_RESUME`, projected via `project()`, consumed by `briefing` instead of raw tail. No new mutating tool required. Read support via `inspect` and `resume --json` and `mcp/server.py continuum_resume` response.

**Storage:** Additive, no migration. Post-anchor live log, so compaction preserves lessons and archive remains digest-auditable.

**Testing:** `tests/test_attempt_lesson.py` with synthetic repair derivation, hash coverage, empty case, compaction survival, size bound, briefing exclusion of raw tail, determinism property.

**Alternatives rejected:** keep lessons only in rendered informed retry (loses durability after `os._exit`), agent-authored summary alone (`EXTERNAL_AGENT` trust, cannot mitigate self-conditioning), LLM trajectory summarization (scope belongs to memory systems).

### 7.3 Layer 3: Instant detection plus scoped confirm plus token floor (P2)

**Goals:** G3 plus cost. Cut resume detection from message plus inference plus call to tens of milliseconds, remove the per-MCP confirm tax, and lower the per-session token floor.

**Changes:**

* `SessionStart` hook that runs `continuum resume --json` out of band and injects a pre-rendered banner when an interrupted run exists, else silent. Uses existing `hooks.py make_auto_checkpoint_hook` pattern.
* Precomputed `.continuum/resume.json` written on every checkpoint (next recovery decision, goal, progress, contract next step) for instant reads without starting Python, per `instant_detection.md`.
* Wrapper `continuum-resume-banner` for clients without hooks.
* Scoped confirm `continuum confirm --scope self` that only clears `REQUIRES_REVIEW` due to `Origin.EXTERNAL_AGENT`, not env drift, per `confirm_tax.md`. Keeps the human gate for real staleness while removing the tax. Same-client auto-confirm can layer later with audit.
* Slim subset `continuum_resume_check` plus lazy tool exposure and system prompt trim, per `token_floor.md`. Read-only split stays intact.

**Testing:** hook unit plus shell integration (real `SessionStart` payload file), banner precompute latency, scoped confirm leaves env-stale `REQUIRES_REVIEW` intact, token floor measured via `tools/list` size.

### 7.4 Layer 4: Atomic dual-state rewind (P2) - Issue 292

**Goal:** G4. Restore projection and workspace in one verifiable step.

**Changes:**

* Build on `checkpoint compaction` anchor mechanism plus `environment/snapshot.py GitProvider` plus transactional sandbox option (Yan 2512.12806, 1.8s per transaction, 14.5 percent overhead). Choice per resource: ledged external effects vs snapshotted files. This is the AgentRewind top ablation: without env rewind, resumed sessions restart cognition from a progress bar.

**Testing:** file written before checkpoint, env mutated, rewind restores both to the anchored version, hashes verified.

### 7.5 Layer 5: Sleep-time consolidation (P3)

**Goal:** G5. Turn idle time into cheaper future sessions without building a memory system.

**Changes:**

* During `health` quiet windows and on a schedule, distill `events_archive` into `TrajectoryReport {attempts, scar_rate, stall_sites}` feeding the weekly report sketched in `policy_learning.md`. Stay out of semantic memory. WMT is a memory system; CONTINUUM stays neutral, per `long_horizon_gaps.md` section 6.

### 7.6 Layer 6: Prefix trust monitor, advisory (P3)

**Goal:** G6. Surface drift that local action checks miss.

**Changes:**

* RGE-style Role/Goal/Evidence scoring on top of `health`, advisory only, never moves `mode` or `safe`. Extends the `evidence` plus `reason` contract explainability added in Phase 1.

### 7.7 Cross-layer invariants

* All events remain hash-chained and `verify()` covers them.
* Lessons, plan units, and trajectory reports are derived from validator evidence, never invented.
* Compaction preserves anchors and lessons. Archive is fully re-digestible.
* MCP remains deny-by-default. New tools are mutating where they write, read-only where they inspect.

## 8. API and storage changes

### 8.1 Public API

* Python: `EventType.PLAN_UPSERT`, `EventType.ATTEMPT_LESSON`, `SemanticState.plan`, `SemanticState.attempt_lessons`, `AttemptLesson`, `build_attempt_lesson`.
* MCP: new `continuum_record_plan` (mutating). `continuum_resume` adds `plan` and `attempt_lessons` to read-only response. New slim `continuum_resume_check` for token floor.
* CLI: `continuum record-plan`, `continuum health` already shipped, `continuum confirm --scope self`, `continuum briefing` already shipped. Help and `references/cli.md` updated.

Additive only. No existing command changes shape without `plan` present.

### 8.2 Storage

No schema migration for Layers 1 and 2 beyond the new event types. `storage/migrations.py` runner exists for forward migrations when needed. If Layer 4 requires a workspace snapshot table, it will ship as an additive migration with `schema_migrations` entry, per `storage/postgres.py` parity.

## 9. Security and privacy

* New provenance strings that are user data (goal text, plan titles, evidence) are HTML-escaped in the dashboard (`#334` pattern) and truncated deterministically.
* Writes via MCP remain `EXTERNAL_AGENT` and `REQUIRES_REVIEW`. No path launders trust. `propose` or `describe_advisory` stage 0 paths stay stage 0 per PR 311 fix.
* `CONTINUUM_MCP_TOKEN` and `CONTINUUM_SERVE_TOKEN` remain fail-closed. Dashboard POST remains fail-closed until `CONTINUUM_DASHBOARD_TOKEN` is set.
* Sidecar and gateway still refuse invalid JSON with 400 (`#323`) and cap POST bodies, per CHANGELOG.

## 10. Testing strategy

Each layer lands with its own test file and a live subprocess proof where a process boundary matters:

* Layer 1: `tests/test_plan_upsert.py` plus a plan milestone bench and a `health` milestone anchor test.
* Layer 2: `tests/test_attempt_lesson.py` plus compaction survival and briefing integration.
* Layer 3: `tests/test_sessionstart_hook.py`, `tests/test_scoped_confirm.py`, `tests/test_token_floor.py`.
* Layer 4: `tests/test_dual_rewind.py` with real `os._exit` harness.
* Blockers: `tests/test_*` for 307, 308, 309 diagnostics (PR 311 already covers most).

Full gate on every PR: `pytest -q`, `ruff check` plus `ruff format --check`, `mypy src/continuum`, plus the `benchmarks/run.py` suite for the measurement layers.

## 11. Measurement and benchmarks

Extend `benchmark/phase6` and `benchmarks/run.py` toward HORIZON judge discipline and FM-Bench years-scale simulation:

* Unsafe resume rate target 0, must survive ACRFence semantic rollback (10 of 10).
* Recovery decision accuracy vs validator evidence.
* Unnecessary human escalation rate (today 31 percent of 13 Phase 6 scenarios need human; scoped confirm should lower the self-cert share without masking env drift).
* Dependency repair precision and duplicate side effects (today 0 for CONTINUUM).
* Duplicate work and recovery compression ratio per `references/bench.md`.
* Token cost of durability (issue 297): publish the measurement nobody has, with a `continuum health --json` plus `benchmark` harness that reports tokens with and without the hook and slim subset.

No numbers are published until measured. `docs/CONTINUUM_MASTER_PLAN.md` section 3.2 and `ARCHITECTURE_EVOLUTION.md` section 9 already require baselines (native harness persistence, LangGraph durable execution, naive checkpoint, transcript replay).

## 12. Rollout plan

Tiers are sized so one phase lands as one reviewed PR.

**Milestone M1: remove the taxes (1 to 2 weeks, low risk) - unblocks months iteration speed**

* M1a. Merge PR 311 (budget before dedup plus pin diagnostics). Required before any layer that touches keys or pins.
* M1b. Layer 3 hook plus precomputed banner plus scoped confirm plus slim subset. Docs plus one example `examples/resume_hook_demo.py`.
* M1c. Good first issue sweep that touches the same surfaces: 323, 307 hint fix, 308 inert model fix, 309 covered, 318 sidecar hint, 327 env parsing unification, plus docs 266, 267, 271.

**Milestone M2: make months demoable (2 to 3 weeks, medium risk, flagship)**

* M2a. Layer 1 PLAN_UPSERT. One example `examples/plan_milestones.py` that proves 5 units with deps, crash after unit 2, resume lists 3 to 5 without re-exploration.
* M2b. Layer 2 attempt lessons. One example `examples/attempt_lesson.py` that forces a failure, forks, hard-kills, resumes, and asserts the new session receives the lesson.
* M2c. Start issue 292 dual-state rewind behind a feature flag so bench can call it without it being default.
* M2d. Begin good first issue paydown: 282 compose.yaml for Postgres, 280 docker smoke test, 283 notebook demo.

**Milestone M3: publish the proof (2 weeks, measurement)**

* M3a. Extend `benchmark/phase6` with HORIZON-style judge and FM-Bench years simulation. Publish the 6 metrics above against real baselines (issue 293).
* M3b. Layers 5 and 6 consolidation plus trust monitor as advisory health extensions.
* M3c. Address 290 consumed-grant lifecycle and 294 provenance through compaction where they intersect the archive path.

**After M3:**

* Revisit the 9 research novelty issues in 244 for adoption, plus 254 large-payload offloading and 215 PyPI publish. All remain scoped as additive, each with acceptance criteria.

## 13. Alternatives considered

* Keep free text goal plus counters: simple but lossy, forces re-exploration. Rejected for G1.
* Build full memory subsystem: out of scope, violates neutrality, competes with WMT and sleep-time work. Rejected for G5. Do reports instead.
* Own the runtime like Temporal: enforces at the cost of abandoning every external CLI where users live. Rejected in #213. Hybrid boundary enforcement (observe plus gate plus gateway plus OTel) is the chosen path.
* Do nothing on resume taxes: measured insufficient twice, once in e2e argument drift and once in the 207 kill-window reproduction.

## 14. Open questions

* Q1. What is the right `PLAN_UPSERT` merge rule for concurrent writers to the same `plan_id` under a stale lease. Current proposal is latest wins, but lease semantics may require last-writer-wins with conflict `CONFLICTED`.
* Q2. Should `AttemptLesson` be emitted on every `REPAIR_AND_RESUME` or only on fork. Leaning to both but behind a helper that stays pure until the caller requests it, so `assess` stays read-only.
* Q3. Where should trajectory reports live: as events in the hash chain, as a separate `reports` table, or as interchange artifacts. Leaning to events for audit, with bounded size.
* Q4. Token floor measurement needs a deterministic tokenizer. Should it vendor a count or reuse the gateway byte count plus tool schema size. Leaning to the latter for zero new deps.

## 14. Admissibility Layer (Issue #295)

DART formalizes that a controller-legal restore can still be semantically
invalid when committed downstream work depends on outputs that would be rolled
back. CONTINUUM implements this as a commitment graph:

* Completed actions optionally record consumed_inputs: checkpoint_seq,
  event_positions, component_ids, action_ids (bounded, validated).
* On resume, check_admissibility walks forward from the candidate checkpoint.
  Any COMPLETED action whose consumed_inputs references a position after the
  checkpoint makes the checkpoint inadmissible for plain RESUME.
* Validator emits a machine-readable ComponentValidationEntry (ACTION) with
  detail listing each blocking commitment and its chain position. Old rows
  without consumed_inputs remain admissible.
* Engine maps inadmissibility to REPAIR_AND_RESUME (re-derivable) or
  REQUEST_HUMAN (action graph) instead of offering RESUME. The sealed
  contract names each blocking commitment with chain position in both
  invalidated and evidence, so the refusal is auditable.

## 15. Risks and mitigations

* R1. Touching `Goal` semantics ripples through projection and diff. Mitigation: keep `plan` separate from `goal` text, reuse `PlanStep`, make old runs project to empty plan.
* R2. Lessons could be mistaken as trusted if derived from `EXTERNAL_AGENT` summaries. Mitigation: derive only from deterministic evidence and pin `Origin.DETERMINISTIC`.
* R3. Hook on every `SessionStart` could slow sessions when no run exists. Mitigation: early exit 0 with no output and precomputed file read when available.
* R4. Dual-state rewind snapshot cost. Mitigation: opt-in per resource, feature flag, measure before graduating.

## 16. References

* METR Time Horizon 1.1 (2026-01-29) and arXiv:2503.14499
* HORIZON: arXiv:2604.11978 (Wang et al., 2026-04-13)
* AgentRewind: arXiv:2608.14380 and MettleBench
* ACRFence: arXiv:2603.20625v1
* Crab: arXiv:2604.28138
* RetryGuard: arXiv:2511.23278
* Weighted Memory Tree: arXiv:2608.20631 (2026-08-21)
* Beyond Suspicious Steps / RGE: arXiv:2608.17718
* MileGPO: arXiv:2608.19803 and MileGPO reliability-calibrated shaping
* FM-Bench: arXiv:2608.18423 (20 years, 26 tools)
* Sleep-time compute: arXiv:2504.13171, Auto-Dreamer: arXiv:2605.20616, RecMem recurrence-triggered consolidation
* Self-conditioning: arXiv:2509.09677, LEAD no-recovery: arXiv:2603.06870, off-plan drift: arXiv:2602.19008
* Internal audit: `citation-audit-2026-08-24.md`, `references/related-work.md`
* Upgrade inputs: `docs/research/*` (9 notes plus this synthesis), PR 311 diff, open issues list 2026-08-24 (48 open), recent merges list above

## 17. Appendix A: how to review this spec

Reviewers: check that no section invents a number that is not cited, that every `Added` has a falsifiable test named, and that no `Changed` weakens an existing guarantee listed in `STATUS.md` Verified. The spec is additive. If a line would require a migration or a new `RecoveryMode`, it must be called out explicitly and justified with a concrete scenario.

## 18. Appendix B: current test surfaces that must stay green

* `pytest` full suite, `ruff check`, `ruff format --check`, `mypy src/continuum` in CI
* `tests/test_action_ledger.py` (including stable key and token fallback)
* `tests/test_phase1.py` through `tests/test_lease.py` (Phase 1 to Phase 6 plus ledger, impact, health)
* `tests/test_mcp_server.py` (orphaned WAL self-heal), `tests/test_serve.py`, `tests/test_reconcilers.py`, `tests/test_action_index.py`, `tests/test_interchange.py`
* `benchmarks/run.py` and `tests/test_benchmark.py` (CONTINUUM-Bench) and `tests/test_phase6.py` (13 scenarios)

## 19. Appendix C: file inventory for reviewers

Core: 60 files in `src/continuum` as listed in section 3.2. Entry points: `continuum.cli.main:main`, `continuum.mcp.server:main`. Tests: 99 files. Examples: `crash_recovery_agent.py`, `context_compaction.py`, `model_switch.py`, `recovery_walkthrough.py`, plus real-LLM crash harnesses. Docs: `README.md` (11 tools, 33 commands, 5 seams), `docs/*` research notes, `references/*` specs.

---

End of spec. Next write is the PR that lands `WEB_SYNTHESIS.md` plus this spec plus the `STATUS.md` checklist update.

