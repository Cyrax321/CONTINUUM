# CONTINUUM architecture data (source-verified)

Every fact below was read directly from the code at the current `main` commit.
Where useful, the `file:line` citation is given so any claim can be re-checked.
Use this as the single source of truth when drawing the diagram. No value here
is inferred or idealized.

Convention: MCP tool names are prefixed with `continuum_`. CLI subcommand names
are not (the CLI binary itself is `continuum`, so its verbs are bare).

---

## 1. Tiers (top to bottom in the diagram)

1. Agents and clients
2. Framework adapters (optional)
3. CONTINUUM SDK (core; standard library only) + MCP server (stdio, deny by default)
4. Durable storage
5. External systems
6. Output: Resume (bounded recovery context)

---

## 2. Agents and clients (Layer 1)

- Claude Code
- LangGraph agent
- OpenAI agent
- Generic Python agent (in-process facade)

No code class needed; these are the callers.

---

## 3. Framework adapters (Layer 2) `src/continuum/adapters/`

| Class | File:line | Notes |
|--|--|--|
| `GenericAgentAdapter` | `generic.py:33` | In-process facade; trusted as `Origin.DETERMINISTIC` |
| `OpenAIAgentAdapter` | `openai.py:129` | Wraps the OpenAI Agents SDK |
| `LangGraphAgentAdapter` | `langgraph.py:105` | Subclasses `GenericAgentAdapter`; wraps a LangGraph `StateGraph` |

Optional installs. An agent may also call the SDK or MCP server directly.

---

## 4. MCP server (stdio, deny by default) `src/continuum/mcp/`

Server name: `continuum-mcp` (`server.py`). Twelve tools, all names prefixed,
recounted from the tool registrations on 2026-09-16.

| Tool (exact name) | Kind | Source |
|--|--|--|
| `continuum_validate` | read-only | `server.py:937`, annotation `read_only` at `:944` |
| `continuum_resume` | read-only | `server.py:997`, annotation `read_only` at `:1006` |
| `continuum_list_actions` | read-only | `server.py:1512`, annotation `read_only` at `:1518` |
| `continuum_record_progress` | mutating | `server.py:683` |
| `continuum_checkpoint` | mutating | `server.py:743` |
| `continuum_record_summary` | mutating | `server.py:790` |
| `continuum_record_plan` | mutating | `server.py:857` |
| `continuum_confirm` | mutating | `server.py:1133` |
| `continuum_intercept_action` | mutating | `server.py:1200` |
| `continuum_complete_action` | mutating | `server.py:1400` |
| `continuum_fail_action` | mutating | `server.py:1435` |
| `continuum_reconcile_action` | mutating | `server.py:1464` |

Read-only annotation is `ToolAnnotations(read_only_hint=True)`
(`server.py:599`); mutating is `read_only_hint=False` (`server.py:600`).
Read-only count = 3, mutating count = 9.

Auth gate (allowlist for mutating tools):
- Primary env var: `CONTINUUM_MCP_ALLOW` (`authz.py:72`, `POLICY_ENV_VAR`).
- Backward-compatible alias: `CONTINUUM_MCP_MUTATING_CLIENTS` (`authz.py:79`, `POLICY_ENV_VAR_ALIAS`).
- Per-project file: `.continuum/mcp-policy.json` (`authz.py:81`, `POLICY_FILENAME`).
- Without explicit permission, no mutating call succeeds. This is the
  "DENY BY DEFAULT. WORKS WITH CLAUDE CODE." boundary.

---

## 5. CONTINUUM SDK subsystems (Layer 3) `src/continuum/`

| Subsystem | Module | Responsibility |
|--|--|--|
| Extractors | `state/extractor.py` | Turn agent output into state deltas |
| Event Log | `events.py` | Append-only, hash-chained source of truth |
| State Engine (projection) | `state/semantic.py` | `state = reduce(apply, events)`; reproducible, prefix-closed |
| Versioning | `state/versioning.py` | Content-addressed, linked; unchanged state returns `None` |
| Semantic diff | `state/diff.py` | Renders what changed between two states |
| Action Ledger | `actions/ledger.py` | Records external side effects; idempotent; reconcilable |
| Reconcilers | `actions/reconciliation.py` | Resolve uncertain side-effect outcomes |
| Checkpoint Manager | `checkpoint/manager.py` + `policy.py` | Decides when to snapshot; restores by replaying the gap |
| Environment | `environment/snapshot.py` + `diff.py` | Pluggable snapshot; conservative diff |
| Validator | `state/validator.py` | Staleness propagation through dependency chain |
| Recovery Engine | `recovery/engine.py` + `planner.py` + `contract.py` | Computes one recovery decision; read-only; sealed contract |
| Security | `security/hashing.py` | Deterministic canonical hashing; provenance |

