# Months-Scale Agent Reliability: Live Web Synthesis 2026-08-24

Scope: what it takes to run agents for weeks and months. Every claim links a source. Where something is a plan rather than a built feature, it says so. This note consolidates the live arXiv sweep on 2026-08-24, the 9 prior research notes in `docs/research/`, and the current CONTINUUM implementation.

## 1. The ceiling and why it exists

METR Time Horizon 1.1 (metr.org, 2026-01-29, update to arXiv:2503.14499) puts the 50 percent success horizon at roughly 2 to 5 hours as of early 2026, doubling every 3 to 4 months since 2024 and every 7 months long term. Extrapolation places month-long tasks at 2027 to 2031 if the trend holds.

Task success is the product of per-step reliabilities. At 99.9 percent per step, 100 thousand steps still fail about 10 percent of the time, and per-step reliability is not constant:

* **Self-conditioning** (arXiv:2509.09677): models become more likely to err when the context already contains their own errors. Scaling model size does not fix it. Thinking and context hygiene do.
* **No-recovery bottleneck** (LEAD, arXiv:2603.06870): errors concentrate on a few hard steps that are irreversible without an explicit recovery path.
* **Off-plan drift is self-reinforcing** (arXiv:2602.19008): each off-canonical step raises the odds the next one drifts too, plus 22.7 points per step. Monitoring mid-trajectory adherence and restarting bad runs helps.
* **Planning and forgetting dominate at horizon** (HORIZON, arXiv:2604.11978): repeating failed actions across steps, catalogued over 3100 plus trajectories across 4 domains (GPT-5 variants and Claude) with a trajectory-grounded judge at inter-annotator kappa 0.61 and human to judge kappa 0.84.

Consequence for infrastructure: decomposition plus verification plus exactly-once effects plus recoverable context plus drift monitoring is the only known multiplier on horizon.

## 2. What the web tested recently

### 2.1 Diagnostics

* **HORIZON (2604.11978, Wang et al., 2026-04-13)** introduces a cross-domain diagnostic benchmark that systematically constructs tasks and classifies long-horizon failures. It is the first to measure horizon-dependent degradation across model families with a reproducible judge pipeline. For CONTINUUM it supplies a measurement method we should copy for our benchmark extension, plus evidence that subplanning is the top failure class.

### 2.2 Recovery

* **AgentRewind (2608.14380)** presents a runtime recovery framework that records aligned checkpoints of agent context and controlled environment and introduces MettleBench for long-horizon engineering tasks. Ablation finds environment rewind is the single most valuable component. This validates our open issue 292, atomic dual-state rewind, and the priority assigned in `long_horizon_gaps.md` section 5.
* **Crab (2604.28138)** measures how restored environment divergence derails recovered agents and argues for semantics-aware checkpoint or restore. This supports refusing resume until validation passes, which CONTINUUM already does via `StateValidator`.
* **ACRFence (2603.20625v1)** defines semantic rollback attacks (Action Replay and Authority Resurrection) when checkpoint restore does not roll back external effects, validates 10 of 10 duplicate commits with Claude Code CLI and Qwen3-32B, and proposes a framework-agnostic mitigation with effect log and fork semantics. Our `replay_similarity.py` and `fork.py` already implement the proposed but unbuilt defence on top of the gate plus ledger, as recorded in CHANGELOG gap fix for 2603.20625.

### 2.3 Memory and trust

* **Weighted Memory Tree (WMT, 2608.20631, 2026-08-21)** organizes execution into tasks, subtasks, and actions with dynamic retention scores and event-based updates that fold completed trajectories while preserving access. Evaluated on GAIA-Text with Qwen3-8B and Gemma. It argues naïve summarization loses. For CONTINUUM it maps to the open consolidation layer in `long_horizon_gaps.md` section 6, sleep-time compute and Auto-Dreamer. Our honest scope is narrower: distill the compacted archive into trajectory reports, not a semantic memory system.
* **Beyond Suspicious Steps: Ontological Trust (2608.17718)** introduces RGE, an online monitor that decomposes trust along Role, Goal, and Evidence and estimates prefix-level drift. This validates the `RecoveryContract.evidence` plus `reason` fields added in Phase 1 and suggests an advisory health monitor extension.
* **MileGPO (2608.19803)** derives process-level credit from grouped rollouts via milestone discovery and reliability-calibrated shaping. This is a concrete design input for milestone-anchored progress.

### 2.4 Benchmarks and scale

