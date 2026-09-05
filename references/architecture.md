## Architecture

A complete system diagram, a sequence diagram of the recovery data flow, and an
enumerated reference (tools, recovery modes, checkpoint policies, reconcilers,
state fields, event types) are in
[architecture-diagram.md](architecture-diagram.md).

### Data Model (Phase 1 - Complete)


Built on immutable, frozen Pydantic v2 models with cryptographic hash chains:

```
SemanticState
+-- Goal                    What the agent is trying to do
+-- Progress                Completed / pending / failed counts
+-- PlanStep[]              Structured execution plan
+-- Decision[]              Durable decisions with evidence trails
+-- Finding[]               Claims with evidence and confidence scores
+-- Evidence[]              Referenced evidence with checksums
+-- PendingWork[]           Remaining tasks
+-- Approval[]              Human approvals with expiration
+-- ExternalDependency[]    Versioned external resources
+-- ModelState              Model-specific assumptions (revalidated on switch)
```

### Event Log

Append-only, hash-chained event stream. Source of truth for every run:

```
e1.prev_hash = None          e1.hash = H(content(e1))
e2.prev_hash = e1.hash       e2.hash = H(content(e2))
e3.prev_hash = e2.hash       e3.hash = H(content(e3))
```

Tamper detection built in. `EventLog.verify()` recomputes every digest and re-walks the chain, localizing damage.

29 event types covering the full lifecycle: `RUN_STARTED`, `TOOL_CALLED`, `DECISION_CREATED`, `STATE_CHECKPOINTED`, `ENVIRONMENT_CHANGED`, `RECOVERY_STARTED`, `ACTION_RECONCILED`, and more.

### Semantic State Projection (Phase 2 - Complete)

State is not stored and mutated. It is *projected* from the event log by a pure fold:

```
state = reduce(apply, events, empty_state)
```

Two properties make this safe to recover from, and both are tested:

- **Reproducibility** - folding the same prefix twice yields an equal state. Timestamps come from the events, never from `now()`.
- **Prefix-closure** - `project(events, upto=n)` equals the state that existed after event `n`.

Together with the log's `trusted_through`, a run whose tail was tampered with can still be recovered up to its last verified event:

```python
report = log.verify("run_4821")
trusted = report.trusted_through["run_4821"]
state = project("run_4821", log.events("run_4821"), upto=trusted)
```

Unknown event types are counted, not fatal - a newer writer's vocabulary must never render a run unrecoverable.

### State Extraction

Extraction is pluggable through a single protocol:

```python
class StateExtractor(Protocol):
    name: str

    def extract(self, context: ExtractionContext) -> SemanticState: ...
```

`DeterministicExtractor` is the default and the only one required. It folds the event log - no model, no network, no clock.

`LLMExtractor` is optional and deliberately constrained. The caller supplies the callable; CONTINUUM has no provider dependency, no API key handling, no network default. The model may only **add** components, never modify or delete recorded facts. Everything it produces is tagged `Origin.LLM` and forced to `REQUIRES_REVIEW`. If it raises, extraction degrades to the deterministic result - losing an optional enrichment must never cost a recovery.

The deterministic layer is authoritative. The model is an advisor.

### State Versioning

Every accepted mutation appends a version. Versions are content-addressed and linked, so history can be audited exactly like the event log:

```python
chain = VersionChain("run_4821")
chain.commit(state, reason="milestone")  # -> VersionEntry(version=0)
chain.commit(state, reason="timer")  # -> None: semantically unchanged
chain.verify()  # recompute fingerprints, re-walk links
```

`commit` returns `None` when nothing meaningful changed, so timer-driven checkpoint policies cannot inflate history with noise. Fingerprints ignore bookkeeping fields - a state means the same thing regardless of when it was projected.

### Durable Storage (Phase 3 - Complete)

SQLite by default. No server, no cloud account, no daemon:

```python
from continuum import SQLiteStorage, Run, EventType, project

with SQLiteStorage("agent.db") as store:
    store.create_run(Run(run_id="run_4821", goal="Analyze 10,000 documents"))
    store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "...", "total": 10000})
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"count": 3421})

    state = project("run_4821", store.read_events("run_4821"))
    store.verify_events("run_4821").ok
```

**What the engine guarantees:** append-only events, atomic sequence allocation, and durability once `append_event` returns. WAL journaling keeps readers unblocked while the agent writes. `synchronous=FULL` costs an fsync per append - the alternative can lose the last commits on a crash, silently reintroducing exactly the duplicate-work problem CONTINUUM exists to prevent.