Inside the SDK diagram, draw the **Event Log** as the highlighted hub: it is the
source of truth, and events persist to storage.

---

## 6. Extractors `src/continuum/state/extractor.py`

| Class | File:line | Notes |
|--|--|--|
| `DeterministicExtractor` | `extractor.py:81` | Default; folds the event log, no model |
| `LLMExtractor` | `extractor.py:119` | Optional; adds info tagged `Origin.LLM`, flagged `REQUIRES_REVIEW` |

---

## 7. Event Log `src/continuum/events.py`

- Append-only, hash-chained: each event stores the digest of the prior event.
- `EventLog.verify()` re-walks the chain and localizes the first corrupted
  event (`events.py:401`).
- 51 event types (`EventType` StrEnum, `events.py:47`). Complete list:

```text
RUN_STARTED, RUN_COMPLETED, RUN_ABORTED, TASK_UPDATED, RUN_FORKED,
RUN_RESTORED, RUN_MERGED, TOOL_CALLED, TOOL_COMPLETED, TOOL_FAILED,
DECISION_CREATED, DECISION_INVALIDATED, EVIDENCE_ADDED, FINDING_ADDED,
FINDING_INVALIDATED, WORK_ADDED, WORK_COMPLETED, DEPENDENCY_DECLARED,
CONSTRAINT_PINNED, CONSTRAINT_RETRACTED, APPROVAL_REQUESTED,
APPROVAL_GRANTED, APPROVAL_REVOKED, MODEL_CHANGED,
MODEL_ASSUMPTION_RECORDED, STATE_CHECKPOINTED, STATE_VALIDATED,
ENVIRONMENT_CHANGED, RECOVERY_STARTED, RECOVERY_COMPLETED,
RECOVERY_BLOCKED, REVIEW_CONFIRMED, REASONING_SUMMARY, EVENT_LOG_ANCHORED,
PERCEPTION_OBSERVED, BRANCH_RESOLVED, ACTION_RECORDED, ACTION_RECONCILED,
ACTION_COMPENSATED, GRANT_DENIED, LIVENESS_SILENCE_DETECTED,
LIVENESS_RECOVERED, RISK_OBSERVED, AUTHORITY_CONSUMED,
AUTHORITY_RECONCILED, ATTEMPT_LESSON, TRAJECTORY_REPORT, PLAN_UPSERT,
MEMORY_TOMBSTONED, NOTIFICATION_SENT, NOTIFICATION_FAILED
```

---

## 8. Action Ledger `src/continuum/actions/`

- Two-phase: `claim(action, key)` reserves the effect; caller performs it;
  `complete(key, external_id)` records the outcome.
- Idempotent by a content-derived resource key: a retry returns the stored
  result instead of repeating the effect.
- Crash between claim and complete leaves the outcome `UNKNOWN`, never guessed.
- Reconcilers (`reconciliation.py`):

| Reconciler | File:line | Behavior |
|--|--|--|
| `ProbeReconciler` | `:73` | Asks the external system; only strategy that produces evidence |
| `ManualReconciler` | `:131` | Escalates to a human |
| `AssumeNotOccurredReconciler` | `:102` | Retries; only when caller explicitly asserts `idempotent=True` |

There is deliberately **no** `AssumeOccurred` strategy (assuming success without
evidence silently drops work).

Action states (`ActionStatus`, `models.py:109`): `PLANNED`, `STARTED`,
`COMPLETED`, `FAILED`, `UNKNOWN`, `COMPENSATED`, `REQUIRES_REVIEW`, `EXPIRED`
(8 values).

---

## 9. Checkpoint Manager `src/continuum/checkpoint/`

Six policies (`policy.py`):