* **FM-Bench (2608.18423)** runs a football club for 20 in-game years through 26 tools and 340 to 400 decisions per year, measuring long-horizon management under competing agents. It proves a months benchmark must simulate years of simulated time, not just steps. Our Phase 6 suite is still hours scale (13 scenarios) and needs an FM-Bench style horizon extension.
* **Wuying-Browser-Agent, SkillGate (2608.17319, 2608.18852), Natural-Language Workflows Are Not Software Yet (2608.21341)** together show that skill selection and workflow artifact compilation are now first-class decisions. This supports treating `PlanStep.depends_on` as a real dependency graph, which CONTINUUM already does.

## 3. What CONTINUUM already covers

All rows below are shipped and tested, not planned. See `STATUS.md` and `CHANGELOG.md` for commits and test counts.

| Need | Shipped | Module |
| --- | --- | --- |
| Survive crashes without corrupting state | Yes | `events.py`, `state/semantic.py`, `storage/sqlite.py`, `checkpoint/` |
| Never repeat a paid side effect after replay | Yes | `actions/ledger.py`, `actions/idempotency.py`, `gate.py`, `gateway.py` |
| Never trust the agent's own report of itself | Yes | `provenance_map.py`, `9738b9e` self-certification fix |
| Resume cognition, not just state | Yes | `checkpoint/reasoning rehydration (#235)`, `continuum briefing` |
| Month-scale logs without linear cost | Yes | `checkpoint compaction (#239)`, `actions/action_index (#216)` |
| Retry storms bounded | Yes | `budgets.py (#240)` |
| Unknown-outcome effects settle from reality | Yes | `reconcilers.py` probes, gateway settlement |
| Replay correctness under prompt or model drift | Yes | `pinning.py (#241)`, `replayguard.py (#237)` |
| Many agents, one truth | Yes | `recovery/family.py` parent or child, aggregated contracts (#243) |
| Surgical repair of only what broke | Yes | `recovery/impact.py`, `state/validator.py scope`, `analysis/depends.py` |
| Explainable recovery | Yes | `RecoveryContract.evidence` plus `reason` (Phase 1) |
| Tamper-evident audit | Yes | `recovery/ledger.py` (Phase 5) |

Live proofs: `e2e-autonomy-test` 7 of 7 mechanics, MCP Inspector CLI sequences A through C, real LLM hard-crash harnesses for LangChain, OpenAI, LangGraph via OpenRouter (`examples/*_real_llm_crash.py`), and `continuum benchmark` with 0 duplicates for CONTINUUM strategies vs 50 for naive.

## 4. Gaps that block months

These are open by design, endorsed by the synthesis above.

1. **Resume context is curated by nobody.** Briefing serves the newest self-authored summary verbatim. Self-conditioning says error-laden history degrades the next session. Partially addressed via `recovery/summary.py build_informed_retry`, but the policy for what the next session should not see again is open.
2. **Failed attempts leave no structured memory.** Ledger records attempts, but no artifact distills cross-attempt lessons into a first-class event a fresh session can consume. Fork events carry a reason string; they could carry structured lessons. This is gap 3 in `long_horizon_gaps.md`.
3. **Progress is a flat counter, not a plan.** `Run.goal` plus `completed/total` cannot represent milestone 3 of 5, verified. Needs a milestone layer with verification gates per milestone (`task_context.md` `PLAN_UPSERT`). Largest of the open items because it touches `Goal` semantics and projection.
4. **Environment rewind is not aligned.** Checkpoints restore projection, not workspace. AgentRewind finds env rewind is the top ablation. Tracked as issue 292, atomic dual-state rewind.
5. **Idle cycles consolidate nothing.** Sleep-time compute (arXiv:2504.13171) and Auto-Dreamer (arXiv:2605.20616) plus WMT show offline consolidation turns idle time into cheaper future sessions. Honest scope for CONTINUUM is trajectory reports from the compacted archive, not a memory system (`long_horizon_gaps.md` section 6, `policy_learning.md`).
6. **Taxes on every resume.** `instant_detection.md` (hook, precomputed banner, wrapper), `confirm_tax.md` (scoped confirm), and `token_floor.md` (slim subset plus lazy exposure). These are not correctness bugs, they are cost that compounds over a month of restarts.

## 5. What weeks actually need

Combining METR trend with reliability findings: a week-long run needs roughly four orders of magnitude more reliable steps than today delivers unaided. The known path is architectural: decompose into milestones, verify each step against ground truth, make every side effect exactly once, recover alignment on restart with rehydration, watch for degradation between crashes, and never pay twice for the same mistake. CONTINUUM covers every layer except the two marked open above that matter most for month-scale autonomy: structured attempt memory and milestone-anchored progress.

## 6. Novelty layers to build, ordered

The six layers below are additive, backward compatible, and each has a falsifiable test. They correspond to the issues referenced.

### Layer 1: Milestone-anchored plan (P1) - Issue 312

* Event: `PLAN_UPSERT {plan_id, units: [id, title, status, depends_on]}`. Projection: `SemanticState.plan` reusing `PlanStep`. Tool: `continuum_record_plan` (mutating, allowlisted) at one event per unit.
* Validator walks `depends_on`. Health monitor anchors on milestones, not counters. Falsifiable: change dependency used only by step 4, only step 4 invalidates.

### Layer 2: Structured attempt memory (P1) - Issue 313

* Event: `ATTEMPT_LESSON {falsified, env_delta, scar_action_ids, next_avoid, source_evidence}` derived deterministically from `RecoveryDecision.rationale` plus ledger, `Origin.DETERMINISTIC`, bounded at 2KB. Projected into `SemanticState.attempt_lessons`, consumed by briefing instead of raw tail.
* Falsifiable: hard-kill mid-action, fork, fresh `project()` shows one lesson, new session lists it and error density drops.

### Layer 3: Instant detection plus scoped confirm plus token floor (P2)

* `SessionStart` hook runs `continuum --json resume` out of band, injects banner when an interrupted run exists, else silent. `.continuum/resume.json` precomputed on every checkpoint for instant reads. `continuum_resume_check` slim subset plus scoped `confirm --scope self` that only clears `EXTERNAL_AGENT` self-cert. System prompt trim per `token_floor.md`.
* Falsifiable: cold-start banner latency under 100ms, `request_human` rate on self-cert only artifacts drops, token floor measured via `tools/list` size.

### Layer 4: Dual-state rewind (P2) - Issue 292

* Atomic pair `(events plus workspace snapshot)` anchored at the recovery point. Per resource choice: transactional sandbox vs ledger, per analysis in `long_horizon_gaps.md` section 5.
* Falsifiable: file written before checkpoint, env mutated, rewind restores both projection and file to the same version.

### Layer 5: Sleep-time consolidation (P3)

* During `health` quiet windows, distill `events_archive` into `TrajectoryReport {attempts, scar_rate, stall_sites}` feeding the weekly report sketched in `policy_learning.md`. No semantic memory subsystem.
* Falsifiable: after 10 idle compactions, report lists top stall action type and scar rate, all rows digest-auditable.

### Layer 6: Prefix trust monitor (P3) - advisory

* RGE-style `Role/Goal/Evidence` scoring on top of `health`, advisory only, never moves mode. Falsifiable: drift injection lowers trust score while local action checks stay valid.

## 7. Measurement plan

Extend `benchmark/phase6` plus `benchmarks/run.py` toward HORIZON judge and FM-Bench horizon:

* **Unsafe resume rate** target 0, must survive ACRFence semantic rollback.
* **Recovery decision accuracy** against HORIZON-style judge, plus **unnecessary human escalation rate** (today 31 percent of 13 scenarios need human).
* **Dependency repair precision** and **duplicate side effects** (today 0 for CONTINUUM in bench).
* **Duplicate work** and **recovery compression ratio** per `references/bench.md`, with years of simulated time added.

No invented numbers are presented here.

## 8. What this note does not claim

* CONTINUUM did not invent checkpointing, durable execution, rewind, idempotency, event sourcing, or plugin architecture.
* The months thesis is a hypothesis to test against baselines (native harness persistence, LangGraph durable execution, naive checkpoint, transcript replay). See `docs/CONTINUUM_MASTER_PLAN.md` section 3.
* MCP authorization is by declared `clientInfo` unless `CONTINUUM_MCP_TOKEN` is set, per `STATUS.md`.

## 9. Sources

* Live arXiv: 2604.11978 HORIZON, 2608.14380 AgentRewind, 2603.20625 ACRFence, 2604.28138 Crab, 2608.20631 WMT, 2608.17718 ontological trust, 2608.19803 MileGPO, 2608.18423 FM-Bench, 2608.21341 Natural-Language Workflows.
* Prior synthesis: 2509.09677 self-conditioning, 2603.06870 LEAD, 2602.19008 off-plan drift, 2504.13171 sleep-time compute, 2605.20616 Auto-Dreamer.
* Internal: `long_horizon_gaps.md`, `task_context.md`, `instant_detection.md`, `confirm_tax.md`, `token_floor.md`, `policy_learning.md`, `human_gate_minimization.md`, `cross_agent_portability.md`, `ARCHITECTURE_EVOLUTION.md`, `CONTINUUM_MASTER_PLAN.md`, `STATUS.md`, `references/related-work.md` and `citation-audit-2026-08-24.md`.

## 10. Next writes

* Issue 312 covers Layer 1. Issue 313 covers Layer 2. Layers 3 through 6 map to the remaining research notes or to issue 292.
* `STATUS.md` checklist section is updated to point here so the next session does not re-derive the plan.
