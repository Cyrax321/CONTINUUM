# CONTINUUM Recovery-Safety Audit: Engineering Handoff

To: Team Lead / Maintainers
From: Senior Distributed Systems Verification Engineer
Date: 2026-10-13
Repository: CONTINUUM (fork `Adhi1-2/CONTINUUM`, HEAD `c6e00411`)
Deliverable Root: `recovery_audit/`

---

## 1. Mission and Scope

The objective of this assignment was to conduct a comprehensive recovery-safety audit of CONTINUUM to answer a central question:
*Can CONTINUUM safely recover when a process crashes before, during, or after an external action?*

The primary acceptance criterion is:
**ZERO UNSAFE DUPLICATE EXTERNAL SIDE EFFECTS.**
If an external action may already have succeeded but its durable completion record is missing, recovery must never blindly re-execute it. CONTINUUM implements honest at-least-once execution with mandatory reconciliation; it does not claim universal exactly-once semantics.

The audit covered Phases 1 through 8:
- Phase 1: Baseline repository reconnaissance and classification of existing failures.
- Phase 2: Architecture mapping of action lifecycles, storage CAS, and recovery contracts.
- Phase 3: Deterministic fault-injection harness implementation.
- Phase 4: Full automated execution of scenarios S1 through S8.
- Phase 5: Deterministic bug reproduction and implementation of minimal fixes for findings F1 and F2.
- Phase 6: Production of test matrix and machine-readable evidence artifact.
- Phase 7: Technical recovery-safety report.
- Phase 8: Independent adversarial verification, sanitization (zero em dashes), and final handoff.

---

## 2. Discovered Correctness Bugs

Two high-severity correctness defects were discovered on the audited tree:

### Finding F1: TOCTOU Race in `ActionLedger.claim` (Duplicate External Side Effect)
- **Defect**: In `src/continuum/actions/ledger.py`, `ActionLedger.claim` folded the event log to check if an action slot was available, and then appended an `ACTION_RECORDED` event. This read-then-write sequence was not atomic.
- **Impact**: When two concurrent workers claimed the same action key without a lease, both folded an empty log, both received `fresh=True`, and both performed the external side effect. This directly violated the audit's core acceptance criterion.

### Finding F2: Corrupted Record Unhandled Exception in `RecoveryEngine.assess`
- **Defect**: In `src/continuum/checkpoint/manager.py` and `src/continuum/recovery/engine.py`, `assess` relies on `restore(..., on_unprojectable="degrade")` to produce an advisory verdict when a log is damaged. However, when an event payload in SQLite was corrupted JSON, `storage.read_events` raised `CorruptedRecord` during read, before the fold's degrade handler could engage. Auxiliary readers inside `assess` also raised unhandled exceptions.
- **Impact**: A damaged log crashed the assessment with a traceback instead of producing an actionable recovery verdict. Suppressing the error without marking the state would have caused the engine to return `mode=RESUME` over an unreadable log, creating an unsafe green light.

---

## 3. Fixes and Regression Tests

Both fixes were implemented with minimal changes to production code and verified with dedicated regression tests:

### F1 Fix and Regression Test
- **Implementation** (`src/continuum/actions/ledger.py`):
  - Captures `_expected_sequence = self.storage.last_sequence(self.run_id)` before calling `resolve_claim`.
  - Forwards `expected_sequence` to `storage.append_event` in `_record`.
  - Wraps claim resolution in a bounded CAS retry loop (`_CLAIM_CAS_ATTEMPTS = 8`).
  - On write conflict (`ConcurrentWriteError`), the loser re-folds against the updated head, detects the winner's `STARTED` record, updates status to `UNKNOWN`, and raises `UnknownSideEffect`.
- **Regression Suite**: `tests/test_recovery_audit_tocou.py` (3 tests).
  - Pre-fix result: Failed with exit 1 (`AssertionError: assert not True` on `outcome_b.fresh`).
  - Post-fix result: 3 passed in 0.08s (exit 0).

