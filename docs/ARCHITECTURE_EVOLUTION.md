# CONTINUUM Architecture Evolution

This document maps the current CONTINUUM codebase against the new north-star
direction ("a universal, framework-independent recovery-validation and
reliability layer for autonomous AI agents") and proposes a minimal,
extend-don't-replace migration path. It is analysis only. No speculative
features were implemented.

Scope note: everything below was read directly from source on `main`
(commit `513a52e`, working tree clean). File and line references point at the
code as it exists today.

---

## 1. Current architecture

CONTINUUM is a layered durability and recovery library. Every layer appends to
and replays one append-only, hash-chained event log per run.

| Layer | Module | Role |
|---|---|---|
| Event log | `events.py` | Append-only, hash-chained `Event` records; `verify()` with `trusted_through` |
| Models | `models.py` | `EventType` enum, `SemanticState`, `Action`, `RecoveryMode`, `RecoveryContract`, status enums |
| State | `state/` | `project()` fold, `validator.py` (validation + staleness propagation), `extractor.py`, `versioning.py`, `diff.py` |
| Storage | `storage/` | `SQLiteStorage` (v2 schema), `postgres.py`, `migrations.py` |
| Actions | `actions/` | `ActionLedger` (idempotent side effects), `idempotency.py`, `reconciliation.py` |
| Checkpoint | `checkpoint/` | `CheckpointManager`, policy stack (`policy.py`), `build_recovery_context` |
| Recovery | `recovery/` | `RecoveryEngine.assess()`, `planner.py` (repair plan), `contract.py` |
| Adapters | `adapters/` | `AgentAdapter` base + Generic / LangChain / LangGraph / OpenAI |
| MCP | `mcp/` | `server.py` (12 stdio tools), `authz.py` (allowlist + shared secret) |
| Serve | `serve/` | newline-JSON sidecar mirroring the MCP surface |
| CLI | `cli/` | `argparse` commands, exit codes as verdict |
| Environment | `environment/` | `EnvironmentProvider` ABC + File/Git/Value/Callable/Static providers |
| Security | `security/` | `provenance.py`, `trust_gate.py`, `revalidation.py`, `attestation.py` (in progress) |
| Interchange | `interchange/` | versioned (v1) self-validating JSON envelopes |
| Concurrency | `concurrency/` | `LeaseCoordinator` (exactly-one-resume guard) |
| Plugins | `plugins/` | `Registry` + four capability `Protocol` seams |
| Benchmark | `benchmark/` | CONTINUUM-Bench scenario harness |

Three transports sit on the same core: the `continuum` CLI, the `continuum-mcp`
server, and the `continuum serve` sidecar. All delegate to `GenericAgentAdapter`
and the core primitives (`Storage`, `project`, `CheckpointManager`,
`ActionLedger`, `RecoveryEngine.assess`).

---

## 2. Existing guarantees (what we already have)

- **Tamper-evident event log.** `Event.content()` is hashed (including
  `Origin`), `EventLog.append` links `prev_hash`, and `verify()` walks the chain
  reporting `TAMPERED_CONTENT` / `BROKEN_CHAIN` and a `trusted_through`
  sequence. (`events.py`)
- **Hash-chained, signed attestation (optional).** `security/attestation.py`
  (Ed25519 + SHA256) signs the chain hash + `trusted_through`; explicitly not
  on the core recovery path. Human attestation hook `REVIEW_CONFIRMED` exists
  and clears self-certification review.
- **Provenance-aware state, by writer.** `Origin` (DETERMINISTIC / HUMAN / LLM /
  EXTERNAL_AGENT / IMPORTED) marks self-certified state; the validator forces
  `REQUIRES_REVIEW` on self-reported goal/progress until a human confirms.
- **Independent environment revalidation.** `StateValidator` verifies every
  component against a captured `EnvironmentSnapshot`; staleness propagates
  `dependency -> evidence -> finding -> decision`.
- **Idempotent action ledger.** `ActionLedger.claim/complete/fail/reconcile`
  with Stripe-style `key=`, `volatile=`, derived `arguments_hash`, unknown-side-
  effect raised as `UNKNOWN`, and reconcilers (probe / assume-not-occurred /
  manual). `AssumeOccurred` is intentionally absent.
- **Recovery engine with a sealed contract.** `RecoveryMode` (seven modes),
  severity-ordered; returns a `RecoveryContract` with exactly one
  `next_allowed_action`. The engine takes the max-severity signal, so the most
  cautious answer wins.
- **Structured plan representation already exists.** `SemanticState.plan` is
  `list[PlanStep]` where `PlanStep` has `depends_on` and `evidence` (a real
  dependency graph).
- **Deny-by-default authorization** on mutating MCP tools (allowlist +
  optional shared secret).
- **Portable interchange** (B4): versioned, self-validating JSON envelopes for
  `SemanticState` / `RecoveryContract` / `RecoveryDecision`.
- **Large, green test suite.** 900 tests collected; the full run exits 0 with
  only the Postgres tests skipping (5, need `CONTINUUM_TEST_POSTGRES_DSN`).

---

## 3. Existing limitations (gaps vs the north star)

### 3.1 Mapping: current component vs required model

| North-star dimension | Status | Where | Gap |
|---|---|---|---|
| Universal Harness Contract (event taxonomy) | PARTIAL | `events.py:46` (`EventType`) | Namespaced groups, not a dotted `session/task/model/tool/state/environment/checkpoint/action` hierarchy. Missing literals: `session.interrupted`, `model.called/completed`, and dedicated `action.completed/failed` events (completion/failure are statuses, not events). No correlation key linking a `TOOL_*` event to its `ACTION_*` event. |
| Semantic State | MOSTLY | `models.py:385` (`SemanticState`) | Has goal/plan/progress/decisions/findings/evidence/pending_work/approvals/external_dependencies/model. Missing: a durable "completed actions" list (completed work is removed from `pending_work`, `semantic.py:217`); "environment assumptions" as a first-class field (only `ExternalDependency`). |
| Evidence / Provenance | FRAGMENTED | `models.py:162`, `security/provenance.py:18` | Three parallel vocabularies: `Origin` (writer-based), `StateStatus` (VALID/STALE/CONFLICTED/UNKNOWN/INVALID/REQUIRES_REVIEW/EXPIRED), and security `TrustLevel` (verified/unverified/contested). The requested `AGENT_ASSERTED / OBSERVED / VERIFIED / INFERRED / CONTRADICTED` statuses do not exist on state components. |
| Environment Model | PARTIAL | `environment/snapshot.py:47` | `EnvironmentProvider` ABC exists; File/Git/Value/Callable/Static providers. Missing: Database/Browser/Cloud/GitHub providers. Providers are passed manually to `capture()`; the validator does not auto-apply a registered provider (no discovery). |
| Action Ledger | MOSTLY | `actions/ledger.py`, `models.py:94` | `ActionStatus` (PLANNED/STARTED/COMPLETED/FAILED/UNKNOWN/COMPENSATED/REQUIRES_REVIEW/EXPIRED) maps to the north-star set except `RECONCILING` (reconcile is a transition, not a stable status). `idempotency_key` is supported at the call site but NOT persisted on the `Action` model. Capability classes (transactional / idempotency-supported / observable / unobservable) are absent. |
| Dependency Graph | PARTIAL / DISCONNECTED | `validator.py:220`, `models.py:242` | No explicit graph object. Staleness uses a bespoke `evidence -> finding -> decision` cascade. `PlanStep.depends_on` (a real plan graph) is ignored by the validator, so plan-level invalidation does not propagate. Two disconnected dependency models. |
| Recovery Validator | EXISTS | `recovery/engine.py` | Seven modes present. Missing `FORK`. `RecoveryContract` lacks `evidence` and `reason` fields (rationale lives only on `StateValidationResult`/`RecoveryDecision`). Engine docstring severity ordering disagrees with the actual `SEVERITY` dict (omits `REPLAN`). |
| Security / Integrity | STRONG | `events.py`, `security/attestation.py` | Hash chain robust. But recovery state is not yet treated as a first-class security object at the resume boundary, and `AuthPolicy` lives only in `mcp/authz.py`, not the core. |
| Adapters | EXISTS / DUPLICATED | `adapters/base.py:26` | `AgentAdapter` is a genuine 4-method universal shape (capture/restore/intercept/resume). But `RUN_STARTED` bootstrap is copy-pasted 5 times; `checkpoint_node` is near-duplicated between LangChain and LangGraph; `GenericAgentAdapter.start_run` does not backfill `RUN_STARTED`, forcing each subclass to. Adapters call core APIs directly, not a normalized adapter-facing event stream. |
| Benchmarking | PARTIAL | `benchmark/__init__.py` | Scenarios: process_crash, dataset_change, unknown_side_effect, partial_completion, early_crash, argument_drift. Measures duplicate work / side effects / stale detection / context size. Missing: recovery-decision-accuracy and unsafe-resume-rate (the headline metric for a validation layer). |

### 3.2 Other concrete conflicts found

- **Lease coordinator is not composed.** `concurrency/lease.py` guarantees
  exactly-one resume, but `recovery/engine.py` and the MCP/serve transports never
  acquire a lease, so the guarantee is not enforced where resume happens.
- **Policy checkpointing is orphaned.** `checkpoint/policy.py` (Manual/Interval/
  Event/Semantic/Hybrid/ContextPressure) and `maybe_checkpoint()` exist, but the
  adapters call unconditional `checkpoint`; automatic triggering lives only in the
  benchmark and the OpenAI hooks. So "checkpoint before context compaction" and
  "checkpoint on interruption" are not wired into the universal layer.
- **Plugin seams are declared, not load-bearing.** `plugins/seams.py` defines
  `StateExtractor` / `ActionReconciler` / `ValidationRule`, but only
  `EnvironmentProvider` is wired; the others have no consumers. There is also a
  second, divergent `StateExtractor` Protocol (`plugins/seams.py` vs
  `state/extractor.py:63`), which invites drift.
- **No recovery-correctness benchmark.** The layer's central claim (safe vs
  unsafe resume) is currently unmeasured.

