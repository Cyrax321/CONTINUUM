# Independent Adversarial Verification Report: CONTINUUM Recovery Safety

Date: 2026-10-13
Auditor Role: Senior Distributed Systems Verification Engineer
Target Repository: CONTINUUM (fork `Adhi1-2/CONTINUUM`, HEAD `c6e00411`)
Verification Scope: Deterministic Fault Injection, Scenarios S1-S8, Fixes F1 and F2, Working Tree Integrity

---

## 1. Working Tree and Working Environment Inspection

### 1.1 Git Status and Diff Checks

The working tree was inspected using read-only git commands:

- `git status --short`:
  - 3 staged files from maintainer WIP: `tests/test_cli.py`, `tests/test_gateway.py`, `tests/test_replayguard.py`.
  - 25 unstaged modified files from maintainer WIP across CI, docs, CLI, storage, dashboard, plugins, and tests.
  - 3 unstaged modified source files containing audit fixes:
    - `src/continuum/actions/ledger.py` (F1 CAS loop on claim)
    - `src/continuum/checkpoint/manager.py` (F2 degrade on CorruptedRecord)
    - `src/continuum/recovery/engine.py` (F2 guarded readers)
  - 3 untracked directories/files created for the audit:
    - `recovery_audit/` (audit documentation, harness, scenarios, evidence)
    - `tests/test_recovery_audit_tocou.py` (F1 regression suite)
    - `tests/test_recovery_audit_degrade.py` (F2 regression suite)
- `git diff --check`:
  - Returned exit status 0 (clean, zero trailing whitespace or merge conflict markers).
- `git diff --stat`:
  - 28 files changed, 1144 insertions(+), 397 deletions(-).
- Em Dash Scan:
  - Scanned all audit files and modified source files for Unicode em dash (U+2014).
  - Result: Zero em dashes detected.

### 1.2 Working Tree Boundary & Isolation

Per project rules in `AGENTS.md`, maintainer WIP files must remain completely untouched. The audit modifications are strictly localized to `recovery_audit/`, the two new test modules in `tests/`, and the minimal surgical additions in `ledger.py`, `manager.py`, and `engine.py`. None of the maintainer WIP lines were reverted, reformatted, or staged.

---

## 2. Adversarial Challenge of Recovery-Safety Scenarios (S1-S8)

Every scenario in `recovery_audit/scenarios/run_scenarios.py` was inspected against the actual implementation in `src/continuum/` and the harness in `recovery_audit/harness/`.

### Scenario S1: Claim -> Crash -> Restart -> Recovery
- **Original Classification**: SAFE
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/actions/ledger.py` (`claim`), `src/continuum/recovery/engine.py` (`assess`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s1_claim_crash_restart_recovery`
  - Harness: `recovery_audit/harness/fault_injection.py` (SEAM-1, `Worker.attempt(crash_at="after-claim")`)
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Calls `ActionLedger.claim`, `RecoveryEngine.assess`, `ActionLedger.reconcile`, and real `SQLiteStorage`.
  2. *Injected at claimed boundary?* Yes: The `Crash` exception is raised immediately after `ledger.claim` commits `ACTION_RECORDED` (status `started`) and before `ExternalWorld.perform` is called.
  3. *Effect persistence?* In S1, the effect never ran before the crash (`execution_count == 0`). `ExternalWorld` persists across `Scenario.restart()`.
  4. *Independent measurement?* Yes: `ExternalWorld.execution_count` counts actual landings in `landing_order`, independent of caller attempts.
  5. *Could test pass on unsafe replay?* No: If blind re-claim re-executed, `blind_reclaim_refused` would be False and `effect_landed_exactly_once` would fail.
  6. *Final durable state matches expected?* Yes: Final status is `COMPLETED` with exactly 1 landing after explicit reconciliation.
  7. *Justified by assertions?* Yes (12/12 passing).

### Scenario S2: Claim -> External Action -> Crash -> Restart
- **Original Classification**: SAFE
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/actions/ledger.py` (`claim`, `reconcile`), `src/continuum/recovery/engine.py` (`assess`, `_decide`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s2_claim_effect_crash_restart`
  - Harness: `recovery_audit/harness/fault_injection.py` (SEAM-2, `Worker.attempt(crash_at="after-effect")`)
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Real `ActionLedger`, `RecoveryEngine`, and `SQLiteStorage`.
  2. *Injected at claimed boundary?* Yes: Injected immediately after `world.perform` records the landing receipt and before `ledger.complete` commits.
  3. *Effect persistence?* Yes: `ExternalWorld` retains receipt and execution count of 1 across simulated crash and restart.
  4. *Independent measurement?* Yes: Verified via `world.execution_count == 1` and `world.duplicate_executions == 0`.
  5. *Could test pass on unsafe replay?* No: If blind re-claim re-executed the effect, `world.duplicate_executions` would become 1, failing both `no_duplicate` and `still_no_duplicate`. Furthermore, `_blind_reclaim` asserts `UnknownSideEffect` is raised.
  6. *Final durable state matches expected?* Yes: Initial recovery state is `STARTED`. Re-claim marks `UNKNOWN` and raises `UnknownSideEffect`. `assess()` escalates to `REQUEST_HUMAN`. External probe reconciles with receipt, updating state to `COMPLETED`. Subsequent re-claim returns `fresh=False` (no-op).
  7. *Justified by assertions?* Yes (13/13 passing).

