# CONTINUUM: complete architecture specification

This file is the authoritative, text-only description of the CONTINUUM
architecture. It is written so it can be handed to an image-generation AI to
produce a professional architecture diagram (PNG). Every name, count, and flow
below reflects the actual implementation, not an idealization.

CONTINUUM is a framework-agnostic, durable recovery layer for long-running AI
agents. It separates volatile LLM context from permanent task state by
constructing semantic checkpoints: the minimum verified information an agent
needs to continue after a crash, a model switch, or a changed environment.

---

## 1. Design principles (the honest guarantees)

- State is rebuilt from an append-only event log, never from a mutable
  snapshot alone. The log is the source of truth.
- Exactly-once side effects are impossible across system boundaries, so the
  system is explicitly at-least-once with mandatory reconciliation.
- Stale state is shown, never hidden. A changed dependency invalidates the
  reasoning built on it, transitively.
- The most cautious applicable recovery signal wins. Safety beats convenience.
- The core library and CLI use only the Python standard library. No
  model-provider dependency is taken for a size hint.

---

## 2. System tiers (diagram layers, top to bottom)

Layer 1 - Agents and clients
  - Claude Code
  - LangGraph agent
  - OpenAI agent
  - Generic Python agent (in-process facade)

Layer 2 - Framework adapters (optional installs)
  - LangGraphAgentAdapter
  - OpenAIAgentAdapter
  - GenericAgentAdapter

Layer 3 - CONTINUUM SDK (the core; only the standard library)
  - Extractors
  - Event Log
  - State Engine (semantic projection)
  - Action Ledger
  - Checkpoint Manager
  - Environment
  - Validator
  - Recovery Engine
  - Security
  - (MCP server: a stdio surface on top of the SDK, deny by default)

Layer 4 - Durable storage
  - SQLiteStorage (WAL, synchronous FULL)

Layer 5 - External systems
  - GitHub, email, APIs (anything an agent side-effects against)

Layer 6 - Output
  - Resume (bounded recovery context handed back to the agent)

---

## 3. Component catalog

### 3.1 Agents and clients
Any system that drives a long-running task. CONTINUUM does not care which
agent framework is used; it observes and records through adapters and the MCP
server.

### 3.2 Framework adapters
Thin wrappers that translate a framework's native loop into CONTINUUM calls.
- GenericAgentAdapter: in-process Python facade, trusted as Origin.DETERMINISTIC.
- OpenAIAgentAdapter: wraps the OpenAI Agents SDK.
- LangGraphAgentAdapter: subclasses GenericAgentAdapter; wraps a LangGraph StateGraph.
These are optional. An agent can also call the SDK or MCP server directly.

### 3.3 MCP server (stdio, deny by default)
A Model Context Protocol server exposing 12 tools. It is read-only by default.
- Read-only tools (3): continuum_validate, continuum_resume,
  continuum_list_actions.
- Mutating tools (9): continuum_record_progress, continuum_checkpoint,
  continuum_record_summary, continuum_record_plan, continuum_confirm,
  continuum_intercept_action, continuum_complete_action, continuum_fail_action,
  continuum_reconcile_action.
