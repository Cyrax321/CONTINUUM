# CONTINUUM, Master Plan, Status, Issues, Limitations, and Next Work

> Single consolidated source of truth (as of 2026-08-20). Companion to
> `STATUS.md` (deep operational detail), `docs/ARCHITECTURE_EVOLUTION.md`
> (north-star migration plan), `CHANGELOG.md`, and the "Differentiator &
> Shipping Plan" brief. New contributors start with `docs/CONTRIBUTING_ONBOARDING.md`.
> Terms are defined in `docs/GLOSSARY.md`. Threats and assumptions are scoped in `docs/threat_model.md`.
>
> Scope rule: this document records what is verified, what is believed, and what
> is neither. No benchmark numbers are invented. The thesis is a hypothesis to
> test, not a claim.

---

## 1. Goal and thesis

CONTINUUM is a **universal recovery-judgment / recovery-validation layer for
autonomous AI agents**. It is not another agent harness, not a durable-execution
runtime, not primarily a memory system, not primarily a workflow orchestrator,
and not primarily a checkpoint database.

It answers a different question than those systems:

> "Given the state recorded at time T and the world that exists now, is it still
> safe and semantically valid to continue from that state?"

The core principle:

> **A checkpoint is evidence of what was believed/recorded at time T, not proof
> that continuing from that state is still correct.**

Conceptually:

```
AGENT HARNESS
     |
     v
DURABLE EXECUTION      (survive a crash, resume from the journal)
     |
     v
CONTINUUM             (judge whether the journal is still valid to continue)
     +--> environment validation
     +--> provenance / evidence
     +--> dependency analysis
     +--> side-effect reconciliation
     +--> recovery decision
     |
     v
RESUME / REPAIR / HUMAN
```

### 1.1 Differentiators (the four properties to converge on)

1. **External-world validation.** Compare checkpoint-time assumptions against
   the *current* external world (git, filesystem, dependencies, DB, APIs, cloud,
   credentials, browser). Distinguish internal checkpoint state from current
   external reality. Do not blindly invalidate everything; determine which
   assumptions changed and whether they matter.
2. **Dependency-graph-localized repair.** Use the agent's actual plan
   (`PlanStep.depends_on`) so invalidation is surgical: "repair step 4, not
   restart the run." This is the flagship technical differentiator (Phase 2).
3. **Universal recovery contract.** A sealed, harness-independent
   `RecoveryContract` with one next action plus `evidence`/`reason`, consumable
   by any orchestrator, including ones that already have their own durable
   execution (Temporal, Diagrid, LangGraph checkpointer). CONTINUUM judges what
   the journal means; it does not replace the journal.
4. **Published recovery-correctness benchmark.** Measure unsafe-resume-rate,
   recovery-decision-accuracy, unnecessary-human-escalation-rate,
   dependency-repair precision, in a field that recently flagged this as
   unbenchmarked (Phase 6).

### 1.2 Competitive landscape (honest)

- Durable execution is commodity (Temporal, Cloudflare Workflows, DBOS, Restate,
  Inngest, AWS Lambda Durable Functions, Microsoft Durable Task). Do not compete.
- Diagrid Catalyst 2.0 is the closest competitor: hash-chained, signed,
  multi-framework durable execution. Do not lead with attestation. The gap they
  leave open: they prove what ran, they do not judge whether it is still safe to
  continue. That gap is ours.
- Recent papers (Cordon, MemTX, AgentRewind, SCOPEGATE, verified-tool-calls)
  independently converged on adjacent ideas. This validates the problem and
  shrinks the head start; it does not eliminate the angle. MemTX's cascading
  repair is adjacent to Phase 2 (external world + plan graph vs internal memory
  graph): cite and differentiate, do not imply priority.

---

## 2. Current state (verified)