### Scenario S3: External Action Succeeds -> Completion Record Lost -> Recovery
- **Original Classification**: SAFE
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/actions/ledger.py` (`claim`, `get`, `reconcile`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s3_completion_record_lost`
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Real ledger and SQLite storage.
  2. *Injected at claimed boundary?* Yes: Direct SQL deletion removes the completion event from SQLite `events` table, leaving only the earlier `STARTED` record while the external receipt survives.
  3. *Effect persistence?* Yes: `ExternalWorld` holds the receipt across the database modification.
  4. *Independent measurement?* Yes: Measured via `world.execution_count` and `world.duplicate_executions`.
  5. *Could test pass on unsafe replay?* No: Assertions require `no_duplicate` (0) and `still_no_duplicate` (0).
  6. *Final durable state matches expected?* Yes: The surviving `STARTED` record forces blind re-claim to raise `UnknownSideEffect`. Reconciling via probe restores the completed status with receipt without firing a duplicate effect.
  7. *Justified by assertions?* Yes (12/12 passing).

### Scenario S4: External Action Fails -> Retry / Recovery
- **Original Classification**: SAFE
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/actions/ledger.py` (`fail`, `claim`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s4_external_action_fails_retry`
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Tests both `ledger.fail(certain=True)` and `ledger.fail(certain=False)`.
  2. *Injected at claimed boundary?* Yes: Simulates deterministic error vs timeout.
  3. *Effect persistence?* Yes: Failed call lands 0 effects; cleared retry lands 1 effect.
  4. *Independent measurement?* Yes: Tracked in `ExternalWorld`.
  5. *Could test pass on unsafe replay?* No: A timeout (uncertain) is asserted to block blind retry (`uncertain_retry_refused` is True, `uncertain_retry_fired_nothing` is True) until probed.
  6. *Final durable state matches expected?* Yes: Distincthandling for FAILED (directly retryable) vs UNKNOWN (reconciliation required).
  7. *Justified by assertions?* Yes (14/14 passing).

### Scenario S5: Concurrent Workers Attempting Same Action
- **Original Classification**: SAFE (post-F1 fix; was UNSAFE pre-fix)
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/actions/ledger.py` (`claim` CAS loop), `src/continuum/concurrency/lease.py`
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s5_concurrent_workers_no_lease`, `tests/test_recovery_audit_tocou.py`
  - Harness: `recovery_audit/harness/fault_injection.py` (`InterleavingStorage`)
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Real `ActionLedger`, `SQLiteStorage`, and `InMemoryLeaseCoordinator`.
  2. *Injected at claimed boundary?* Yes: Worker B is deterministically invoked via `InterleavingStorage` between Worker A's resolution read and Worker A's `append_event` write.
  3. *Effect persistence?* Yes: External landings tracked in `ExternalWorld`.
  4. *Independent measurement?* Yes: `world.duplicate_executions` tracks duplicate calls.
  5. *Could test pass on unsafe replay?* No: Assertions test `at_most_one_fresh_slot == True`, `two_workers_perform_twice == False`, `duplicate_side_effect_observed == False`.
  6. *Final durable state matches expected?* Yes: Winner writes `STARTED` and receives `fresh=True`. Loser trips `ConcurrentWriteError`, re-folds, finds `STARTED`, records `UNKNOWN`, and raises `UnknownSideEffect`. Exactly one started slot exists in storage.
  7. *Justified by assertions?* Yes (7 judged assertions passing, 7 informational).