(Every MCP tool name is prefixed with "continuum_" so it never collides
with a host tool's own tool names.)
An auth gate restricts mutating tools to an allowlist. The primary
environment variable is CONTINUUM_MCP_ALLOW; CONTINUUM_MCP_MUTATING_CLIENTS
is kept only as a backward-compatible alias. A per-project file
.continuum/mcp-policy.json is also honored. Without explicit permission,
no state-changing call succeeds. This is the
"DENY BY DEFAULT. WORKS WITH CLAUDE CODE." boundary.

### 3.4 Extractors
Turn agent output into structured state deltas.
- DeterministicExtractor: the default; folds the event log without any model.
- LLMExtractor: optional; may add information but is tagged Origin.LLM and
  flagged REQUIRES_REVIEW. The model is never assumed authoritative.

### 3.5 Event Log (the source of truth)
- Append-only, hash-chained: each event stores the digest of the prior event,
  so tampering is detectable.
- verify() re-walks the chain and localizes the first corrupted event.
- 29 event types span the full lifecycle. The complete set:
  RUN_STARTED, RUN_COMPLETED, RUN_ABORTED, TASK_UPDATED, TOOL_CALLED,
  TOOL_COMPLETED, TOOL_FAILED, DECISION_CREATED, DECISION_INVALIDATED,
  EVIDENCE_ADDED, FINDING_ADDED, FINDING_INVALIDATED, WORK_ADDED,
  WORK_COMPLETED, DEPENDENCY_DECLARED, APPROVAL_REQUESTED, APPROVAL_GRANTED,
  APPROVAL_REVOKED, MODEL_CHANGED, MODEL_ASSUMPTION_RECORDED,
  STATE_CHECKPOINTED, STATE_VALIDATED, ENVIRONMENT_CHANGED, RECOVERY_STARTED,
  RECOVERY_COMPLETED, RECOVERY_BLOCKED, ACTION_RECORDED, ACTION_RECONCILED,
  ACTION_COMPENSATED.
- Every state component traces to its origin event (provenance).

### 3.6 State Engine (semantic projection)
- Pure function: SemanticState = reduce(apply, events, empty_state).
- Deterministic and reproducible: replaying the same events yields the same
  state. Prefix-closed: any prefix of the log yields a valid partial state.
- Versioning is content-addressed and linked; committing an unchanged state
  returns None rather than a duplicate version.
- A semantic diff renderer summarizes what changed between two states.

### 3.7 Action Ledger
Records external side effects so they are observable and reconcilable.
- Two-phase: claim(action, key) reserves the effect; the caller performs it;
  complete(key, external_id) records the outcome.
- Idempotent by a content-derived resource key, so a retry returns the stored
  result instead of repeating the effect.
- Crash gap: if the process dies between claim and complete, the outcome is
  UNKNOWN, never guessed.
- Reconcilers resolve uncertainty:
  - ProbeReconciler: asks the external system; the only strategy that produces
    evidence.
  - ManualReconciler: escalates to a human.
  - AssumeNotOccurredReconciler: retries, but only when the caller explicitly
    asserts idempotent=True.
  - Deliberately no AssumeOccurred: assuming success without evidence silently
    drops work.

### 3.8 Checkpoint Manager
Decides when to snapshot and how to restore.
- Six policies: ManualPolicy, IntervalPolicy, EventPolicy, SemanticPolicy,
  ContextPressurePolicy, HybridPolicy (the default; any of the above).
- SemanticPolicy is the interesting one: a meaningless progress increment is
  ignored, but an invalidated decision always checkpoints.
- Restore replays the gap: a checkpoint plus the events recorded after it, so a
  crash between checkpoints loses no work.
- Recovery context: the minimum sufficient briefing (goal, verified progress,
  valid decisions, relevant findings, external dependencies) rendered in a few
  hundred characters. Under a token budget, the least important sections drop,
  but goal, verified progress, and stale state are never sacrificed.

### 3.9 Environment
Captures a pluggable snapshot of the world the run depends on (datasets,
files, APIs) and computes a conservative diff against a prior snapshot. A
resource that cannot be inspected becomes UNKNOWN, never VALID.

### 3.10 Validator (staleness propagation)
Before resume, every component is checked against the environment as it is
now. Staleness propagates along the dependency chain:
  dependency -> evidence -> finding -> decision.
A dataset moving v3 to v4 invalidates the evidence built on it, the findings
resting on that evidence, and the decisions resting on those findings. State
that did not depend on the change is left untouched. Model switches are never
assumed safe: state carrying model-specific assumptions is marked STALE under
a different model.

### 3.11 Recovery Engine
Turns validation findings and ledger uncertainty into one decision.
- Read-only: it computes and explains a decision without mutating the run.
- Precedence (most cautious wins):
  RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT.
- Repairs are ordered by dependency, never discovery: reconcile an uncertain
  side effect first, then re-pin a dependency, then re-derive its evidence and
  findings.
- The contract names exactly one next allowed action and is sealed with an
  integrity hash, so it cannot be edited between issue and enforcement.

### 3.12 Security
- Deterministic canonical hashing: sorted keys, UTC-normalized timestamps,
  enum-by-value serialization, rejection of non-finite floats.
- Hash-chained events: tamper-evident audit trail.
- Credentials are referenced, never serialized into state.
- Provenance: every state component traces to its origin event.

### 3.13 Durable storage
SQLiteStorage:
- WAL journaling with synchronous=FULL for durability.
- Atomic sequence allocation for event IDs.
- Integrity verified on read; corruption is refused (CorruptedRecord).
- Concurrent writes fail loudly (ConcurrentWriteError) rather than silently
  losing or duplicating events.

### 3.14 External systems
GitHub, email, APIs: anything an agent performs a side effect against. CONTINUUM
never assumes the effect landed; the Action Ledger and reconcilers manage the
gap.

---

## 4. SemanticState data model (10 semantic fields)

The actual attribute names on the SemanticState model:

- goal: the run's objective (type Goal).
- progress: verified completion counts (type Progress).
- plan: the planned steps (type list[PlanStep]).
- decisions: conclusions the agent may safely act on (type list[Decision]).
- findings: derived observations (type list[Finding]).
- evidence: the sources those findings rest on (type list[Evidence]).
- pending_work: work not yet done (type list[PendingWork]).
- approvals: human approvals required or granted (type list[Approval]).
- external_dependencies: outside resources the run depends on
  (type list[ExternalDependency]).
- model: model identity and model-specific assumptions
  (type ModelState | None).

(Plus metadata fields: run_id, version, source_sequence, created_at,
updated_at.)

---

## 5. Data flows (for the diagram's arrows)

Flow A - Normal recording loop
  Agent -> (record_progress / checkpoint via MCP or SDK) -> Event Log
  Event Log -> (hash chain, append) -> Durable Storage
  Event Log -> State Engine (projection) -> SemanticState
  State Engine -> Versioning / Diff / Validator

Flow B - Action execution (two-phase, idempotent)
  Agent -> MCP intercept_action(key) -> Action Ledger claim
  Agent -> performs effect on External System
  Agent -> MCP complete_action(key, external_id) -> Action Ledger complete
  If crash between claim and complete -> outcome UNKNOWN -> reconciler probes
  External System

Flow C - Resume after crash or change
  Durable Storage -> State Engine (replay) -> SemanticState
  Environment snapshot -> Validator (diff vs checkpoint environment)
  Validator -> staleness report -> Recovery Engine
  Action Ledger uncertainty -> Recovery Engine
  Recovery Engine -> sealed contract (one next action) -> Resume context ->
  Agent

Flow D - Checkpoint
  State Engine -> Checkpoint Manager (policy decides) -> Durable Storage
  Restore -> replay gap -> caught-up SemanticState

---

## 6. Suggested diagram layout (for the PNG generator)

Use a vertical, left-to-right-then-down tiered layout. Boxes are grouped by
the tiers in section 2.

Top tier: four agent boxes in a row, labeled "Agents and clients".
Second tier: three adapter boxes in a row, labeled "Framework adapters
(optional)".
Third tier: one large container labeled "CONTINUUM SDK". Inside it, a 3x3 grid:
  Row 1: Extractors, Event Log (highlight this box, it is the source of truth),
         Action Ledger
  Row 2: State Engine, Checkpoint Manager, Environment
  Row 3: Validator, Recovery Engine, Security
  Place a pill at the top-right of the SDK container: "MCP server: deny by
  default, 12 tools (3 read-only, 9 mutating)".
Fourth tier: two boxes side by side: "Durable Storage (SQLite, WAL)" and
  "External Systems (GitHub, email, APIs)".
Bottom tier: one centered box "Resume (bounded recovery context)".

Arrows:
  Agents -> Adapters -> SDK (vertical spine).
  SDK -> Durable Storage (label "events + state").
  SDK -> External Systems (label "side effects").
  SDK -> Resume (label "resume / recovery"; draw this as the central orange
  arrow so it reads as the safety outcome).
  Inside SDK, emphasize Event Log as the hub: draw a subtle arrow from Event
  Log toward Durable Storage to show persistence.

Visual treatment:
  Brand colors: blue #3B82F6 / #62AFE0 for containers and clients, orange
  #FF5017 for accents, highlights, and the resume arrow, navy #071827 for text.
  White cards with light borders, soft shadows, rounded corners. Clean
  sans-serif typography (Inter or equivalent). Keep it spacious; let the
  diagram use the full width.