- Codebase: ~204 tracked files, 118 Python files, ~30,300 LOC. `src/continuum`
  has 60 files / ~14,800 LOC. ~45 test files, **909 tests passing, 9 skipped, 0
  failed, 0 errored** (skips are environmental: 5 Postgres without
  `CONTINUUM_TEST_POSTGRES_DSN`, 4 adapter tests that skip because `langgraph` /
  `openai-agents` are installed). `ruff` clean; `mypy` has ~20 pre-existing
  errors unrelated to this work.
- Three transports (CLI, MCP stdio, serve sidecar) funnel into
  `GenericAgentAdapter`.
- Layers (all with tests): hash-chained `Event` log -> `project()` fold ->
  `SemanticState` -> `StateValidator` (environment revalidation + staleness
  propagation) -> `ActionLedger` (idempotent side effects) ->
  `RecoveryEngine.assess()` returning a sealed `RecoveryContract`.

### 2.1 What already exists (do not rebuild)

- Tamper-evident hash chain + optional Ed25519 attestation.
- Provenance-aware (by writer) state with `REQUIRES_REVIEW` on
  self-certification; `REVIEW_CONFIRMED` event clears it.
- Independent environment revalidation with staleness propagation
  (`dependency -> evidence -> finding -> decision`).
- Idempotent action ledger with reconciliation; deliberately no
  `AssumeOccurred`.
- Seven-mode recovery engine with a sealed contract; max-severity ordering so
  the most cautious signal wins regardless of evaluation order.
- Structured plan (`PlanStep.depends_on`); deny-by-default MCP authz; portable
  versioned interchange; lease coordinator; policy checkpoint stack;
  CONTINUUM-Bench harness.

### 2.2 Recent work (this session)

- PR wave merged to `main`: #89, #90, #92, #93, #96, #97, #99. #98 closed as a
  duplicate of #97. Contributors: `abyyxhek` (authored #89/#92/#96, diagnosed
  #87/#94, reported #91/#95) and `Adhi1-2` (authored #90/#93/#97/#99).
- `continuum-mcp` name fix (`.mcp.json` key + `MCPServer(name="continuum-mcp")`)
  for issue #87.
- Contributor avatars added.
- **Phase 1 implemented and tested** (see section 4 and
  `docs/ARCHITECTURE_EVOLUTION.md` section 12).

---

## 3. Complete phased plan

Two compatible orderings exist:

- The **engineering** order (Phase 0-6) from `docs/ARCHITECTURE_EVOLUTION.md`.
- The **shipping-impact** reorder (v0.1-v0.5) from the Differentiator brief,
  which pulls the benchmark forward.

Both are shown. Each phase is inspect, understand, identify overlap, define
invariant, write tests, implement minimally, run tests, document. **No phase may
remove existing tests or weaken existing guarantees.**

### 3.1 Engineering phases (ARCHITECTURE_EVOLUTION.md)

| Phase | Name | What | Falsifiable test | Status |
| --- | --- | --- | --- | --- |
| 0 | Baseline | Repo audit, test baseline, this doc | `pytest` green | DONE |
| 1 | Provenance + contract clarity | Unify 3 provenance vocabularies behind one derived enum; add `evidence` + `reason` to `RecoveryContract` | contract carries rationale + real evidence for a known scenario | DONE (2026-08-20) |
| 2 | Dependency-localized repair | Teach staleness propagation to walk `PlanStep.depends_on` so only affected steps are invalidated | changing a dependency used only by step 4 invalidates step 4 but not steps 1-3 | NOT STARTED |
| 3 | Universal adapter funnel | Centralize `RUN_STARTED` bootstrap and `checkpoint_node`; add harness-event map; remove 5x duplication | one normalized event map; no duplicated bootstrap | NOT STARTED |
| 4 | Automatic checkpointing | Wire `maybe_checkpoint()` into the universal layer (tool-end, pre-compaction, interruption) using existing policies | checkpoint fires at meaningful boundaries, not every token | NOT STARTED |
| 5 | Ledger + reconciliation hardening | Persist `idempotency_key`; add `RECONCILING` status; optional capability classes; compose lease into resume boundary | uncertain action reaches `RECONCILING` then confirmed/needs-human, never blind replay | NOT STARTED |
| 6 | Measurement | Add recovery-decision-accuracy and unsafe-resume-rate to the benchmark; build reproducible real-system harness | measured unsafe-resume-rate, not invented | NOT STARTED |