**What it does not guarantee:** exactly-once semantics. A crash between an external side effect and its ledger write leaves the ledger behind reality. Storage cannot close that gap alone; the action ledger reconciles it in Phase 6. The engine is also single-host and not encrypted at rest.

**Write races fail loudly.** Two writers appending to one run take an `IMMEDIATE` lock, with `UNIQUE(run_id, sequence)` as a backstop. One commits, the other gets a `ConcurrentWriteError`. A silently forked chain that still verifies clean would be the worst possible failure, because it looks correct.

**Corruption is refused, never returned.** Runs, versions and checkpoints are validated and hash-checked on read; a mismatch raises `CorruptedRecord` rather than handing back state an agent might act on.

Verified end to end - a worker killed with `os._exit(9)` mid-run, then restarted against the same file:

```text
[pid 58807] started at 0 completed
[pid 58807] *** CRASH at doc 39 ***

[pid 58808] resumed at 40 completed
[pid 58808] finished at 100 completed

events        102, integrity ok=True, trusted_through=102
docs written  100 events, 100 unique -> duplicates=0
```

### Checkpointing (Phase 4 - Complete)

Checkpointing every turn is the obvious design and the wrong one: it costs an fsync per step and fills history with versions that mean nothing. A policy decides instead.

```python
from continuum import CheckpointManager, SemanticPolicy, SQLiteStorage

manager = CheckpointManager(store, policy=SemanticPolicy(progress_stride=25))

for doc in documents:
    process(doc)
    manager.maybe_checkpoint("run_4821")  # writes only when it matters
```

| Policy | Fires when |
|:--|:--|
| `ManualPolicy` | asked explicitly |
| `IntervalPolicy` | N seconds since the last checkpoint |
| `EventPolicy` | a side effect or milestone event occurs |
| `SemanticPolicy` | the *meaning* of the state changed |
| `ContextPressurePolicy` | the context window is filling up |
| `HybridPolicy` | any of the above (the default) |

`SemanticPolicy` is the interesting one. Grinding from document 3,400 to 3,401 changes progress but nothing structural. Invalidating a single decision changes what the agent may safely do next. The first is ignored; the second always checkpoints.

Policies are pure functions of an explicit context - including the clock - so checkpoint timing is testable rather than a source of flaky tests.

**Restore replays the gap.** A checkpoint plus the events recorded after it, so a crash *between* checkpoints does not discard the work in between:

```python
restored = manager.restore("run_4821")
restored.state.progress.completed  # caught up to the log
restored.pending_events  # how much was replayed
```

Measured on a 200-document run killed at document 117:

```text
*** CRASH at doc 117 ***

restored from checkpoint v8 | replayed 17 events (not 135)
finished at 200
```

### Dual-State Rewind (Issue #292)

A checkpoint restores the projected state, but the workspace may have moved
on. Rewind restores both atomically: the event log is projected to the
target checkpoint's ``source_sequence``, and every hook-captured file write
newer than that checkpoint is inverted from the hash-chained evidence log.

```bash
continuum rewind <run_id> --to <checkpoint> [--force] [--dry-run]
```

* **Deterministic revert set** — from ``TOOL_COMPLETED`` events, not model judgment.
* **Digest-verified** — current file digest must match the last observed digest after the checkpoint; on mismatch the file is reported as a conflict and left untouched.
* **Fail-closed** — external edits since the checkpoint surface as conflicts; nothing is clobbered silently.
* **Snapshot-backed** — file content for each observed digest is stored under ``.continuum/file-snapshots/<sha256>`` at observation time, so the bytes for any past digest are recoverable. Large or unreadable files (>10 MiB) are reported as unrecoverable.
* **Validated** — after reverting files, revalidation is run against the rewound world before issuing a resume verdict.

Untracked-file writes (created after checkpoint) are deleted; tracked-file writes are restored from the snapshot for the digest at checkpoint, or listed as unrecoverable when no snapshot exists.

### Recovery Context

On resume the agent is handed the minimum sufficient briefing, not the transcript:

```text
CURRENT GOAL
  Analyze 200 documents  (goal v1)

NEXT SAFE ACTION
  continue_analysis

VERIFIED PROGRESS
  200 completed, 0 pending, 0 failed (of 200)
  derived from events 1..227

VALID DECISIONS
  d_12: Only peer-reviewed studies

RELEVANT FINDINGS
  f_0 (0.90): pattern at 0
  ... and 5 more findings

EXTERNAL DEPENDENCIES
  dataset: v3 [valid]
```

