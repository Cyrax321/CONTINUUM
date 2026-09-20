# Glossary of Recovery Terms

Every term here is used in issues and in code. Definitions point to the implementation so they can be verified, not inferred.

- **Anchor**  
  A checkpoint or ledger entry that must survive cleanup and compaction. In code a checkpoint with `trigger == CheckpointTrigger.RECOVERY` (`src/continuum/checkpoint/policy.py:62`) and a ledger entry with `anchor == True` (`src/continuum/recovery/ledger.py:88`) are anchors. `CheckpointManager.prune` and `RecoveryLedger.compact` keep anchors while dropping older ephemeral entries. See `src/continuum/recovery/cleanup.py:20` for the cleanup rule that preserves referenced anchors.

- **Lease**  
  A short lived, per run ownership claim that prevents two recoveries from acting as authority at once. Implemented by `LeaseCoordinator` in `src/continuum/concurrency/lease.py:58`. The recovery ledger can be constructed with a `LeaseCoordinator` (`src/continuum/recovery/ledger.py:208`) so `append_decision` and `record_attempt` acquire the lease for the run. `Phase 4` added the `RECOVERY` trigger to `CheckpointTrigger` and the `StateCheckpoint.reason` field so a lease protected anchor can be created atomically via `CheckpointManager.checkpoint_on_recovery`.

- **Contract**  
  The machine readable verdict `RecoveryContract` (`src/continuum/models.py:1060`) sealed by hash. It carries `recovery_status`, `checkpoint_version`, `verified` and `invalidated` lists, `required_actions`, a single `next_allowed_action`, plus the Phase 1 fields `evidence` and `reason`. Built by `build_contract` in `src/continuum/recovery/contract.py:83` and surfaced by `RecoveryEngine.assess`. The contract is the single permitted next step. Rendering is in `src/continuum/recovery/contract.py`.

- **Provenance**  
  The three orthogonal axes that trace a fact back to its origin: `Origin` (who asserted it, `src/continuum/models.py:172`), `TrustLevel` (how verified it is, `src/continuum/security/provenance.py:19`), and `StateStatus` (what validity state it is in, `src/continuum/models.py:94`). `src/continuum/provenance_map.py:60` projects these onto `CanonicalProvenance` without deleting the source enums. `ProvenanceView` carries all three source values alongside the canonical labels.

- **Checkpoint**  
  A sealed `StateCheckpoint` (`src/continuum/models.py:1133`) bundling the projected `SemanticState` at a version, the event cursor it covers, and the `EnvironmentSnapshot` it was verified against. Created in `src/continuum/checkpoint/manager.py:168` via `checkpoint`, restored via `restore` which replays events recorded after the checkpoint. The checkpoint policy that decides when to write lives in `src/continuum/checkpoint/policy.py:52`.

- **EnvironmentSnapshot**  
  A capture of the current external world for a run (`src/continuum/models.py:1024`). Each `EnvResource` (`src/continuum/models.py:1014`) records a named resource and its version or checksum. Comparison of a checkpoint environment and a live environment drives `EnvironmentDiff` and the validator's staleness propagation.

- **Validation**  
  `StateValidator` in `src/continuum/state/validator.py:218` decides per component whether the checkpointed state is still true against the live environment. Staleness propagates `dependency -> evidence -> finding -> decision`. This propagation is mirrored by the `DependencyGraph` in `src/continuum/recovery/impact.py:47` for localized repair.

- **Ledger**  
  The append only, tamper evident `RecoveryLedger` (`src/continuum/recovery/ledger.py:205`) over a `LedgerBackend` (memory or JSONL file). Entries are hash chained from `GENESIS`. `verify` reports the last trusted index. `record_attempt` tracks retry counts and writes an anchored `human_required` gate when a threshold is reached. `compact` drops a prefix while re sealing the chain. Reconciliation (`reconcile`) detects drift between the ledger chain and the live state version.

- **Repair plan**  
  `RepairPlan` in `src/continuum/recovery/planner.py:111` listing ordered `RepairStep`s derived from validation statuses and uncertain actions (`plan_repairs`). Steps are ordered so inputs are re-derived before consumers.

- **Adapter action**  
  `AdapterAction` (`src/continuum/adapters/actions.py:24`) is the uniform `name + params + dep_scope` operation that every adapter emits. `AdapterResult` carries the outcome. `run_action` is the facade over `AgentAdapter.intercept_action` where the ledger provides idempotency and the telemetry hook (`on_event`, issue 162) can observe each execution.

- **Telemetry**
  Optional observer callback `on_event` on `run_action` (`src/continuum/adapters/actions.py:53`). Disabled by default. Receives `(AdapterAction, AdapterResult)` for every execution, whether completed or failed. A raising observer is suppressed so observability cannot break the action it observes.

- **Liveness**
  Silence as a recovery signal. Cadence contracts in `src/continuum/recovery/health.py` declare how long each phase may go quiet; `continuum watch` evaluates the log against them, appending `LIVENESS_SILENCE_DETECTED` on breach and `LIVENESS_RECOVERED` on recovery (`src/continuum/events.py`). Breach is advisory: it informs the engine decision without replacing validation.

- **Admissibility**
  Whether a restore point is safe to resume from. `check_admissibility` in `src/continuum/state/validator.py` verifies the checkpoint against ledger history, and the engine refuses an inadmissible anchor (`src/continuum/recovery/engine.py:320`) instead of resuming into a commitment the log contradicts.

- **Risk policy**
  The `.continuum/risk-policy.json` mapping from external risk names to recovery modes, loaded by `load_risk_policy` in `src/continuum/recovery/risk.py`. Operators may only tighten defaults, never loosen them. Matching risks arrive as `RISK_OBSERVED` events and land in the contract's `triggering_risks` section.

- **Authority probes**
  External subprocess checks that settle whether a consumed authority is still valid. Configured in `reconcilers.json` and executed by `probe_authority_verdict` in `src/continuum/reconcilers.py:289`, fed the recorded consumption payload on stdin so verification never depends on the agent being assessed.

- **EXTERNAL_MONITOR**
  The `Origin` value (`src/continuum/models.py:227`) for facts observed by outside systems rather than asserted by the agent: risk witnesses, probe verdicts, liveness readings. Marks data the run consumes but no agent self-certified.

- **AUTHORITY_RECONCILED**
  The event (`src/continuum/events.py`) recording a probe's verdict on a consumed authority. A definitive valid verdict clears the consumed mark and unblocks resume; anything else keeps the run blocked. Every probe result is hash-chained, so the audit trail preserves each one.

Reference from the master plan: these terms appear throughout `docs/CONTINUUM_MASTER_PLAN.md` and `docs/ARCHITECTURE_EVOLUTION.md`. The walkthrough in `docs/recovery_walkthrough.md` shows them interacting in one concrete failure.