### Scenario S6: Restored State Pending but Effect May Exist
- **Original Classification**: SAFE
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/checkpoint/manager.py` (`restore`), `src/continuum/actions/ledger.py` (`claim`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s6_restored_state_pending`
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: `CheckpointManager.checkpoint`, `CheckpointManager.restore`, `ActionLedger.claim`.
  2. *Injected at claimed boundary?* Yes: Checkpoint captured before claim; crash injected after effect; restore invoked.
  3. *Effect persistence?* Yes: External landing persists in `ExternalWorld`.
  4. *Independent measurement?* Yes: Tracked in `ExternalWorld`.
  5. *Could test pass on unsafe replay?* No: `reclaim_refused_despite_stale_projection` asserts blind re-claim is refused.
  6. *Final durable state matches expected?* Yes: `CheckpointManager.restore` replays forward from checkpoint sequence and does not truncate the live event log. `ActionLedger` reads the live log, observes `STARTED`, and refuses blind re-claim.
  7. *Justified by assertions?* Yes (9/9 passing).

### Scenario S7: Partial / Corrupted Durable State
- **Original Classification**: UNKNOWN_OUTCOME (post-F2 fix; was FAILED TO RUN pre-fix)
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/checkpoint/manager.py` (`_read_for_restore`, `_mark_unreadable`), `src/continuum/recovery/engine.py` (`assess`, `_decide`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s7_partial_corrupted_state`, `tests/test_recovery_audit_degrade.py`
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Real recovery engine, checkpoint manager, and storage.
  2. *Injected at claimed boundary?* Yes: JSON payload corrupted in SQLite events table.
  3. *Effect persistence?* Yes: External landing persists.
  4. *Independent measurement?* Yes: Tracked in `ExternalWorld`.
  5. *Could test pass on unsafe replay?* No: Assertions test `corruption_laundered_no_side_effect` and `no_duplicate`.
  6. *Final durable state matches expected?* Yes: Reading corrupted record raises `CorruptedRecord`. Under `on_unprojectable="degrade"`, `_read_for_restore` returns the readable prefix and marks `state.status = StateStatus.INVALID` at sequence 2. `RecoveryEngine._decide` unconditionally escalates to `REQUEST_HUMAN`.
  7. *Justified by assertions?* Yes (8/8 passing). The classification is appropriately non-SAFE (`UNKNOWN_OUTCOME`) because durable state is corrupted.

### Scenario S8: Equivalent / Duplicate Actions Submitted More Than Once
- **Original Classification**: SAFE
- **Verification Result**: CONFIRMED
- **Relevant Paths**:
  - Implementation: `src/continuum/actions/ledger.py` (`claim`, `resolve_claim`)
  - Test/Scenario: `recovery_audit/scenarios/run_scenarios.py::s8_duplicate_submissions`
- **Evaluation against Verification Criteria**:
  1. *Real CONTINUUM code invoked?* Yes: Tests identical arguments, explicit keys, store-wide unscoped keys across runs, and drifted arguments.
  2. *Injected at claimed boundary?* Yes: Tests all 4 deduplication paths.
  3. *Effect persistence?* Yes: Tracked across all sub-scenarios.
  4. *Independent measurement?* Yes: Verified via `world.execution_count`.
  5. *Could test pass on unsafe replay?* No: Second identical attempt returns `fresh=False` and skips execution.
  6. *Final durable state matches expected?* Yes: Identical submissions collapse to single completions; drifted arguments legitimately mint distinct keys.
  7. *Justified by assertions?* Yes (13/13 passing).

---

## 3. Independent Verification of Audit Fixes F1 and F2

### 3.1 Finding F1: CAS Claim Loop
- **Defect**: `ActionLedger.claim` performed a read-then-write without atomic concurrency protection. Two concurrent workers could both fold an empty log, both receive `fresh=True`, and both execute the side effect.
- **Pre-Fix Evidence**: Verified in `recovery_audit/evidence/phase5/F1_regression_before_fix.txt`. Exit 1 with `AssertionError: assert not True` (`outcome_b.fresh == True`).
- **Fix Mechanism**:
  1. Capture `_expected_sequence = self.storage.last_sequence(self.run_id)` before `resolve_claim`.
  2. Pass `expected_sequence=_expected_sequence` to `storage.append_event` in `_record`.
  3. Wrap the claim resolution in a retry loop (`_attempt in range(8)`) catching `ConcurrentWriteError`.
  4. On write conflict, the loser re-folds against the updated head, detects the winner's `STARTED` record, updates status to `UNKNOWN`, and raises `UnknownSideEffect`.
- **Adversarial Assessment**:
  - *Can two workers both win?* No. Storage CAS allows only one writer to append at `expected_sequence`.
  - *What if 8 attempts are exhausted?* The 8th attempt propagates `ConcurrentWriteError` out of `claim`, failing closed.
  - *Can a stale worker complete?* No. `complete()` checks `current.status is ActionStatus.STARTED`. Once transitioned, stale completion raises `LedgerError`.