That is a 228-event run rendered in 410 characters. **Stale state is shown, never hidden** - an agent that is not told its dataset changed will confidently continue on invalid assumptions. Under a token budget, sections drop from the least important end, but goal, verified progress and stale state are never sacrificed.

Token figures reported by `estimate_tokens` are a **character-based heuristic, not a tokenizer**. CONTINUUM takes no model-provider dependency for a size hint. No compression ratio is claimed until the benchmark measures real tokens.

### State Validation (Phase 5 - Complete)

**A persisted checkpoint is not trustworthy merely because it was persisted.** Before an agent resumes, every component is checked against the environment as it is now.

```python
from continuum import StaticProvider, capture_environment, validate_state

now = capture_environment("run_4821", StaticProvider(dataset="v4"))
outcome = validate_state(
    restored.state,
    checkpoint_environment=restored.checkpoint.environment,
    current_environment=now,
)

outcome.safe  # False
outcome.state  # same state, with statuses already revised
```

**Staleness propagates.** A dataset moving v3 to v4 does not only invalidate the dependency - it invalidates the reasoning built on it. The validator walks `dependency -> evidence -> finding -> decision`:

```text
[!!] external dependency dataset: conflicted - v3 -> v4
[!!] evidence paper_128: stale - source 'dataset' changed
[!!] finding finding_17: stale - rests on changed evidence: paper_128
[!!] decision d_12: stale - rests on changed support: finding_17
[ok] goal: valid - v1
[ok] progress: valid - 60 completed

Safe to resume: no
```

Marking only the dependency would leave the agent reasoning from conclusions it can no longer justify. State that did not depend on the change is left untouched, so the report stays worth reading.

With the same dataset still in place, the identical run resumes cleanly:

```text
[ok] external dependency dataset: valid - verified unchanged
Safe to resume: yes
```

**Uncertainty degrades, it does not resolve.** A resource that could not be inspected - an API that timed out, a file now unreadable - becomes `UNKNOWN`, never `VALID`. `UNKNOWN` is enough to withhold a clean resume. The system may say "I cannot tell"; it may not guess in its own favour. Callers who genuinely tolerate uncertainty opt in with `strict_unknown=False`, and it stays visible in the report.

**Model switches are never assumed safe.** State produced under one model that carries model-specific assumptions is marked `STALE` when another model takes over, and requires revalidation.

### Action Ledger (Phase 6 - Complete)

Storage gives durability for *state*. It cannot give exactly-once semantics for effects on other systems, because the effect and the record of it are two separate writes with a gap between them. The ledger makes that gap observable instead of invisible.

```python
from continuum import ActionLedger, UnknownSideEffect

ledger = ActionLedger(store, "run_4821")

outcome = ledger.claim("github.create_issue", {"title": "Bug report"})
if outcome.fresh:
    issue = github.create_issue(...)
    ledger.complete(outcome.key, external_id=issue.id, result={"url": issue.url})
else:
    issue_id = outcome.external_id  # already done - previous result returned
```

**Every crash interleaving is accounted for:**

| Crash lands | Ledger state on recovery | Behaviour |
|:--|:--|:--|
| before the claim | nothing recorded | retry is safe |
| between claim and effect | `STARTED`, no result | **outcome unknown** |
| between effect and record | `STARTED`, no result | **outcome unknown** |
| after recording | `COMPLETED` | repeat returns stored result |

The middle two are indistinguishable from the ledger alone - which is exactly why they must not be resolved by assumption. `claim` raises `UnknownSideEffect` and requires a reconciler:

```python
from continuum import ProbeReconciler, Resolution, reconcile_pending

reconcile_pending(
    ledger,
    ProbeReconciler(
        lambda action: Resolution(occurred=True, external_id=find_issue(action)),
    ),
)
```

`ProbeReconciler` asks the external system and is the only strategy that produces evidence. `AssumeNotOccurredReconciler` retries, and requires you to assert `idempotent=True` explicitly so nobody reaches for it by reflex. `ManualReconciler` escalates.

There is deliberately **no `AssumeOccurred` strategy**. Assuming success without evidence silently drops work, and a dropped side effect is invisible - nothing in the system will ever contradict it. A probe that raises is treated as "could not determine", never as evidence of absence: an unreachable API tells you nothing about whether your earlier request landed.