### 3.2 Shipping reorder (Differentiator brief)

| Order | Phase | Why moved |
| --- | --- | --- |
| v0.1 | Phase 1 (provenance + `evidence`/`reason`) | additive, ~2-3 days, zero behavior change, foundation |
| v0.2 | Phase 2 (dependency-localized repair) | flagship differentiator; demoable in <5 min via the GitHub-issue-PR crash scenario |
| v0.3 | Phase 6 benchmark (moved up) | the unbenchmarked gap is time-sensitive; ship before a competitor or paper closes it |
| v0.4 | Phase 3 (universal adapter funnel) | needed for credibility once there is a story; multi-framework support is now the expected bar |
| v0.5 | Phases 4-5 (auto-checkpoint, ledger/lease hardening) | production-readiness for design partners, not launch-day differentiation |

### 3.3 What must NOT change in any phase

- The hash-chained event log and `verify()`.
- The sealed one-next-action `RecoveryContract`.
- The idempotent ledger semantics and the deliberate absence of `AssumeOccurred`.
- The max-severity recovery ordering.
- The deny-by-default MCP posture.
- The "checkpoint is evidence, not proof" principle.
- `FORK` mode is not introduced unless a concrete scenario demands it.

---

## 4. Phase 1 implementation (done, 2026-08-20)

Goal: make recovery decisions explainable and make provenance/verification
semantics internally consistent, without changing existing recovery behavior.

See also docs/recovery_walkthrough.md for an end-to-end trace of one failure
from adapter error to sealed contract, generated from real output.

### 4.1 What changed

- `src/continuum/models.py` (`RecoveryContract`): additive `evidence: list[str]`
  and `reason: str`, both defaulted.