| Policy | File:line |
|--|--|
| `ManualPolicy` | `:135` |
| `IntervalPolicy` | `:147` |
| `EventPolicy` | `:172` |
| `SemanticPolicy` | `:208` |
| `ContextPressurePolicy` | `:312` |
| `HybridPolicy` | `:289` |

Default policy is `HybridPolicy` (with `max_interval_seconds=300`), returned by
`default_policy()` (`policy.py:343`) and used when none is supplied
(`manager.py:92`).

Restore replays events recorded after the checkpoint, so a crash between
checkpoints loses no work. Recovery context renders the minimum sufficient
briefing (goal, verified progress, valid decisions, relevant findings, external
dependencies); under a token budget the least important sections drop, but
goal, verified progress, and stale state are never sacrificed.

---

## 10. Validator (staleness propagation) `src/continuum/state/validator.py`

Before resume, every component is checked against the environment as it is now.
Staleness propagates along:
  `dependency -> evidence -> finding -> decision`.
A dataset moving v3 to v4 invalidates the evidence on it, the findings on that
evidence, and the decisions on those findings. State not depending on the change
is left untouched. Uninspectable resources become `UNKNOWN`, never `VALID`.
Model switches are never assumed safe: state under a different model is marked
`STALE` and requires revalidation.

---

## 11. Recovery Engine `src/continuum/recovery/engine.py`

Seven modes. Most cautious wins (highest rank). Ranks from `SEVERITY`
(`engine.py:69`), which orders `RecoveryMode` (`models.py:122`):

| Mode | Rank | Safety class (`_SAFETY_FOR_MODE`, `engine.py:79`) |
|--|--|--|
| `RESUME` | 0 | `SAFE_TO_RESUME` |
| `REPAIR_AND_RESUME` | 1 | `REQUIRES_REPAIR` |
| `REPLAN` | 2 | `REQUIRES_REVALIDATION` |
| `WAIT` | 3 | `REQUIRES_REVALIDATION` |
| `REQUEST_HUMAN` | 4 | `REQUIRES_HUMAN` |
| `ROLLBACK` | 5 | `BLOCKED` |
| `ABORT` | 6 | `UNSAFE` |

Precedence string (most cautious wins):
`RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT`.

Repairs are ordered by dependency, never discovery: reconcile an uncertain side
effect first, then re-pin a dependency, then re-derive its evidence and findings.
The contract names exactly one next allowed action and is sealed with an
integrity hash. The engine is read-only: it computes a decision without mutating
the run.

---

## 12. SemanticState data model `src/continuum/models.py:740`

Ten semantic fields (exact attribute names):

| Attribute | Type |
|--|--|
| `goal` | `Goal` |
| `progress` | `Progress` |
| `plan` | `list[PlanStep]` |
| `decisions` | `list[Decision]` |
| `findings` | `list[Finding]` |
| `evidence` | `list[Evidence]` |
| `pending_work` | `list[PendingWork]` |
| `approvals` | `list[Approval]` |
| `external_dependencies` | `list[ExternalDependency]` |
| `model` | `ModelState | None` |

Plus the recovery bookkeeping fields `pins`, `unmatched_pin_retractions`,
`attempt_lessons`, and `trajectory_reports`, and the metadata `run_id`,
`version`, `source_sequence`, `status`, `unprojectable_at_sequence`,
`unprojectable_event_type`, `unprojectable_reason`, `created_at`,
`updated_at`.

Provenance / origin enum (`models.py:192`): `DETERMINISTIC` (`:201`), `HUMAN`
(`:210`), `LLM` (`:213`), `EXTERNAL_AGENT` (`:216`). `EXTERNAL_AGENT` is
flagged `REQUIRES_REVIEW`; `LLM` is also flagged `REQUIRES_REVIEW`.

---

## 13. Durable storage `src/continuum/storage/`

| Class / behavior | Source |
|--|--|
| `SQLiteStorage` | `sqlite.py:94` |
| WAL journaling; `synchronous=FULL` | `sqlite.py:121-122` |
| Atomic sequence allocation for event IDs | `sqlite.py` |
| Integrity verified on read; corruption refused | `CorruptedRecord` (`base.py:91`) |
| Concurrent writes fail loudly | `ConcurrentWriteError` (`base.py:84`) |

No exactly-once semantics; the ledger reconciles the gap.

---

## 14. External systems