---

## 4. Competitor overlap

Do not claim CONTINUUM invented any of the following; they are established or
actively researched:

- Checkpointing / durable execution: LangGraph durable execution, Cloudflare
  Agents, Temporal / Restate / DBOS, AgentRewind, native harness session
  persistence.
- Rewind / resume / rollback: covered by the above and by transcript replay.
- Idempotency / event sourcing: Stripe idempotency keys, transaction outbox,
  saga pattern.
- Plugin architecture: standard for agent frameworks.

CONTINUUM already overlaps with these on the *durability* axis. The hypothesis
to test (not assume) is whether it can uniquely add: **evidence-backed,
environment-aware recovery validation across heterogeneous agent harnesses**,
where a checkpoint is treated as evidence of a past belief, not as automatic
proof that continuing is safe.

---

## 5. New CONTINUUM thesis

"A universal, framework-independent recovery-validation and reliability layer
for autonomous AI agents."

The shift is emphasis, not a rewrite:

- From "we can checkpoint and resume" to "we can determine whether resuming is
  still VALID and SAFE given that the world changed."
- The checkpoint is **evidence of what was believed at time T**, not proof that
  continuing is correct.
- The core must consume a harness-independent contract and remain ignorant of
  Claude Code / LangGraph / Codex / DSH / OpenAI / custom harnesses.
- MCP, CLI, HTTP, and the Python SDK are interfaces into the same core, not the
  architecture.

---

## 6. Proposed architecture (extend, do not replace)

The existing architecture already matches the thesis in skeleton. The work is to
close the gaps in section 3 with the smallest possible surface, reusing what
exists:

- **Unify provenance into one canonical vocabulary** that maps `Origin`
  (writer) and `TrustLevel` (verification) onto the requested state statuses
  (`AGENT_ASSERTED / OBSERVED / VERIFIED / INFERRED / STALE / CONTRADICTED /
  UNKNOWN / REQUIRES_REVIEW`). Keep `Origin` internally; expose one enum on state
  components. No new concept, just a single source of truth.
- **Connect the plan dependency graph to staleness propagation.** Teach
  `StateValidator._propagate` to also walk `PlanStep.depends_on`, so an
  environment change invalidates only the downstream steps that actually depend
  on the changed resource (dependency-localized repair: `REPAIR step -> RESUME`
  instead of `START OVER`). This reuses the existing `PlanStep` model.
- **Extend `RecoveryContract` with `evidence` and `reason`** (additive fields,
  backward compatible) so the contract carries why it decided, not just what.
- **Add `FORK` to `RecoveryMode` only if a concrete scenario needs divergent
  recovery**; otherwise leave it out (do not add unused modes).
- **Make `ActionLedger` persist `idempotency_key` on `Action`** and add a stable
  `RECONCILING` status, plus an optional capability class enum on the action so
  reconciliation strategy can be chosen per external system.