- `src/continuum/recovery/contract.py`: `build_contract` threads `reason`
  (from `RecoveryDecision.rationale`, falling back to `StateValidationResult.reason`)
  and `evidence` (validator's per-component details, sorted, never invented);
  `verify_contract` accepts a legacy digest so pre-Phase-1 sealed contracts still
  verify; `render_contract` prints `reason`/`evidence`.
- `src/continuum/recovery/engine.py`: `assess()` passes the decision rationale
  into `build_contract`.
- `src/continuum/provenance_map.py` (new): canonical mapping layer
  (`CanonicalProvenance`, `ProvenanceView`, `summarize`, `canonical_origin`/
  `canonical_trust`/`canonical_state_status`).
- `src/continuum/__init__.py`: exports the new provenance API.
- `tests/test_phase1.py` (new): 9 tests.

### 4.2 Provenance mapping (the three orthogonal axes preserved)

| Axis (question) | Source enum | Canonical label |
| --- | --- | --- |
| WHO asserted it | `Origin` | `canonical_origin` |
| HOW trusted/verified | `TrustLevel` | `canonical_trust` |
| WHAT validity state | `StateStatus` | `canonical_state_status` |

`CanonicalProvenance`: `AGENT_ASSERTED`, `OBSERVED`, `VERIFIED`, `INFERRED`,
`STALE`, `CONTRADICTED`, `UNKNOWN`, `REQUIRES_REVIEW`.

Mapping (auditable, reversible in intent):

- `Origin.DETERMINISTIC -> OBSERVED`, `HUMAN -> VERIFIED`,
  `LLM -> AGENT_ASSERTED`, `EXTERNAL_AGENT -> AGENT_ASSERTED`,
  `IMPORTED -> INFERRED`.
- `TrustLevel` `"verified" -> VERIFIED`, `"unverified" -> INFERRED`,
  `"contested" -> CONTRADICTED`.
- `StateStatus.VALID -> VERIFIED`, `STALE -> STALE`, `CONFLICTED ->
  CONTRADICTED`, `UNKNOWN -> UNKNOWN`, `INVALID -> CONTRADICTED`,
  `REQUIRES_REVIEW -> REQUIRES_REVIEW`, `EXPIRED -> STALE` (original
  `StateStatus.EXPIRED` retained in the source enum).

`ProvenanceView` carries all three source values and their canonical labels, so
no information is lost by normalization. `primary` returns the single most
decision-relevant label: validity state when not plainly valid, else trust, else
who.

### 4.3 What did NOT change

No recovery semantics; the exactly-one `next_allowed_action` invariant, severity
ordering, seven recovery modes, and deny-by-default posture are untouched.
`Origin`/`StateStatus`/`TrustLevel` were not deleted or altered. `FORK` not
added. No existing test modified or weakened.

### 4.4 Tests added (9, all green)

Backward-compat deserialize + legacy-seal verify; evidence/reason present when
available; missing fields do not break callers; round-trip; recovery behavior
unchanged (same mode + invariant); integration scenario (checkpoint -> validation
-> recovery -> contract carrying real `v3 -> v4` evidence and a "repair" reason);
full provenance-mapping coverage; self-certification regression (validator-level
`REQUIRES_REVIEW` until confirmed -> `VALID`, and end-to-end agent-claim ->
`REQUEST_HUMAN` -> `REVIEW_CONFIRMED` -> `RESUME`/`VALID`).

### 4.5 Known limitations (Phase 1)

- `CanonicalProvenance` is a normalized surface, not a replacement; `EXPIRED`
  merges into `STALE` (original retained in source enum).
- Contract `evidence` is drawn from the validator's per-component details only;
  it does not yet fold in uncertain-side-effect provenance from the action ledger
  (Phase 5) or the environment diff summary.
- The canonical mapping is not yet consumed outside tests.
- No benchmark numbers asserted; recovery-decision-accuracy and
  unsafe-resume-rate remain unmeasured (Phase 6).

---

## 5. Issues (open, with disposition)

### 5.1 Open contributor issues (from STATUS.md launch audit)

These are real but non-launch-critical; left open as contributor work.

| Issue | One-line impact | Disposition |
| --- | --- | --- |
| #29 | `ActionLedger.reconcile(occurred=False)` leaves stale `external_id`/`result` | Closed: `reconcile` clears `external_id`, `result`, and `result_hash` on occurred-false path (`src/continuum/actions/ledger.py`) |
| #30 | `FileProvider` reports a missing file as `version=None`, so diff marks it `changed` not `removed` | Closed: `FileProvider.capture` omits missing files entirely so diff classifies them `REMOVED` (`src/continuum/environment/snapshot.py`) |
| #33 | `identity_tokens` drops plain-word resource ids (requires digit/`@`/`.`) | Closed: `_is_strong_token` accepts plain words of sufficient length (`src/continuum/actions/idempotency.py`) |
| #34 | `ActionLedger scoped_to_run=False` does not enforce global uniqueness across runs as documented | Closed: cross-run uniqueness limitation clarified in `idempotency_key` docstring (`src/continuum/actions/idempotency.py`) |
| #36 | `identity_tokens` drops purely-numeric resource ids, so cross-session fallback fails on numeric ids | Closed: `identity_tokens` collects integer scalars as tokens (`src/continuum/actions/idempotency.py`) |
| #42 | Strict mode: uncertain side effect yields `REQUEST_HUMAN` but an auto-reconcile step silently ignores `strict_unknown` | Closed: `plan_repairs` marks reconcile steps `requires_human` in strict mode (`src/continuum/recovery/planner.py`) |
| #43 | Two checkpoints at the same state version collapse to one in `continuum history` | Closed: `cmd_history` lists every checkpoint row instead of keying by version (`src/continuum/cli/main.py`) |
| #45 | `claim(on_unknown=)` resolution is not persisted, so the ledger stays uncertain after call-time resolution | Closed: `claim` records an `on_unknown` resolution as an `ACTION_RECONCILED` event (`src/continuum/actions/ledger.py`) |
| #49 | `StateValidator._check_model` reports model-specific assumptions `VALID` when `expected_model` is `None` (fail-open) | Closed: `_check_model` reports `UNKNOWN` when assumptions exist but either model is unknown (`src/continuum/state/validator.py`) |

### 5.2 Open behavioral questions (not yet filed / not yet resolved)

- **Autonomous usage.** No independent LLM has yet chosen, on its own initiative,
  to call `continuum_checkpoint` / `continuum_resume` without being told the
  exact steps. Verified with scripted clients and e2e kits, but the autonomous
  motivation question is open.
- **Checkpoint version on resume.** `continuum_resume` has reported
  `checkpoint_version: 0` even after a checkpoint was taken. Whether the resume
  contract reflects checkpoints at all is not established. Not filed yet.
- **Stale editable metadata.** `pip show continuum-agent` reports a pre-move
  path; cosmetic, fixed by a clean editable reinstall.

### 5.3 Open research questions (from ARCHITECTURE_EVOLUTION.md)

1. Does dependency-localized invalidation reduce unnecessary human escalation
   without masking real risk? (measure escalation rate)
2. Can a single canonical provenance vocabulary express both writer identity and
   verification strength without losing information the three encode? (Phase 1
   answered: yes, via the derived view)
3. What is the right boundary for automatic checkpointing (overhead vs recovery
   fidelity) across heterogeneous harnesses?
4. How should `FORK` semantics differ from `REPLAN` / `ROLLBACK`, and is it ever
   needed in practice?
5. Can the lease coordinator prevent concurrent recovery safely without becoming
   a distributed-lock bottleneck across `continuum serve` sidecars?
6. Is the "recovery validity across heterogeneous harnesses" claim empirically
   distinct from what LangGraph durable execution + a validation hook already
   provide? (must be tested against baselines, not asserted)

---

## 6. Limitations (what we explicitly do NOT claim)

- CONTINUUM did not invent checkpointing, durable execution, agent resume,
  rewind, idempotency, event sourcing, rollback, or plugin architecture. Those
  are established or actively researched; we reuse them.
- The north-star thesis is a **hypothesis to test**, not a claim to assume. It
  must be validated by the Phase 6 benchmark against baselines (native harness
  persistence, LangGraph durable execution, naive checkpoint, structured task
  summary, transcript replay).
- No benchmark numbers are presented. unsafe-resume-rate,
  recovery-decision-accuracy, unnecessary-human-escalation-rate, and
  dependency-repair precision are not yet measured and must not be invented.
- We do not compete with durable-execution runtimes (Temporal, Cloudflare
  Workflows, DBOS, Restate, Inngest, AWS, Microsoft). CONTINUUM is
  complementary ("bring your own durability").
- MCP authorization is by declared `clientInfo`, not authenticated identity. It
  keeps honestly-named coexisting agents out of each other's runs; it does not
  defend against a deliberately impersonating or malicious local process.
- The #6 deduplication proof is reproducible and not asserted: `continuum
  benchmark` reports 0 duplicate side effects for stable-key/drift strategies
  while `naive_retry`/`replay` repeat every side effect.

---

## 7. What we can work on effortlessly next

Ordered by leverage and low risk. Each item is a concrete, well-scoped task with
a known falsifiable test, so it can be picked up without re-deriving context.

### 7.1 Phase 2: Dependency-localized repair (highest leverage, next)
- **Task.** Teach staleness propagation to walk `PlanStep.depends_on`. A change
  affecting only step 4 invalidates step 4 (and downstream dependents) but not
  steps 1-3.
- **Where.** `src/continuum/state/validator.py` (`_propagate`); `PlanStep` in
  `src/continuum/models.py`.
- **Falsifiable test.** Dependency change used only by step 4 invalidates step 4
  and not 1-3; transitive downstream invalidation; unrelated change leaves all
  valid; stale step repaired then downstream revalidated; cyclic dependency
  rejected/handled.
- **Risk.** Low-Medium. Gate behind tests asserting step-localized
  invalidation; do not change the max-severity ordering or recovery modes.
- **Effort.** Medium (~3-5 days).

### 7.2 Phase 3: Universal adapter funnel
- **Task.** Single `RUN_STARTED` bootstrap + `checkpoint_node`; harness-event map;
  remove the 5x `RUN_STARTED` duplication in the adapters.
- **Falsifiable test.** One normalized event map; no duplicated bootstrap; no
  changed event ordering (do-not-misorder-history invariant).
- **Risk.** Low. No new transport behavior.
- **Effort.** Small-Medium.

### 7.3 Phase 4: Automatic checkpointing
- **Task.** Wire `maybe_checkpoint()` (currently orphaned in `checkpoint/policy.py`)
  into the universal layer at meaningful boundaries (tool completion, important
  state transition, before/after risky external action, context compaction,
  interruption). Not every token.
- **Falsifiable test.** Checkpoint fires at meaningful boundaries; storage/latency
  bounded.
- **Risk.** Low.
- **Effort.** Small-Medium.

### 7.4 Phase 5: Ledger + reconciliation hardening
- **Task.** Persist `idempotency_key` on `Action`; add `RECONCILING` status;
  optional capability classes; compose the lease coordinator into the resume
  boundary.
- **Falsifiable test.** Uncertain action reaches `RECONCILING` then
  confirmed/needs-human, never blind replay; concurrent recovery yields at most
  one valid continuation authority.
- **Risk.** Medium (lease composition could introduce double-resume friction if
  TTLs mis-tuned; start with observability, not enforcement).
- **Effort.** Medium.

### 7.5 Phase 6: Recovery-correctness benchmark (time-sensitive)
- **Task.** Build 8-12 real scenarios (clean crash, crash mid-tool with
  repository change, ambiguous side effect, dependency/credential expired, no
  relevant change [must cleanly RESUME, not over-escalate], model/harness
  change, argument drift, checkpoint tampering, concurrent recovery, partial
  completion, unrelated environment change). Compute unsafe-resume-rate,
  recovery-decision-accuracy, unnecessary-human-escalation-rate,
  dependency-repair precision against baselines (naive resume, bare LangGraph
  checkpointer, unguarded Temporal/Diagrid-style resume).
- **Falsifiable test.** Measured numbers, reproducible, no invented values.
- **Risk.** Low-Medium (benchmark design can reward caution; include the "no
  change -> RESUME" case).
- **Effort.** Medium (and urgent given the closing unbenchmarked gap).

### 7.6 Small open issues (contributor work, low risk)
- #29, #30, #33, #34, #36, #42, #43, #45, #49 from section 5.1. Each is
  isolated and independently fixable; good first issues.

### 7.7 Open questions to resolve (cheap, high information)
- File the `checkpoint_version: 0` on resume observation as an issue; decide
  whether the resume contract should reflect checkpoint versions.
- Continue the autonomous-usage investigation (does an LLM agent call
  `checkpoint`/`resume` on its own initiative?).

---

## 8. Guardrails (do not violate)

- Do not compete with durable-execution runtimes; be complementary.
- Do not lead marketing with hash chains / Ed25519 attestation (Diagrid already
  ships this at scale).
- Do not keep investing in unused plugin seams (`StateExtractor` /
  `ActionReconciler` / `ValidationRule`) until they have a real second consumer;
  wire them into Phase 2 or delete them.
- Do not drop `FORK` in without a concrete scenario.
- Do not invent evidence, benchmark numbers, or novelty claims.
- Preserve backward compatibility and the sealed one-next-action contract.
- After each phase: STOP and report. Do not auto-continue into the next phase
  unless explicitly instructed.

---

## 9. Stop conditions (per phase)

After each phase, report: what changed, why, existing code reused, new files,
modified files, tests added, existing tests before/after, failures/errors,
performance impact if measurable, security impact, backward compatibility,
known limitations, remaining research uncertainty. Then stop until the next
phase is explicitly requested.

**Current position:** Phase 1 is complete and reported. Phase 2 has NOT been
started. Awaiting explicit instruction to begin Phase 2.