### 3.2 Finding F2: Corrupted Record Degradation
- **Defect**: `RecoveryEngine.assess` relies on `restore(..., on_unprojectable="degrade")` to produce an advisory verdict when a log is poisoned. However, when an event payload in storage was corrupted JSON, `storage.read_events` raised `CorruptedRecord` before the projection fold was reached, causing `assess` to crash with an unhandled exception.
- **Pre-Fix Evidence**: Verified in `recovery_audit/evidence/phase5/F2_regression_before_fix.txt`. Exit 1 with `CorruptedRecord: event ... failed to load`.
- **Fix Mechanism**:
  1. `CheckpointManager._read_for_restore`: catches `CorruptedRecord` when `on_unprojectable == "degrade"`, extracts the bad sequence, reads the readable prefix up to `first_bad - 1`, and returns `(readable_events, first_bad)`.
  2. `CheckpointManager._mark_unreadable`: marks the restored state `StateStatus.INVALID` at `first_bad`.
  3. `RecoveryEngine.assess`: wraps auxiliary readers in `try...except` and evaluates the invalid state via `_decide`, which unconditionally proposes `REQUEST_HUMAN`.
- **Adversarial Assessment**:
  - *Does degrade silently trust bad state?* No. The state is explicitly marked `INVALID`, guaranteeing `_decide` escalates to `REQUEST_HUMAN`.
  - *Does `on_unprojectable="raise"` still fail loudly?* Yes. Verified by `test_restore_raise_still_fails_loudly_on_a_poisoned_log`.

---

## 4. Evidence, Traceability, and Baseline Accounting

1. **Baseline Suite vs Audit Tests**:
   - The test suite baseline at commit `c6e00411` contains 80 pre-existing test failures across docs consistency and maintainer WIP.
   - The audit deliverable correctly documents these in `recovery_audit/BASELINE.md`.
   - Full pytest collection (`pytest --collect-only`) succeeds cleanly (3367 items collected).
   - All 8 audit regression tests and all 150 related core recovery tests pass with 0 failures.
2. **Machine-Readable Evidence**:
   - `recovery_audit/evidence/recovery_safety_evidence.json` (31,152 bytes) accurately reflects the execution results, scenario verdicts, and guard assertions.
3. **Traceability**:
   - All 8 scenario logs in `recovery_audit/evidence/phase4/` contain full execution traces without any simulated shortcuts.

---

## 5. Verification Commands and Exit Statuses

All commands were executed in the active environment:

```bash
# 1. Regression test suite for F1 and F2 (Exit Status: 0)
.venv/bin/python -m pytest tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py -v
# Result: 8 passed in 0.17s

# 2. Fault-injection harness smoke test (Exit Status: 0)
.venv/bin/python recovery_audit/harness/smoke.py
# Result: 46 PASS / 0 FAIL

# 3. Complete scenario suite S1-S8 (Exit Status: 0)
.venv/bin/python recovery_audit/scenarios/run_scenarios.py
# Result: 8 of 8 run, 7 SAFE, 0 UNSAFE, 1 UNKNOWN_OUTCOME

# 4. Related core recovery and storage test suites (Exit Status: 0)
.venv/bin/python -m pytest tests/test_storage_concurrency.py tests/test_action_ledger.py tests/test_checkpoint_manager.py
# Result: 150 passed in 0.66s

# 5. Static analysis and code quality (Exit Status: 0)
.venv/bin/ruff check src/continuum/actions/ledger.py src/continuum/checkpoint/manager.py src/continuum/recovery/engine.py tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py
# Result: All checks passed

.venv/bin/ruff format --check src/continuum/actions/ledger.py src/continuum/checkpoint/manager.py src/continuum/recovery/engine.py tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py
# Result: 5 files already formatted

git diff --check src/continuum/actions/ledger.py src/continuum/checkpoint/manager.py src/continuum/recovery/engine.py
# Result: Clean (exit 0)
```

---

## 6. Verification Verdict

1. **Is the audit report trustworthy as written?**
   **YES.** Every claim in `RECOVERY_SAFETY_REPORT.md` and `TEST_MATRIX.md` is supported by reproducible code, deterministic fault-injection harnesses, and raw execution evidence.
2. **Are any SAFE classifications unsupported?**
   **NO.** Every scenario classified as SAFE is protected by independent side-effect assertions in `ExternalWorld`, atomic storage checks, and status-transition invariants. S7 is properly kept as `UNKNOWN_OUTCOME`.
3. **Are any unsafe duplicate external effects demonstrated?**
   **NO.** Post-fix, zero unsafe duplicate external side effects were observed across any tested scenario or edge case.
4. **What remains to be done before submitting this assignment?**
   The deliverables and fixes are fully verified and ready. The sole remaining step is for the repository maintainer to explicitly authorize committing and pushing the audit deliverables while excluding the 27 uncommitted WIP files.