### F2 Fix and Regression Test
- **Implementation** (`src/continuum/checkpoint/manager.py`, `src/continuum/recovery/engine.py`):
  - Added `CheckpointManager._read_for_restore`: catches `CorruptedRecord` under `degrade` mode, extracts the damaged sequence number, and returns the readable prefix up to `first_bad - 1`.
  - Added `CheckpointManager._mark_unreadable`: marks the restored state `StateStatus.INVALID` at `first_bad`.
  - Guarded auxiliary readers in `RecoveryEngine.assess` against `CorruptedRecord`.
  - `RecoveryEngine._decide` unconditionally escalates invalid states to `RecoveryMode.REQUEST_HUMAN`.
- **Regression Suite**: `tests/test_recovery_audit_degrade.py` (5 tests).
  - Pre-fix result: Failed with exit 1 (`CorruptedRecord` unhandled traceback).
  - Post-fix result: 5 passed in 0.12s (exit 0).

---

## 4. Deterministic Fault-Injection Harness and Scenarios

The harness is implemented in `recovery_audit/harness/fault_injection.py` and documented in `recovery_audit/HARNESS_DESIGN.md`:
- Subclasses real `SQLiteStorage` (`CrashingStorage`), preserving WAL mode, hash chaining, and rollback invariants.
- Uses `ExternalWorld` to count and log actual external side effects independently of caller attempt counts.
- Injects counted deterministic `BaseException` crashes at real execution boundaries without sleeps, timeouts, background threads, or network calls.
- Validated by `recovery_audit/harness/smoke.py` (46/46 checks passed).

### Eight Scenarios Tested (`recovery_audit/scenarios/run_scenarios.py`)
1. **S1 (Claim -> Crash -> Restart -> Recovery)**: SEAM-1. Claim durable, crash before effect. Recovery sees `STARTED`, refuses blind re-claim, probes absence, reconciles to `FAILED`, retries cleanly. -> **SAFE**
2. **S2 (Claim -> Effect -> Crash -> Restart)**: SEAM-2. Effect lands externally, crash before completion is durable. Recovery refuses blind re-claim, probes receipt, reconciles to `COMPLETED`, subsequent claim is a no-op. -> **SAFE**
3. **S3 (Completion Record Destroyed)**: Completion event deleted via direct SQL. Recovery detects `STARTED`, refuses blind re-claim, probes receipt, restores completion. -> **SAFE**
4. **S4 (Action Fails -> Retry)**: Evaluates deterministic error vs timeout. Certain failure marks `FAILED` and retries directly; uncertain timeout marks `UNKNOWN` and blocks retry until probed. -> **SAFE**
5. **S5 (Concurrent Workers on Same Action)**: Evaluates unleased claim race and leased worker. Unleased race handled by CAS retry loop; leased worker fenced by `ClaimLockError`. -> **SAFE**
6. **S6 (Restored State Pending but Effect May Exist)**: Checkpoint captured before claim. Restoring forward preserves in-flight `ACTION_RECORDED` event; live log protects slot from replay. -> **SAFE**
7. **S7 (Partial / Corrupted Log Payload)**: Completion payload corrupted in place. System fails closed, degrades cleanly, and escalates to human intervention with zero duplicate effects. -> **UNKNOWN_OUTCOME**
8. **S8 (Duplicate Submissions)**: Tests identical arguments, explicit keys, store-wide unscoped keys across runs, and drifted arguments. Identical submissions collapse; drifted payloads mint distinct keys. -> **SAFE**

---

## 5. Actual Test Results and Evidence Paths

All test results were directly executed and verified in this session:

- **Audit Regression Tests**:
  - Command: `.venv/bin/python -m pytest tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py -v`
  - Output: 8 passed in 0.20s (Exit code: 0)
- **Harness Smoke Test**:
  - Command: `.venv/bin/python recovery_audit/harness/smoke.py`
  - Output: 46 PASS / 0 FAIL (Exit code: 0)
- **Scenario Suite S1-S8**:
  - Command: `.venv/bin/python recovery_audit/scenarios/run_scenarios.py`
  - Output: 8 run; 7 SAFE, 0 UNSAFE, 1 UNKNOWN_OUTCOME; 88/88 judged assertions passed (Exit code: 0)