GitHub, email, APIs: anything an agent performs a side effect against. CONTINUUM
never assumes the effect landed; the Action Ledger and reconcilers manage the
gap.

---

## 15. CLI commands (46) `src/continuum/cli/main.py:3688` (`build_parser`)

`init`, `runs`, `start`, `inspect`, `status`, `history`, `provenance`,
`impact`, `events`, `diff`, `validate`, `health`, `resume`, `notify-test`,
`confirm`, `complete`, `budget`, `tree`, `fork`, `restore`, `merge`,
`compact`, `checkpoint`, `rewind`, `record-plan`, `observe`, `gateway`,
`briefing`, `precompact`, `gate`, `hooks`, `verify`, `forget`, `reconcile`,
`actions`, `show-contract`, `replay`, `export-evidence`, `benchmark`,
`attest-keygen`, `attest`, `attest-verify`, `serve`, `dashboard`, `tui`,
`watch`. All accept `--json`.

---

## 16. Security / canonical hashing `src/continuum/security/hashing.py`

- `canonical()` returns a sorted, JSON-native representation (`:72`).
- Serialization uses `json.dumps(..., sort_keys=True, separators=(",", ":"),
  ensure_ascii=True)` (`:79`).
- Enums are serialized by value (`:33-36`).
- Non-finite floats (`NaN`/`Infinity`) are rejected (`:42-43`).
- `datetime`/`date` serialize as ISO-8601 normalized to UTC
  (`:47-52`).
- Hash-chained events form a tamper-evident audit trail (`events.py`).
- Credentials are referenced, never serialized into state.

---

## 17. Data flows (arrows for the diagram)

A. Recording loop
   Agent -> (record_progress / checkpoint via MCP or SDK) -> Event Log
   Event Log -> (hash chain, append) -> Durable Storage
   Event Log -> State Engine (projection) -> SemanticState
   State Engine -> Versioning / Diff / Validator

B. Action execution (two-phase, idempotent)
   Agent -> MCP `continuum_intercept_action(key)` -> Action Ledger `claim`
   Agent -> performs effect on External System
   Agent -> MCP `continuum_complete_action(key, external_id)` -> Action Ledger `complete`
   Crash between claim and complete -> outcome UNKNOWN -> reconciler probes
   External System

C. Resume after crash or change
   Durable Storage -> State Engine (replay) -> SemanticState
   Environment snapshot -> Validator (diff vs checkpoint environment)
   Validator -> staleness report -> Recovery Engine
   Action Ledger uncertainty -> Recovery Engine
   Recovery Engine -> sealed contract (one next action) -> Resume context -> Agent

D. Checkpoint
   State Engine -> Checkpoint Manager (policy decides) -> Durable Storage
   Restore -> replay gap -> caught-up SemanticState

---

## 18. Suggested diagram layout (verified node labels)

Vertical, tiered. Group boxes by the tiers in section 1.

- Top tier: four agent boxes, "Agents and clients".
- Second tier: three adapter boxes (use the exact class names from section 3),
  "Framework adapters (optional)".
- Third tier: one large container "CONTINUUM SDK". Inside, a 3x3 grid:
  - Row 1: Extractors, Event Log (highlight; source of truth), Action Ledger
  - Row 2: State Engine, Checkpoint Manager, Environment
  - Row 3: Validator, Recovery Engine, Security
  - Pill at top-right of the SDK container:
    "MCP server: deny by default, 12 tools (3 read-only, 9 mutating)".
- Fourth tier: two boxes side by side: "Durable Storage (SQLite, WAL)" and
  "External Systems (GitHub, email, APIs)".
- Bottom tier: one centered box "Resume (bounded recovery context)".

Arrows:
  Agents -> Adapters -> SDK (vertical spine).
  SDK -> Durable Storage (label "events + state").
  SDK -> External Systems (label "side effects").
  SDK -> Resume (label "resume / recovery"; draw as the central orange arrow,
  the safety outcome).
  Inside SDK, a subtle arrow from Event Log toward Durable Storage (persistence).

Visual: brand colors blue `#3B82F6` / `#62AFE0` for containers and clients,
orange `#FF5017` for accents/highlights/the resume arrow, navy `#071827` for
text. White cards, light borders, soft shadows, rounded corners, spacious
layout that uses the full width.