This is honest **at-least-once with mandatory reconciliation**, not exactly-once. The gap is documented rather than marketed away.

Verified with real subprocesses - a worker that creates a GitHub issue then dies with `os._exit(9)` before recording it:

```text
=== RECOVERY ===
checkpoint v2: 60/100 docs, replayed 11 events
uncertain side effects: 1 -> ['github.create_issue']
refused blind retry (UNKNOWN_SIDE_EFFECT)
reconciled: confirmed as performed: github.create_issue

[!!] external dependency dataset: conflicted - v3 -> v4
[!!] evidence paper_128: stale - source 'dataset' changed
[!!] finding finding_17: stale - rests on changed evidence: paper_128
[ok] progress: valid - 60 completed

repeat claim -> fresh=False, external_id=481
completed 100/100 | events verified: True

=== external system ===
issue count: 1
```

Sixty documents not reprocessed, one dataset change detected and propagated, and **exactly one issue created** despite the crash.

### Restore-Point Admissibility (Issue #295)

A checkpoint that is merely persisted is not yet admissible. Every completed
action records its consumed inputs (checkpoint_seq, event_positions,
component_ids, action_ids). On assess, the validator checks whether any
completed downstream action consumed state after the checkpoint. If so, the
checkpoint is inadmissible for plain RESUME. The engine maps this to
REPAIR_AND_RESUME for re-derivable commitments and to REQUEST_HUMAN for
action-graph commitments. The contract names each blocking commitment with
its chain position, so an operator or agent can see exactly which downstream
work would be lost by rewinding. The check is deterministic, hash-chained
and survives old rows (empty consumed_inputs is admissible).

### Recovery Engine (Phase 7 - Complete)

Validation says what is wrong. The ledger says what may have happened. The engine turns both into one decision.

```python
from continuum.recovery import RecoveryEngine

decision = RecoveryEngine(store).assess("run_4821", current_environment=now)

decision.mode  # RecoveryMode.REQUEST_HUMAN
decision.next_allowed_action  # "reconcile_action:action_011f511d"
decision.permits("rederive_finding:finding_17")  # False
```

**The most cautious applicable signal wins.** Each signal proposes a mode; the engine takes the maximum on an explicit ordering:

```text
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

This matters because the signals co-occur. A run can have a stale dataset *and* an uncertain side effect at the same time. Returning whichever was noticed first would make recovery depend on iteration order - and the unsafe answer would win about half the time.

**Repairs are ordered by dependency, not discovery.** Reconciling an uncertain side effect always comes first: nothing else is safe while the world may or may not have been modified. A dependency is re-pinned before the evidence and findings derived from it, since repairing in the wrong order produces work that is stale the moment it finishes.

**The contract names exactly one next action.** Listing everything currently allowed would let an agent pick the convenient step and skip the reconciliation it was supposed to do first:

```text
run_id:            run_1
checkpoint:        v2
recovery_status:   requires_human
verified:          goal, progress
invalidated:       evidence:paper_128 (stale), external_dependency:dataset (conflicted),
                   finding:finding_17 (stale)
required_actions:
  - reconcile_action:action_011f511df03cf454
  - revalidate_dependency:dataset
  - rederive_evidence:paper_128
  - rederive_finding:finding_17
next_allowed:      reconcile_action:action_011f511df03cf454
```

Contracts are deterministic and sealed with an integrity hash - one that could be edited between issue and enforcement would gate nothing.

The engine is **read-only**. It computes and explains a decision without mutating the run, which is what makes assessment safe to perform against a live database.

Full run - crash, dataset change, and an interrupted side effect together:

```text
Recovery decision: REQUEST_HUMAN
  because 1 external side effect(s) have unknown outcomes

agent tries to skip ahead -> permitted? False
after reconciling         -> REPAIR_AND_RESUME, next: revalidate_dependency:dataset