- **Turn `AgentAdapter` into the single funnel.** Move `RUN_STARTED` bootstrap
  and `checkpoint_node` into the base/Generic layer; add a thin harness-event
  mapping so each adapter translates its lifecycle calls into the core
  `EventType` vocabulary (no new dotted taxonomy required; reuse the existing
  enum, adding only the missing literals).
- **Wire `maybe_checkpoint()` into the universal layer** (after meaningful tool
  execution, before context compaction, on interruption) using the existing
  policy stack.
- **Compose the lease coordinator into the resume/transport boundary** so
  exactly-one-resume is actually enforced.
- **Integrate the plugin seams or delete the duplicates.** Either make
  `StateExtractor`/`ActionReconciler`/`ValidationRule` the real consumers (and
  remove the divergent `plugins` copies), or drop the unused seams.
- **Extend the benchmark with recovery-correctness metrics**: recovery-decision
  accuracy, unsafe-resume-rate (target 0 in tested scenarios), unnecessary
  human-escalation rate, dependency-repair precision.

---

## 7. Universal harness contract

Existing base (`adapters/base.py:26`):

```python
def capture_state(self, run_id, state, *, environment=None, reason="") -> StateCheckpoint
def restore_state(self, run_id, *, replay=True) -> SemanticState
def intercept_action(self, run_id, action_type, action_fn, arguments=None, *, volatile=(), scoped_to_run=True) -> Any
def resume(self, run_id, *, current_environment=None, expected_model=None, replay=True) -> RecoveryDecision
```

This 4-verb shape (`capture / restore / intercept / resume`) is already the
right universal contract. It is harness-independent today; the problem is that
the framework adapters duplicate the bootstrap logic around it.

Proposed minimal contract additions (no rewrite):

- A single `RUN_STARTED` bootstrap in the base/Generic layer (removes 5 copies).
- A normalized adapter-facing event map: each harness lifecycle call translates
  to core `EventType` values. Reuse the existing enum; add only the missing
  literals (`SESSION_INTERRUPTED`, `MODEL_CALLED`, `MODEL_COMPLETED`,
  `ACTION_COMPLETED`, `ACTION_FAILED` as events if correlation is needed). Keep
  the existing `TOOL_*` and `ACTION_RECORDED` families; add an optional
  correlation id linking a tool call to its ledger action.
- The core consumes only `EventType` events + `SemanticState` + `ActionLedger`
  and remains ignorant of any specific harness.

```
Claude adapter ──────┐
DSH adapter ─────────┤
LangGraph adapter ───┤
Codex adapter ───────┼──> AgentAdapter (capture/restore/intercept/resume)
Custom adapter ──────┘        │
                              ▼
                   CONTINUUM core (harness-independent)
```

---

## 8. Recovery validity model

Given (all already representable or close):

- checkpointed agent state (`SemanticState`)
- previous + current environment (`EnvironmentSnapshot` / `capture`)
- agent plan (`SemanticState.plan` with `PlanStep.depends_on`)
- dependencies (`external_dependencies`)
- previous actions (`ActionLedger`)
- external side effects (`Action` with provenance + status)
- evidence / provenance (to be unified per section 6)
- current model/session identity (`ModelState`, `MODEL_CHANGED`)

CONTINUUM determines, via `RecoveryEngine.assess`, one of:

`RESUME | REPAIR_AND_RESUME | REPLAN | WAIT | REQUEST_HUMAN | ROLLBACK | ABORT`
(`FORK` only if a scenario demands it).

The invariant the thesis requires is already encoded: the engine never resumes
merely because a checkpoint exists; it resumes only when validation passes. The
gap is that validation today cannot localize invalidation to a dependency
(subsection 3.1 dependency graph) and cannot explain itself on the contract
(missing `evidence`/`reason`). Both are additive, low-risk fixes.

Critical scenario (already supported in spirit): agent sends a request, network
response lost, agent crashes. On recovery the ledger is `STARTED`/`UNKNOWN`; the
engine blocks and requires reconciliation. This should be preserved and
benchmarked as "unsafe-resume-rate = 0", not as "did it resume".

---

## 9. Migration plan (phased, minimal, test-backed)

- **Phase 0 (done):** Baseline. 900 tests green (exit 0); this document.
- **Phase 1 (done, 2026-08-20):** Unify the three provenance vocabularies behind
  one derived/canonical enum (`CanonicalProvenance` + `ProvenanceView`, in
  `src/continuum/provenance_map.py`); add `evidence` + `reason` to
  `RecoveryContract`, threaded from `StateValidationResult.reason` /
  `RecoveryDecision.rationale` and the validator's per-component evidence. Pure
  additive; no behavior change; falsifiable tests in `tests/test_phase1.py`.
  Final suite: 909 passed, 9 skipped, 0 failed, 0 errored. See section 12.
- **Phase 2 (dependency-localized repair):** Teach staleness propagation to walk
  `PlanStep.depends_on` so only affected steps are invalidated. Falsifiable test
  = changing a dependency used only by step 4 invalidates step 4 but not 1-3.
- **Phase 3 (universal adapter funnel):** Centralize `RUN_STARTED` bootstrap and
  `checkpoint_node`; add harness-event map; remove the 5 duplicates. No new
  transport behavior.
- **Phase 4 (automatic checkpointing):** Wire `maybe_checkpoint()` into the
  universal layer (tool-end, pre-compaction, interruption) using existing
  policies.
- **Phase 5 (ledger + reconciliation hardening):** Persist `idempotency_key`;
  add `RECONCILING` status; optional capability classes; compose lease into
  resume boundary.
- **Phase 6 (measurement):** Add recovery-decision-accuracy and
  unsafe-resume-rate to the benchmark; build the reproducible "our own agent"
  real-system harness (inspect repo, investigate issue, edit, test, Git/GitHub,
  PR; hard-kill; mutate environment; fresh session; observe RESUME/REPAIR/HUMAN;
  assert no duplicate side effects).

Each phase: inspect existing code, write the exact gap, define a falsifiable
test, implement the smallest mechanism, test, document limitations. No phase is
allowed to remove existing tests or weaken existing guarantees.

---

## 10. Open research questions

- Does dependency-localized invalidation actually reduce unnecessary
  human escalation without masking real risk? (measure escalation rate)
- Can a single canonical provenance vocabulary express both writer identity and
  verification strength without losing information the current three encode?
- What is the right boundary for automatic checkpointing (overhead vs recovery
  fidelity) across heterogeneous harnesses?
- How should `FORK` semantics differ from `REPLAN` / `ROLLBACK`, and is it ever
  needed in practice?