- **Related Core Recovery Test Suites**:
  - Command: `.venv/bin/python -m pytest tests/test_storage_concurrency.py tests/test_action_ledger.py tests/test_checkpoint_manager.py`
  - Output: 150 passed in 0.66s (Exit code: 0)

### Evidence Directory Layout
- Machine-Readable Summary: `recovery_audit/evidence/recovery_safety_evidence.json` (31 KB)
- Per-Scenario Traces and Logs: `recovery_audit/evidence/phase4/` (17 JSON and TXT logs)
- Pre- and Post-Fix Reproduction Evidence: `recovery_audit/evidence/phase5/` (`F1_regression_*.txt`, `F2_regression_*.txt`)
- Baseline Test Suite Logs: `recovery_audit/evidence/baseline_suite_output.txt`, `baseline_suite_junit.xml`

---

## 6. S7 UNKNOWN_OUTCOME and Human Intervention

Scenario S7 corrupts an event payload at sequence 2 in SQLite.
The result is classified as **`UNKNOWN_OUTCOME`**, not `SAFE`.

**Why Human Intervention is Required**:
When durable records are damaged, the engine can read the valid prefix up to sequence 1, but cannot determine what events or side effects occurred at sequence 2 or later. Scoring this scenario as SAFE would dishonestly claim that unreadable bytes are verified. The fail-safe behavior is to degrade to `StateStatus.INVALID` and propose `RecoveryMode.REQUEST_HUMAN`, guaranteeing that no blind actions are re-executed while alerting an operator to inspect or repair the log.

---

## 7. Baseline Failures and Context

Running the full repository test suite against the frozen baseline at commit `c6e00411` yielded 80 failures out of 3359 collected tests. These are fully documented in `recovery_audit/BASELINE.md`:
- ~34 failures: Documentation self-consistency count drift.
- ~22 failures: Pre-existing maintainer WIP refactoring `dependency` to `scope` in `RecoveryLedgerEntry`.
- ~8 failures: CLI surface drift.
- 1 failure: Optional `psycopg` dependency missing.
- Baseline defects B0-1 (unterminated docstring in `sqlite.py`) and B0-2 (missing `_liveness_digest_value` in `contract.py`) are present at commit `c6e00411`, but masked by the maintainer's working tree WIP.

The audit fixes introduced zero new test failures.

---

## 8. Remaining Risks and External-System Assumptions

1. **Reconciliation Probe Availability**:
   CONTINUUM relies on reconciliation probes to confirm if an in-flight action landed after a crash at SEAM-2. If an external system provides no queryable receipt API, idempotency token, or status endpoint, CONTINUUM cannot settle the action automatically and must escalate to `REQUEST_HUMAN`.
2. **Content-Addressed Derived Keys**:
   If an application mutates action arguments across retries (e.g., adding dynamic timestamps), CONTINUUM treats it as a distinct action and derives a new key. Applications must supply explicit caller keys when payloads vary across retries.

---

## 9. Exact Files Intended for Submission

The following files constitute the audit deliverables and minimal production fixes:

```
# Audit documentation, harness, scenarios, and evidence
recovery_audit/BASELINE.md
recovery_audit/ARCHITECTURE.md
recovery_audit/HARNESS_DESIGN.md
recovery_audit/TEST_MATRIX.md
recovery_audit/RECOVERY_SAFETY_REPORT.md
recovery_audit/INDEPENDENT_VERIFICATION.md
recovery_audit/PROGRESS.md
recovery_audit/HANDOFF.md
recovery_audit/harness/
recovery_audit/scenarios/
recovery_audit/evidence/

# Deterministic regression test suites
tests/test_recovery_audit_tocou.py
tests/test_recovery_audit_degrade.py

# Production source fixes
src/continuum/actions/ledger.py
src/continuum/checkpoint/manager.py
src/continuum/recovery/engine.py
```

All 28 maintainer WIP files across CI, docs, CLI, adapters, dashboard, and tests remain untouched and will NOT be included in the commit.

---

## 10. Authorization Request

All verification gates have passed. In accordance with `AGENTS.md`, **no git commits, branch mutations, or pushes have been performed**.

Please review the diffs in the three production files and authorization is requested to stage and commit only the audit files listed above.