=== external system === issues created: 1
```

### Security

- **Deterministic canonical hashing** - sorted keys, UTC-normalized timestamps, enum-by-value serialization, rejection of non-finite floats
- **Hash-chained events** - tamper-evident audit trail
- **Credentials never serialized** into state - referenced only, never stored
- **Provenance tracking** - every state component traces back to its origin event
- **Provenance that survives compaction** - every checkpoint component carries its `Origin` tag; compaction output preserves per-fact origin rather than a single summary trust level. Summaries are stamped `derived_origin = min(sources)` over the full history (archived + live) and the projector clamps any claimed `derived_origin` to `min(claimed, writer source, weakest seen)`, so an archived `EXTERNAL_AGENT` fact cannot be laundered into `DETERMINISTIC` via `build_informed_retry`, `REASONING_SUMMARY`, or briefing `PROVENANCE MAP` (hash-chained `source_sequence`/`source_event_id` stay resolvable to archived rows, verified by `verify_events` across the anchor boundary). See `docs/audit-provenance-compaction-294.md`.

---


## Project Structure

```
continuum/
+-- README.md
+-- LICENSE                          Apache 2.0
+-- CHANGELOG.md
+-- pyproject.toml
|
+-- src/
|   +-- continuum/
|       +-- __init__.py              Public API surface
|       +-- models.py                Immutable data models
|       +-- events.py                Hash-chained event log
|       +-- state/
|       |   +-- __init__.py
|       |   +-- semantic.py          Deterministic event -> state projection
|       |   +-- extractor.py         Pluggable extraction (LLM optional)
|       |   +-- versioning.py        Content-addressed version chain
|       |   +-- diff.py              Semantic diff and renderer
|       |   +-- validator.py         Validation and staleness propagation
|       +-- storage/
|       |   +-- __init__.py          open_storage() URL dispatch
|       |   +-- base.py              Storage interface and stated guarantees
|       |   +-- sqlite.py            WAL, transactions, integrity on read
|       +-- checkpoint/
|       |   +-- __init__.py
|       |   +-- policy.py            When to checkpoint
|       |   +-- manager.py           Create, seal, persist, restore
|       |   +-- context.py           Bounded recovery context
|       +-- environment/
|       |   +-- __init__.py
|       |   +-- snapshot.py          Pluggable environment capture
|       |   +-- diff.py              Conservative snapshot comparison
|       +-- actions/
|       |   +-- __init__.py
|       |   +-- idempotency.py       Content-derived action identity
|       |   +-- ledger.py            Durable record of side effects
|       |   +-- reconciliation.py    Resolving uncertain outcomes
|       +-- recovery/
|       |   +-- __init__.py
|       |   +-- engine.py            Decide how a run may resume
|       |   +-- planner.py           Ordered repair steps
|       |   +-- contract.py          Sealed, gated next action
|       +-- mcp/
|       |   +-- __init__.py
|       |   +-- server.py            9 stdio tools, read-only/mutating split, auth gate
|       +-- adapters/
|       |   +-- __init__.py          AgentAdapter contract
|       |   +-- base.py              Shared adapter plumbing
|       |   +-- generic.py           In-process Python facade
|       |   +-- langgraph.py         LangGraph integration (optional)
|       |   +-- openai.py            OpenAI Agents SDK integration (optional)
|       +-- cli/
|       |   +-- __init__.py
|       |   +-- main.py              argparse CLI, read-only by default
|       |   +-- exitcodes.py         Exit codes as a safety contract
|       +-- security/
|           +-- __init__.py
|           +-- hashing.py           Deterministic canonical hashing
|
+-- tests/
    +-- test_models.py               Model invariants and serialization
    +-- test_events.py               Chain integrity and tamper detection
    +-- test_hashing.py              Canonical hashing properties
    +-- test_projection.py           Fold correctness, reproducibility, prefix-closure
    +-- test_projection_edges.py     Malformed logs and partial payloads
    +-- test_extractor.py            Extractor protocol and LLM containment
    +-- test_versioning.py           Version chain integrity
    +-- test_diff.py                 Semantic diff behaviour
    +-- test_storage.py              Persistence, durability, corruption refusal
    +-- test_storage_concurrency.py  Thread and multi-process write races
    +-- test_storage_edges.py        Payload validation and URL handling
    +-- test_checkpoint_policy.py    Policy decisions and triggers
    +-- test_checkpoint_manager.py   Creation, restore, crash interleavings
    +-- test_recovery_context.py     Bounded context and truncation safety
    +-- test_environment.py          Capture, diffing, unverifiable resources
    +-- test_validator.py            Validation and staleness propagation
    +-- test_mcp_server.py           MCP tools, auth gate, read-only guarantee
    +-- test_action_ledger.py        Idempotency and the crash gap
    +-- test_reconciliation.py       Strategies + real-subprocess crash tests
    +-- test_recovery_engine.py      Decision precedence and contract gating
    +-- test_recovery_planner.py     Repair ordering and determinism
    +-- test_cli.py                  Exit-code contract and read-only guarantees
```

---