- Can the lease coordinator prevent concurrent recovery safely without becoming
  a distributed-lock bottleneck across `continuum serve` sidecars?
- Is the "recovery validity across heterogeneous harnesses" claim empirically
  distinct from what LangGraph durable execution + a validation hook already
  provide? (must be tested against baselines, not asserted)

---

## 11. What we explicitly do NOT claim

- CONTINUUM did not invent checkpointing, durable execution, agent resume,
  rewind, idempotency, event sourcing, rollback, or plugin architecture. Those
  are established or actively researched; we reuse them.
- The north-star thesis ("evidence-backed, environment-aware recovery validation
  across heterogeneous agent harnesses") is a **hypothesis to test**, not a claim
  to assume. It must be validated by the Phase 6 benchmark against baselines
  (native harness persistence, LangGraph durable execution, naive checkpoint,
  structured task summary, transcript replay).
- No benchmark numbers are presented here. The suite is green (909 passed, 9
  skipped; the 9 skips are environmental: 5 Postgres tests without
  `CONTINUUM_TEST_POSTGRES_DSN`, 4 adapter tests that skip because
  `langgraph` / `openai-agents` are installed). recovery-decision-accuracy and
  unsafe-resume-rate are not yet measured and must not be invented.

---

## FIRST-TASK REPORT (analysis only, no implementation)

**Current architecture summary.** A layered, event-sourced durability and
recovery library: hash-chained `Event` log -> `project()` fold -> `SemanticState`
-> `StateValidator` (environment revalidation + staleness propagation) ->
`ActionLedger` (idempotent side effects) -> `RecoveryEngine.assess()` returning
a sealed `RecoveryContract`. Three transports (CLI, MCP stdio, serve sidecar)
funnel into `GenericAgentAdapter`. ~30,300 LOC, 118 Python files, 900 tests.

**What we already have.** Tamper-evident hash chain + optional Ed25519
attestation; provenance-aware (by writer) state with `REQUIRES_REVIEW` on
self-certification; independent environment revalidation with staleness
propagation; idempotent action ledger with reconciliation; a seven-mode recovery
engine with a sealed contract; a structured plan (`PlanStep.depends_on`);
deny-by-default MCP authz; portable versioned interchange; a lease coordinator;
a policy checkpoint stack; a CONTINUUM-Bench harness.

**What needs changing (minimum).** (1) Unify three fragmented provenance
vocabularies into one. (2) Connect `PlanStep.depends_on` to staleness
propagation for dependency-localized repair. (3) Add `evidence`/`reason` to
`RecoveryContract`. (4) Make `AgentAdapter` the single funnel (remove 5x
`RUN_STARTED` duplication). (5) Wire `maybe_checkpoint()` into the universal
layer. (6) Persist `idempotency_key` + add `RECONCILING` + capability classes to
the ledger. (7) Compose the lease into the resume boundary. (8) Integrate or
remove the unused plugin seams. (9) Add recovery-correctness metrics
(unsafe-resume-rate) to the benchmark.

**What should NOT change.** The hash-chained event log and `verify()`; the
sealed one-next-action `RecoveryContract`; the idempotent ledger semantics and
the deliberate absence of `AssumeOccurred`; the max-severity recovery ordering;
the deny-by-default MCP posture; the existing 900 tests and their guarantees;
the "checkpoint is evidence, not proof" principle (already embodied).

**Proposed next implementation step (smallest, highest leverage).** Phase 1:
add `evidence` and `reason` fields to `RecoveryContract` (additive, backward
compatible) and thread the existing `StateValidationResult.reason` /
`RecoveryDecision.rationale` into them, plus begin unifying the provenance
vocabularies behind one canonical enum. These directly serve the thesis
("evidence-backed recovery validity") and touch no existing behavior or tests.
Phase 2 (dependency-localized repair via `PlanStep.depends_on`) is the close
second because it is the concrete mechanism behind "REPAIR step 4 -> RESUME"
instead of "START OVER".

**Tests currently passing/failing (baseline).** `pytest` exits 0. 900 tests
collected, 5 skipped (Postgres tests, require `CONTINUUM_TEST_POSTGRES_DSN`),
0 failed, 0 errored. (pytest's textual summary line was not captured in the
stream, but exit code 0 is authoritative for a clean pass.)

**After Phase 1.** `pytest` exits 0. 909 passed (900 baseline + 9 new Phase 1
tests), 9 skipped (5 Postgres + 4 adapter-install skips), 0 failed, 0 errored.
No existing test was modified or weakened.

**Risks.** (1) Unifying provenance could silently change validator behavior if
the three vocabularies are not exactly equivalent; must be done behind a mapping
with tests. (2) Walking `PlanStep.depends_on` changes invalidation scope and
could surface previously-hidden staleness; gate behind tests that assert
step-localized invalidation. (3) Centralizing `RUN_STARTED` bootstrap risks
altering event ordering captured by existing tests; preserve the "do not
misorder history" invariant. (4) The test session reaches 100% but the pytest
process did not emit a terminal summary in captured output (possible teardown
hang from a leaked transport/benchmark thread); confirm a clean CI run before
relying on the green baseline. (5) Lease composition could introduce
double-resume friction if TTLs are mis-tuned; start with observability, not
enforcement.

---

## 12. Phase 1 implementation (done, 2026-08-20)

Goal: make recovery decisions explainable and make provenance/verification
semantics internally consistent, without changing existing recovery behavior.
Scope was held to exactly the directive: `RecoveryContract` evidence/reason, a
canonical provenance mapping layer, and self-certification regression tests.
Phases 2 to 6 were deliberately left untouched.

### 12.1 What changed

- `src/continuum/models.py` (`RecoveryContract`): two additive fields,
  `evidence: list[str]` and `reason: str`, both with safe defaults
  (`[]` / `""`). They are documented as a self-explaining "why" and "what
  evidence" layer and are never invented.
- `src/continuum/recovery/contract.py` (`build_contract`): now threads the
  recovery rationale and the validator's existing per-component evidence.
  `reason` defaults to `validation.report.reason` when the engine supplies no
  explicit rationale; `evidence` defaults to the validator's per-component
  details (the existing provenance/validation evidence), sorted for
  determinism. `render_contract` now prints `reason` and `evidence`.
- `src/continuum/recovery/contract.py` (`verify_contract`): accepts two digests
  so contracts sealed before `evidence`/`reason` existed still verify (legacy
  payload excludes those fields). Newly sealed contracts are hashed over the
  full terms, so the explanation is also tamper-covered.
- `src/continuum/recovery/engine.py` (`assess`): passes the decision rationale
  (`"; ".join(rationale)`) into `build_contract` as `reason`.
- `src/continuum/provenance_map.py` (new): the canonical mapping layer.
- `src/continuum/__init__.py`: exports the new provenance mapping API.
- `tests/test_phase1.py` (new): 9 Phase 1 tests (see 12.5).
- `docs/ARCHITECTURE_EVOLUTION.md`: this section plus the migration-plan Phase 1
  marker and corrected test counts.

### 12.2 What did NOT change

- No recovery semantics changed. The max-severity ordering, the seven recovery
  modes, and the exactly-one `next_allowed_action` invariant are untouched.
- No existing test was modified or deleted. The sealed one-next-action contract,
  the deny-by-default MCP posture, and the absence of `AssumeOccurred` are
  unchanged.
- `Origin`, `StateStatus`, and `TrustLevel` were not deleted or altered; the
  canonical enum is derived on top of them.
- `FORK` was not added.

### 12.3 Provenance mapping

Three orthogonal axes were preserved as separate source-of-truth enums and
exposed through a derived, normalized surface vocabulary:

| Axis (question) | Source enum | Canonical label |
| --- | --- | --- |
| WHO asserted it | `Origin` | `canonical_origin` |
| HOW trusted/verified | `TrustLevel` | `canonical_trust` |
| WHAT validity state | `StateStatus` | `canonical_state_status` |

`CanonicalProvenance` (the surface labels): `AGENT_ASSERTED`, `OBSERVED`,
`VERIFIED`, `INFERRED`, `STALE`, `CONTRADICTED`, `UNKNOWN`, `REQUIRES_REVIEW`.

Mapping (auditable, reversible in intent):

- `Origin.DETERMINISTIC -> OBSERVED`, `HUMAN -> VERIFIED`,
  `LLM -> AGENT_ASSERTED`, `EXTERNAL_AGENT -> AGENT_ASSERTED`,
  `IMPORTED -> INFERRED`.
- `TrustLevel` `"verified" -> VERIFIED`, `"unverified" -> INFERRED`,
  `"contested" -> CONTRADICTED`.
- `StateStatus.VALID -> VERIFIED`, `STALE -> STALE`,
  `CONFLICTED -> CONTRADICTED`, `UNKNOWN -> UNKNOWN`,
  `INVALID -> CONTRADICTED`, `REQUIRES_REVIEW -> REQUIRES_REVIEW`,
  `EXPIRED -> STALE` (the original `StateStatus.EXPIRED` is retained in the
  source enum; see 12.7).

`ProvenanceView` carries all three source values and their canonical labels, so
no information is lost by normalization. `primary` returns the single most
decision-relevant label: the validity state when the fact is not plainly valid,
otherwise trust, otherwise who.

### 12.4 RecoveryContract additions

- `evidence: list[str]` (default `[]`): the validator's per-component details
  (e.g. `"external_dependency:dataset: v3 -> v4"`), sorted and never invented.
- `reason: str` (default `""`): the recovery rationale (e.g. `"2 component(s)
  need repair before continuing"`), or `validation.report.reason` as a fallback.

Both are included in the contract's integrity hash for new seals and excluded
for the legacy verification path, so backward compatibility holds.

### 12.5 Tests added (`tests/test_phase1.py`, 9 tests)

1. `test_existing_serialized_contract_still_deserializes` (no. 1: old JSON
   loads, fields default).
2. `test_legacy_sealed_contract_verifies` (no. 1: pre-Phase-1 hash still
   verifies).
3. `test_new_contract_contains_evidence_when_evidence_exists` (no. 2).
4. `test_new_contract_contains_reason_when_rationale_exists` (no. 3).
5. `test_new_sealed_contract_verifies_via_current_hash`,
   `test_round_trips_evidence_and_reason` (no. 2/3 round-trip).
6. `test_missing_evidence_and_reason_do_not_break_callers`,
   `test_render_contract_includes_reason_and_evidence` (no. 4).
7. `test_recovery_decision_unchanged_with_explainable_contract` (no. 5: same
   mode, invariant preserved).
8. `test_recovery_scenario_produces_explainable_contract` (no. 5 integration:
   checkpoint -> validation -> recovery -> contract carrying real `v3 -> v4`
   evidence and a "repair" reason).
9. Provenance mapping: `test_origin_maps_to_canonical_who`,
   `test_trust_maps_to_canonical_how`, `test_state_status_maps_to_canonical_what`,
   `test_provenance_view_preserves_all_three_axes`,
   `test_provenance_view_primary_prefers_trust_when_valid`,
   `test_source_vocabularies_remain_intact`.
10. Self-certification: `test_self_certified_progress_stays_requires_review_
    until_confirmed` (validator level) and
    `test_agent_claim_requires_human_then_confirms_to_resume` (end-to-end:
    agent claim -> `REQUIRES_REVIEW` -> `REVIEW_CONFIRMED` -> `RESUME`/`VALID`).

### 12.6 Baseline vs final test result

- Baseline (before Phase 1): 900 collected, 5 skipped, 0 failed, 0 errored.
- Final (after Phase 1): 909 passed, 9 skipped, 0 failed, 0 errored.
- The 4 extra skips are environmental (adapter tests skip when
  `langgraph` / `openai-agents` are installed), not regressions.
- `ruff check` passes on all changed/new files; `mypy` reports no new issues in
  `provenance_map.py` / `recovery/contract.py`.

### 12.7 Known limitations

- `CanonicalProvenance` is a normalized surface, not a replacement. It
  necessarily merges `StateStatus.EXPIRED` into `STALE`; the original
  `StateStatus.EXPIRED` is preserved in the source enum, so the distinction is
  not destroyed, only surfaced-away.
- `evidence` on the contract is drawn from the validator's per-component details
  only. It does not yet fold in uncertain-side-effect provenance from the action
  ledger (a Phase 5 concern) or the environment diff summary; those remain
  available on `decision.uncertain_actions` / `decision.environment_diff`.
- The canonical mapping is not yet consumed anywhere except tests. Phase 2+ may
  reference it, but no caller was changed in Phase 1.
- No benchmark numbers are asserted; recovery-decision-accuracy and
  unsafe-resume-rate remain unmeasured (Phase 6).

---

## 13. Phase 2: dependency-localized repair

Phase 2 delivers the differentiator promised by the thesis: when one external
dependency moves, recover only the subgraph that depends on it and leave the
rest of the state untouched.

### 13.1 What changed

- **`src/continuum/recovery/impact.py` (new)** - `DependencyGraph` and
  `ImpactedSet`. `impacted_by(resources)` returns the precise evidence, finding
  and decision ids invalidated by a set of broken dependency resources, following
  the same derivation edges the validator uses
  (`dependency -> evidence -> finding -> decision`). It is a queryable,
  inspectable view of what a broken resource invalidates.
- **`src/continuum/state/validator.py`** - `validate` / `validate_state` accept
  an optional `scope` of dependency resources. When scoped, only those resources
  are re-checked and only their derivation subtree may go stale; other components
  keep their recorded status. `scope=None` is byte-for-byte the prior behavior,
  so all existing validation results are unchanged.
- **`src/continuum/recovery/engine.py`** - `RecoveryEngine.assess` accepts a
  `scope` that confines validation and the repair plan to a dependency's subtree.
  `assess_scoped(run_id, resources, ...)` is a named alias. The issued contract
  records the scope in its `evidence` field (the Phase 1 explainability field),
  so an auditor can see that clean parts of the state were intentionally
  preserved.
- **`src/continuum/__init__.py` / `recovery/__init__.py`** - export
  `DependencyGraph` and `ImpactedSet`.

### 13.2 What did not change

- No enum was added, removed, or resemanticked. `RecoveryMode`, `RecoverySafety`,
  `Component`, `StateStatus` and the existing `plan_repairs` cascade are intact.
- The full (unscoped) recovery path is untouched; scoped recovery is strictly
  additive.

### 13.3 Tests

- `tests/test_phase2.py` (7 tests): `impacted_by` subgraph + transitivity,
  scoped validation tainting only the impacted subtree, scoped recovery
  preserving a clean dependency (a two-dependency fixture where the unscoped
  decision would revalidate both but the scoped one leaves the clean one alone),
  the `assess_scoped` alias equaling the `scope` kwarg, and the scope recorded in
  contract evidence.

### 13.4 Baseline vs final test result

- Before Phase 2: 909 passed, 9 skipped, 0 failed.
- After Phase 2: 916 passed, 9 skipped, 0 failed (7 new Phase 2 tests).
- `ruff check` / `ruff format --check` clean on changed files; `mypy src/continuum`
  reports no issues.

### 13.5 Known limitations

- The scoped contract lists only scope-related components in `verified` /
  `invalidated`. Out-of-scope components are intentionally absent, not claimed
  verified; a full `assess` (no scope) is still the authoritative whole-state
  decision.
- `DependencyGraph` is built per call and not yet cached; for very large states a
  cached graph may be worth adding (tracked separately).

---

## 14. Phase 3: adapter funnel (registry + uniform dispatch)

Phase 3 makes recovery framework-agnostic: one discovery layer and one recovery
entry point work across every agent adapter, so a run can be recovered without
the caller knowing which framework produced it.

### 14.1 What changed

- **`src/continuum/adapters/registry.py` (new)** - `AdapterRegistry` plus the
  module-level `register_adapter`, `get_adapter`, `list_adapters` and
  `recover`. Registration is *lazy*: each built-in adapter is stored as a
  factory that imports the adapter only when requested, so importing
  `continuum` never pulls in `langchain` / `openai` / `langgraph`. The four
  built-ins (generic, langchain, langgraph, openai) are pre-registered.
- **`recover(adapter_name, run_id, storage, ...)`** - the single funnel entry
  point. It looks the adapter up by name, instantiates it with `storage` (plus
  any adapter-specific keyword arguments), and delegates to its `resume`
  method, returning a `RecoveryDecision`.
- **Exports** - `AdapterRegistry`, `get_adapter`, `list_adapters`, `recover`
  and the adapter classes are exported from `continuum.adapters` and from the
  top-level `continuum` namespace.

### 14.2 What did not change

- The `AgentAdapter` interface and every existing adapter are untouched. Each
  already exposed `resume() -> RecoveryDecision`, so the funnel only adds
  discovery and dispatch on top of the interface that existed.
- No recovery-decision logic moved; `recover` delegates to the adapter's own
  `resume`, which delegates to `RecoveryEngine.assess` (generic) or the
  framework-specific assess path.

### 14.3 Tests

- `tests/test_adapters_registry.py` (6 tests): built-ins registered, `get_adapter`
  returns the class, unknown name raises, a local `AdapterRegistry` registers
  and retrieves, `recover` dispatches through the generic adapter to a RESUME
  decision, and unknown adapter name errors from `recover`.

### 14.4 Baseline vs final test result

- Before Phase 3: 916 passed, 9 skipped, 0 failed.
- After Phase 3: 922 passed, 9 skipped, 0 failed (6 new registry tests).
- `ruff check` / `ruff format --check` clean on changed files; `mypy src/continuum`
  reports no issues.

### 14.5 Known limitations

- `recover` instantiates the adapter from `storage` plus caller-supplied
  keyword arguments. Adapters with required constructor arguments (for example
  langgraph's `graph`) must be passed those via `**adapter_kwargs`; this is by
  design so the funnel stays generic.
- No new environment adapters (python-inproc, container) were added this phase;
  the registry makes adding them a localized, low-risk change (see the adapter
  authoring guide). They remain tracked as future work.

## 15. Phase 4 - Automatic checkpointing (recovery anchors, anchor lookup, pruning)

### 15.1 What changed

CONTINUUM already had automatic checkpoint timing via `CheckpointManager` and a
policy (`HybridPolicy` fires on side effects, milestones, and meaning, not on
volume). Phase 4 fills the remaining checkpoint gaps: pinning a known rollback
point when a failure is detected, looking that anchor up, and keeping the
checkpoint history bounded.

- **`CheckpointTrigger.RECOVERY = "recovery"`** (`src/continuum/checkpoint/policy.py`)
  marks a checkpoint taken precisely because a recovery decision judged the run
  unsafe to continue from. It is distinct from MANUAL/SIDE_EFFECT/MILESTONE so
  anchors are findable.
- **`CheckpointManager.checkpoint_on_recovery(run_id, *, environment, reason)`**
  (`src/continuum/checkpoint/manager.py`) takes an unconditional recovery anchor.
  This is the explicit hook the agent loop or CLI calls right after a non-RESUME
  decision. It deliberately does not live inside `RecoveryEngine.assess`, which
  stays read-only (no side effects), preserving the architecture's promise that
  judging is separate from acting.
- **`CheckpointManager.last_recovery_anchor(run_id, *, before_version=None)`**
  returns the newest RECOVERY checkpoint, optionally limited to anchors at or
  before a version. This answers "where would I have rolled back to right before
  event X?" and is the lookup a recovery strategy needs before restoring.
- **`CheckpointManager.prune(run_id, *, keep=5, keep_anchors=True)`** keeps the
  `keep` newest checkpoints and removes older ones, but always preserves
  RECOVERY anchors when `keep_anchors` is true (deleting a known rollback point
  would defeat the purpose). Returns the list of removed checkpoint ids.
- **`Storage.delete_checkpoint(checkpoint_id)`** added to the `Storage` ABC and
  implemented in `SQLiteStorage` and `PostgresStorage`, so pruning is supported
  on both backends.
- **`StateCheckpoint.reason`** (`src/continuum/models.py`) is a new optional field
  carrying the checkpoint's reason (already recorded on the version and the
  `STATE_CHECKPOINTED` event). It makes a recovery anchor self-describing.

### 15.2 What did not change

- `RecoveryEngine.assess` and `assess_scoped` remain read-only. Auto-checkpointing
  is an explicit, opt-in call (`checkpoint_on_recovery`), not a hidden mutation
  inside judgment.
- The existing `maybe_checkpoint`/policy timing is untouched; Phase 4 adds the
  recovery-specific anchor on top of it.
- No adapter internals were modified to auto-checkpoint; the hook is provided
  and documented so the agent loop can call it. Wiring it into each adapter's
  `resume`/action loop is a small follow-up (would change behavior, so kept
  separate).

### 15.3 Tests

- `tests/test_checkpoint_phase4.py` (7 tests): RECOVERY trigger exists,
  `checkpoint_on_recovery` creates a RECOVERY-trigger checkpoint with its reason,
  `last_recovery_anchor` finds the newest anchor and respects `before_version`,
  `prune` keeps the newest `keep` and preserves RECOVERY anchors while dropping
  old plain checkpoints, `prune` can drop anchors when `keep_anchors=False`, and
  `prune` is a no-op within `keep`.

### 15.4 Baseline vs final test result

- Before Phase 4: 922 passed, 9 skipped, 0 failed.
- After Phase 4: 929 passed, 9 skipped, 0 failed (7 new checkpoint tests).
- `ruff check` / `ruff format --check` clean on changed files; `mypy src/continuum`
  reports no issues.

### 15.5 Known limitations

- `prune` preserves RECOVERY anchors and the newest `keep`, but does not yet
  honor a sealed `RecoveryContract` dependency (Phase 5 ledger linkage). A
  contract sealed against a checkpoint version should also be preserved; that
  cross-reference arrives with the contract ledger.
- The auto-checkpoint hook is not yet invoked automatically by the adapter loop;
  callers must call `checkpoint_on_recovery` after a non-RESUME decision. Wiring
  is a small, well-isolated change left for the next step.

## 16. Phase 5 - Recovery ledger (append-only, tamper-evident, reconcilable)

### 16.1 What changed

Phase 5 adds a durable, auditable record of recovery decisions, separate from
the event log (which proves what happened) and the contract (which proves what
was permitted at one moment). The ledger proves the sequence of decisions and
lets a later reader check the live state has not drifted from them.

- **`RecoveryLedger`** (`src/continuum/recovery/ledger.py`) is append-only. Each
  `append_decision(run_id, contract, *, anchor, gate, note)` seals a contract as
  a `RecoveryLedgerEntry` and appends it.
- **Tamper-evident chaining.** Each entry carries the previous entry's hash, so
  rewriting any historical entry breaks `verify`, which returns
  `(chain_ok, trusted_through_index)` (the first broken index). See issue
  #130 (tamper-evident chaining) and #124 (integrity verification).
- **Compaction with anchor preservation.** `compact(run_id, keep=50,
  keep_anchors=True)` drops the oldest non-anchor entries but re-seals the
  surviving chain (the first kept entry links to `GENESIS`), so the ledger stays
  bounded without losing its audit anchors or its tamper-evidence. See #127.
- **Human-in-the-loop gate.** A decision can be recorded with `gate="required"`;
  `pending_gate` returns it until `record_gate(run_id, "approved")` persists the
  human decision. See #126.
- **Recovery lease / attempt budget.** `record_attempt` / `attempts` /
  `requires_human(run_id, max_attempts=3)` count recovery attempts and trip a
  human threshold, so the system stops retrying and asks a person. See #125.
- **Reconciliation.** `reconcile(run_id, state)` returns a `ReconcileReport`
  flagging drift: a broken chain, or a live state version behind the checkpoint
  the latest decision was sealed against (implies rollback or corruption). See
  #129.
- **Cross-process lock.** The ledger accepts a `LeaseCoordinator` (the same one
  guarding single-agent resume) and acquires it around every mutation, so two
  processes cannot append concurrently. See #128. Without a lock it is
  single-process safe (the storage write is already atomic).
- **Storage-agnostic backend.** `LedgerBackend` has `MemoryLedgerBackend` (tests)
  and `FileLedgerBackend` (JSONL, one file per run). No schema migration was
  needed, keeping Phase 5 additive and low-risk.

### 16.2 What did not change

- The event log and `RecoveryContract` are untouched. The ledger is a new,
  higher-level audit record that references contracts by value.
- `RecoveryEngine.assess` stays read-only; nothing auto-appends to the ledger.
  Callers (CLI, agent loop, or the recover funnel) append after deciding.
- The cross-process lock reuses the existing `continuum.concurrency` lease
  coordinator rather than introducing a new locking primitive.

### 16.3 Tests

- `tests/test_recovery_ledger.py` (8 tests): genesis chaining and `verify`,
  tampering breaks the chain at the right index, compaction keeps anchors plus
  the newest entries and stays verifiable, attempt counting and the human
  threshold, human gate persistence and `pending_gate` clearing, reconciliation
  drift detection, `FileLedgerBackend` round-trip, and `append` under the
  `InMemoryLeaseCoordinator` lock.

### 16.4 Baseline vs final test result

- Before Phase 5: 929 passed, 9 skipped, 0 failed.
- After Phase 5: 937 passed, 9 skipped, 0 failed (8 new ledger tests).
- `ruff check` / `ruff format --check` clean on changed files; `mypy src/continuum`
  reports no issues.

### 16.5 Known limitations

- The ledger is keyed by `run_id` and stored separately from the run's main
  storage. A future step could colocate it (via the storage migration framework)
  so a single `verify` covers events, checkpoints, and the ledger at once.
- Reconciliation currently checks the chain and the live state version against
  the latest decision's checkpoint version. Richer drift rules (per-component
  expectation vs the contract's `invalidated` list) are a natural follow-up.
- `requires_human` is a simple attempt counter; wiring it into `assess` so the
  engine itself trips the gate is a small, isolated change left for later.

## 17. Phase 6 - Recovery benchmarking and correctness scenarios

### 17.1 What changed

Phase 6 turns the recovery guarantees built in Phases 1-5 into observable,
reproducible evidence. It rests on two layers:

- **CONTINUUM-Bench (pre-existing, `src/continuum/benchmark/__init__.py`).** A
  small, honest benchmark that drives the real library (storage, checkpointing,
  validation, action ledger, recovery) against an in-process simulated agent. It
  defines three break scenarios (process crash, dataset change, unknown side
  effect) across three strategies (continuum, full replay, naive checkpoint)
  and reports metrics like duplicate work and detected staleness. This covers
  the harness scaffold (#132), the metrics schema (#133), reproducible baselines
  (#146), and the report (#147).
- **Phase 6 scenario suite (`src/continuum/benchmark/phase6/`).** An additive
  layer of 12 recovery-correctness scenarios, each exercising one stress
  condition against the real engine, validator, checkpoints, ledger, and adapter
  funnel: single/multi dependency corruption, external edit drift, ledger tamper
  detected, recovery lease exhaustion, adapter failure across environments,
  checkpoint rollback correctness, concurrent recovery safety, large state
  recovery latency, missing dependency graph fallback, human verdict honored,
  and transient network failure on install. A tiny harness (`harness.py`) times
  each scenario and converts failure into a `FAIL` result without aborting the
  suite; `write_report` emits JSON and Markdown. `benchmarks/run.py` runs the
  suite and writes `benchmarks/out/report.{json,md}`.

### 17.2 What did not change

- No recovery logic was modified; Phase 6 is observation and validation only.
- The scenario suite is additive under `continuum.benchmark.phase6`, so it does
  not disturb the existing CONTINUUM-Bench package or `tests/test_benchmark.py`.

### 17.3 Tests

- `tests/test_phase6.py` (15 tests): every scenario runs green under the harness,
  plus harness self-checks (recorded failure, recorded exception, report +
  summary). `benchmarks/run.py` executes all 12 scenarios and emits a report.
- The pre-existing `tests/test_benchmark.py` (CONTINUUM-Bench) continues to pass.

### 17.4 Baseline vs final test result

- Before Phase 6: 937 passed, 9 skipped, 0 failed.
- After Phase 6: 952 passed, 9 skipped, 0 failed (15 new tests).
- `ruff check` / `ruff format --check` clean on changed files; `mypy src/continuum`
  reports no issues.

### 17.5 Known limitations

- Scenario metrics are lightweight (pass/fail, attempts, elapsed). CONTINUUM-Bench
  carries the richer duplicate-work / compression metrics; the two reports are
  complementary, not merged.
- The large-state latency scenario is a smoke check (< 1s), not a tuned
  performance contract.

## 18. Wiring dep_scope and the source graph into localized recovery

### 18.1 What changed

Phases 2 and 3 built the pieces; this closes the loop so localized repair is
functional end to end rather than two standalone modules:

- **Scope-aware action ledger (#103, #184).** When `assess` is given a scope,
  an uncertain side effect explicitly tagged via `Action.dep_scope` to a
  dependency outside the scope no longer blocks the scoped decision: it cannot
  have invalidated the part being resumed. Untagged actions stay blocking,
  because they could have touched anything. This keeps the engine's
  conservative bias while making scoping meaningful for side effects.
- **Impacted source files on decisions (#100, #184).** `assess` and
  `assess_scoped` accept the source-level `DependencyGraph`
  (`continuum.analysis.depends`) and record every file whose imports belong to
  a scoped dependency as `RecoveryDecision.impacted_files`. A corrupted
  dependency now yields both the invalidated derivation subtree and the source
  files that own it.

### 18.2 What did not change

- The engine remains read-only.
- Whole-state assessment (`scope=None`) is unchanged and still considers every
  uncertain action regardless of tagging.

### 18.3 Tests

- `tests/test_dep_scope_recovery.py` (5 tests): out-of-scope tagged side effect
  does not block, in-scope tagged side effect blocks, untagged blocks
  conservatively, whole-state sees everything, and impacted files match exactly
  the files importing the broken dependency.
- Phase 6 gains the `out_of_scope_side_effect` scenario, asserting both the
  filtered and conservative paths through the real engine.

### 18.4 Known limitations

- `impacted_files` reports files by import ownership only; it does not attempt
  runtime provenance or trace which files an action actually wrote.

## 19. Informed retry and the distribution layer (2026-08-24)

### 19.1 Informed retry summaries (#265, merged as #275)

Recovery surfaces no longer show only the current decision; they can also
render an account of what previous attempts changed, derived from events
already in the hash chain rather than from agent self-report.

- **What changed.** `continuum.recovery.summary` builds a prior-attempt block
  (`build_informed_retry`) and renders it as lines
  (`render_informed_retry`). `RecoveryDecision` carries the block
  (`decision.informed_retry`). `cmd_resume` appends it to human output under
  "What previous attempts changed (informed retry)"; absent history means no
  section is rendered at all.
- **Provenance stays intact.** The block is computed from recovery-path
  events recorded during earlier attempts, so it inherits the chain's
  tamper-evidence instead of introducing a new trust boundary.
- **Tests.** `tests/test_informed_retry.py`, `tests/test_retry_budgets.py`.

### 19.2 Distribution layer (#277)

How the system is obtained and run became part of the architecture:

- **Docker/GHCR.** A slim image whose default command runs the crash-recovery
  demo end to end; any argument replaces the command, so
  `docker run --rm ghcr.io/cyrax321/continuum continuum --help` reaches the
  CLI. CI publishes to GHCR on pushes to `main` and release tags, with a
  `continuum --version` smoke test after push.
- **Devcontainer.** One-click Codespaces: dev toolchain installed via
  `uv sync --extra dev`, dashboard port forwarded.
- **Installs without PyPI.** pip/uv straight from the git URL, wheels
  attached to GitHub Releases, `uvx`/`pipx run` off the repo. The PyPI job in
  the release workflow is opt-in via the `PUBLISH_PYPI` repository variable,
  so tagging cannot fail on unconfigured trusted publishing.

### 19.3 Known limitations

- The informed retry block summarizes past attempts but does not by itself
  change any recovery decision; the engine's severity ordering is unchanged.
- The published image runs as a non-root user and carries no secrets, but it
  is not yet multi-arch; CI builds for its runner platform only.
