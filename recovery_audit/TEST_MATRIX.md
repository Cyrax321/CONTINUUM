# CONTINUUM Recovery-Safety Audit - Test Matrix

Phase 6 deliverable. This maps every fault-injection scenario to the seam it
attacks, the fault it injects, the verdict it earned, and the specific
assertions that guard the acceptance criterion. It is the human-readable index
into the machine-readable bundle at
[`evidence/recovery_safety_evidence.json`](evidence/recovery_safety_evidence.json).

**Acceptance criterion.** Zero unsafe duplicate external side effects: an
external action that may already have succeeded must never be blindly
re-executed because its durable completion record is missing.

**What the matrix does not claim.** Exactly-once execution. CONTINUUM implements
*honest at-least-once with mandatory reconciliation*. The property under test is
narrower and verifiable: when the ledger cannot prove an effect is settled, it
refuses to perform it again and escalates. See the S3 and S6 rows for where that
refusal is load-bearing.

---

## How to read a row

| Column | Meaning |
|---|---|
| **Seam** | The code boundary the fault is injected at. SEAM-1/SEAM-2 are the two crash windows of the claim→effect→complete protocol; the rest are concurrency, corruption, and idempotency seams. |
| **Injected fault** | What the harness actually does. Every fault is deterministic - a counted crash point, an interleaved storage call, or a direct byte-level write. No timing, no sleeps, no flaky schedules. |
| **Verdict** | `SAFE` (the side effect landed once, or was correctly refused), `UNSAFE` (a duplicate or laundered effect), `UNKNOWN_OUTCOME` (the run cannot be answered from durable state and the harness says so instead of guessing). |
| **Executions** | Count of `world.perform()` calls that reached the external system. |
| **Guard assertions** | The assertions that would have to change for the verdict to flip. Negations are marked `(True = defect)` - for those, a `False` reading is the pass. |

**Assertion accounting.** A scenario's `assertions_total` counts *judged*
assertions only. Descriptive outcomes that are legitimate either way - which
worker won a race, how many slots a schedule happened to open - are recorded as
`informational` and printed `[INFO]`, not scored. This is deliberate: labelling
one scheduling outcome `FAIL` would report a timing detail as a safety defect,
which is precisely the misreading this audit exists to prevent. Every remaining
`[FAIL]` in the evidence tree is a real defect, and there are none.

---

## Scenario matrix

### S1 - Claim → Crash → Restart → Recovery

| | |
|---|---|
| **Seam** | SEAM-1: `claim()` has returned, `world.perform()` has not run |
| **Injected fault** | `crash_at="after-claim"` - `Crash(BaseException)` fires between the durable ACTION_RECORDED append and the external call. The worker, ledger, and storage handle are abandoned with no `close()` or flush, which is exactly what a SIGKILL skips. Restart rebuilds fresh objects against the same on-disk artifacts; only `ExternalWorld` survives, because the external system is a separate process. |
| **Verdict** | **SAFE** |
| **Executions** | 1 |
| **Root question** | The claim is durable but the effect never ran. Does recovery re-perform, or does it notice the slot is taken? |
| **Guard assertions** (12/12) | `blind_reclaim_refused`, `blind_retry_landed_nothing`, `assess_escalates`, `assess_names_the_uncertain_action`, `surviving_action_is_started`, `effect_never_landed`, `probe_found_no_effect`, `retry_after_reconcile_succeeded`, `effect_landed_exactly_once`, `no_duplicate`, `still_no_duplicate`, `exactly_one_action_survived` |
| **Pre-fix behaviour** | Safe. `claim()` reads a STARTED record for the identity and returns `fresh=False` before any effect. This is the seam the protocol gets right by construction, and it is the control the other scenarios are measured against. |

### S2 - Claim → External Action → Crash → Restart

| | |
|---|---|
| **Seam** | SEAM-2: `world.perform()` has returned, the completion record is not yet durable |
| **Injected fault** | `crash_at="after-effect"` - the external system has been contacted and has returned a receipt, then the process dies before `settle`. Restart sees only STARTED. |
| **Verdict** | **SAFE** |
| **Executions** | 1 |
| **Root question** | The effect landed but the log does not say so. This is the acceptance criterion's exact phrasing. |
| **Guard assertions** (13/13) | `blind_reclaim_refused`, `blind_retry_landed_no_second_effect`, `assess_escalates`, `reconcile_confirmed_with_receipt`, `receipt_recorded`, `reclaim_after_reconcile_is_a_noop`, `reclaim_did_not_reexecute`, `effect_landed_before_crash`, `one_action_survived`, `surviving_action_is_started`, `external_id_lost`, `no_duplicate`, `still_no_duplicate` |
| **Pre-fix behaviour** | Safe. The STARTED record blocks the re-claim, `assess` escalates rather than resuming, and the probe's receipt restores the completion so the re-claim becomes a no-op. The recovery path performs zero new effects. |

### S3 - External Action Succeeds → Completion Record Lost → Recovery

| | |
|---|---|
| **Seam** | The durable completion record, after it is written and then destroyed |
| **Injected fault** | A direct byte-level delete of the COMPLETED event from the log, simulating storage decay or a partial-write torn between the append and its acknowledgement. The projection falls back to STARTED. |
| **Verdict** | **SAFE** |
| **Executions** | 1 |
| **Root question** | Does the ledger trust the projection over the external system, or probe first? |
| **Guard assertions** (12/12) | `blind_reclaim_refused`, `blind_retry_landed_no_second_effect`, `reconcile_restored_completion`, `receipt_restored`, `action_was_completed`, `receipt_was_recorded`, `effect_landed_once`, `record_now_reads_started`, `external_id_gone_from_record`, `effect_still_landed_once`, `no_duplicate`, `still_no_duplicate` |
| **Pre-fix behaviour** | Safe. This is the strongest row in the matrix: the log actively disagrees with the external system, and the ledger still does not re-perform. It probes, reconciles the receipt back into the record, and only then re-claims - at which point the slot reads COMPLETED and nothing fires. |

### S4 - External Action Fails → Retry / Recovery

| | |
|---|---|
| **Seam** | The external call's failure mode, and how the caller records it |
| **Injected fault** | Two sub-cases. (a) `fail_mode="fail"` - the external system raises, the caller records FAILED with `side_effect_uncertain=False`. (b) `fail_mode="uncertain"` - a timeout, where the call's outcome is genuinely unknown, recorded UNKNOWN. The external system is then told to stop failing via `world.recover()`, so the retry has something to succeed against. |
| **Verdict** | **SAFE** |
| **Executions** | 2 (one failed attempt, one successful retry) |
| **Root question** | Is a *known* failure retried directly, and an *unknown* one refused until reconciled? |
| **Guard assertions** (14/14) | `first_attempt_recorded_failed`, `failed_attempt_landed_nothing`, `record_is_not_uncertain`, `retry_succeeded`, `retry_landed_exactly_once`, `no_duplicate`, `uncertain_retry_refused`, `uncertain_retry_fired_nothing`, `timeout_record_is_uncertain`, `reconcile_settled_the_timeout`, `retry_after_timeout_reconcile_succeeded`, `timeout_retry_landed_once`, `timeout_no_duplicate`, `timeout_recorded_unknown` |
| **Pre-fix behaviour** | Safe. The distinction the ledger draws between FAILED and UNKNOWN is what makes direct retry safe here: a certain failure means the effect provably did not land, so retry costs nothing. An uncertain one provably might have, so it is refused. This row would break if `ledger.fail` ever stopped distinguishing the two. |

### S5a - Concurrent Workers, Same Action, No Lease

| | |
|---|---|
| **Seam** | The read-then-write inside `ActionLedger.claim`: fold the log to decide the slot is free, then append |
| **Injected fault** | `InterleavingStorage` inserts worker B's `claim` into the *middle* of worker A's - B's `read_events` runs after A's read but before A's append. This is the real TOCTOU window, reached without any thread or timing dependency: the interleave is a counted, deterministic injection at the storage boundary. |
| **Verdict** | **SAFE** (after finding **F1**) |
| **Executions** | 1 |
| **Root question** | Two workers both fold an empty slot and both append. How many perform? |
| **Guard assertions** (7 judged) | `at_most_one_fresh_slot`, `the_loser_was_refused_loudly`, `exactly_one_started_slot`, `leased_scenario_no_duplicate`, `both_workers_believed_they_held_the_slot` (True = defect), `two_workers_perform_twice` (True = defect), `duplicate_side_effect_observed` (True = defect) |
| **Informational** (7) | `worker_a_claimed_a_fresh_slot`, `worker_b_claimed_a_fresh_slot`, `interleave_actually_fired`, `loser_refusal_type` (`UnknownSideEffect`), `started_slots_opened` (`1`), `lease_held_by_a`, `lease_fenced_worker_b` |
| **Pre-fix behaviour** | **UNSAFE.** Both claims received `fresh=True` and both workers performed the side effect. See finding **F1** below. |

### S6 - Restored State Pending but Effect May Exist

| | |
|---|---|
| **Seam** | `CheckpointManager.restore` - a checkpoint taken *before* the claim, replayed forward |
| **Injected fault** | A checkpoint is captured with no action in flight; then the claim, effect, and a SEAM-2 crash occur. The restored projection is built from the stale checkpoint and carries no knowledge of the in-flight action. |
| **Verdict** | **SAFE** |
| **Executions** | 1 |
| **Root question** | Does a stale checkpoint launder an in-flight slot, making the action look free again? |
| **Guard assertions** (9/9) | `reclaim_refused_despite_stale_projection`, `blind_retry_landed_nothing_new`, `reconcile_confirmed`, `reclaim_after_reconcile_did_not_reexecute`, `effect_landed_before_crash`, `restore_replayed_forward`, `checkpoint_predates_the_claim`, `no_duplicate`, `still_no_duplicate` |
| **Pre-fix behavior** | Safe. `restore` replays *forward* from the checkpoint and does not truncate the log, so the ACTION_RECORDED event survives and keeps protecting the identity. This is the property that makes checkpointing safe to interleave with an in-flight protocol - worth noting because "restore to an earlier point" and "the effect may already exist" are in direct tension, and the implementation resolves it in the right direction. |

### S7 - Partial / Corrupted Durable State

| | |
|---|---|
| **Seam** | The storage read path itself - a payload the parser cannot load |
| **Injected fault** | A direct `UPDATE events SET payload = '{"key": "k1", "brok'` - truncating the JSON mid-string so the completion record is unparseable. Damage to durable bytes, not a fold error. |
| **Verdict** | **UNKNOWN_OUTCOME** (after finding **F2**) |
| **Executions** | 1 |
| **Root question** | When the log is unreadable, does the engine answer, raise, or - worst case - advise resuming over state it cannot verify? |
| **Guard assertions** (8/8) | `corrupted_read_fails_closed`, `assess_degraded_to_a_verdict`, `assess_escalated_or_aborted`, `claim_on_poisoned_log_refused`, `corruption_laundered_no_side_effect`, `baseline_was_completed`, `effect_landed_once`, `no_duplicate` |
| **Pre-fix behaviour** | **Raise.** `read_events` raised `CorruptedRecord` out of `assess`, before the fold's `on_unprojectable="degrade"` could engage - so the run that most needed a diagnosis got an exception instead. See finding **F2** below. |
| **Why UNKNOWN_OUTCOME, not SAFE** | A log the storage layer cannot read is untrustworthy, and no amount of careful degradation changes that. The honest answer is `request_human`, and scoring it SAFE would claim a guarantee the storage cannot support. This is the matrix's one deliberately non-SAFE row. |

### S8 - Equivalent / Duplicate Actions Submitted More Than Once

| | |
|---|---|
| **Seam** | Idempotency-key derivation - three separate derivation paths, plus a drift case |
| **Injected fault** | Four sub-cases, none of which crash: (a) identical arguments with no explicit key, (b) an explicit caller-supplied key, (c) a store-wide unscoped key that must collapse across runs, (d) *drifted* arguments that should mint a distinct key and therefore legitimately re-execute. |
| **Verdict** | **SAFE** |
| **Executions** | 3 (one per distinct identity - the drift case is expected to fire) |
| **Root question** | Does each derivation path collapse equivalent submissions to one effect, while still allowing a genuinely different submission through? |
| **Guard assertions** (13/13) | `second_identical_claim_is_a_noop`, `identical_submission_landed_once`, `identical_no_duplicate`, `explicit_key_second_claim_is_a_noop`, `explicit_key_landed_once`, `explicit_key_no_duplicate`, `cross_run_claim_deduplicated`, `cross_run_claim_did_not_open_a_parallel_slot`, `cross_run_landed_once`, `cross_run_no_duplicate`, `drifted_arguments_mint_a_distinct_key`, `drifted_reclaim_still_blocked_when_in_flight`, `drifted_no_duplicate` |
| **Pre-fix behaviour** | Safe. Sub-case (d) is the one that makes this row meaningful: a deduplication scheme that collapsed drifted arguments would be a silent correctness bug, and `drifted_arguments_mint_a_distinct_key` exists to catch exactly that regression. |

---

## Findings the matrix surfaced

Both findings were reproduced by a deterministic regression test **before** any
fix was written, and the failing run was captured to
[`evidence/phase5/`](evidence/phase5/).

### F1 - TOCTOU race in `ActionLedger.claim` (High, duplicate side effect)

**Component.** `continuum/actions/ledger.py`, `ActionLedger.claim`.

**Symptom.** Under the S5a interleave, both workers folded an empty slot, both
appended an ACTION_RECORDED record, both received `fresh=True`, and both
performed the side effect. Two charges for one intent.

**Root cause.** `claim` is a read-then-write: it folds the log to decide the
identity is free, then appends. Nothing made those two steps atomic. The storage
layer already exposed the compare-and-set - `append_event(expected_sequence=...)`
raises `ConcurrentWriteError` when the log head has moved - but `claim` never
passed one.

**Fix.** Capture `storage.last_sequence()` before `resolve_claim`, thread
`expected_sequence` through the three slot-opening `_record` calls, and on
`ConcurrentWriteError` re-fold and let the resolution decide again. The winner's
STARTED record is now visible on the re-fold, so the existing branch raises
`UnknownSideEffect` instead of opening a second slot. The loop is bounded
(`_CLAIM_CAS_ATTEMPTS = 8`) so a permanently busy run answers rather than
spinning.

**Why the loser's error is `UnknownSideEffect`, not `ConcurrentWriteError`.**
The raw storage exception leaking through the ledger's abstraction boundary
broke the maintainers' own concurrency test, whose docstring is explicit:
*"If a future change makes claiming atomic in storage, the assertion below
becomes `== 1`. That is the signal to tighten it, not to delete it."* The loser
of a claim race has exactly the same problem as a caller who finds a
half-finished attempt - it cannot know whether the effect landed - so it gets
the same answer.

**Regression test.** [`tests/test_recovery_audit_tocou.py`](../tests/test_recovery_audit_tocou.py), 3 tests. Pre-fix:
exit 1 ([`F1_regression_before_fix.txt`](evidence/phase5/F1_regression_before_fix.txt)). Post-fix: exit 0, 3 passed.

### F2 - A poisoned log raised instead of degrading (High, silent unsafe resumption risk)

**Component.** `continuum/checkpoint/manager.py`, `CheckpointManager.restore`;
`continuum/recovery/engine.py`, `RecoveryEngine.assess`.

**Symptom.** `assess` on a poisoned log raised `CorruptedRecord` - contradicting
the engine's own issue #383 contract, asserted at `engine.py:348-352`:
*"Degrade, not raise: the engine's whole job is to answer 'where does this run
stand', and a poisoned log is precisely the run that needs an answer."*

**Root cause.** Storage refuses a corrupted payload on *read*, raising
`CorruptedRecord` before the fold's `on_unprojectable="degrade"` machinery can
engage. That machinery only handles an *unprojectable fold* - an event it can
see but cannot interpret. Five auxiliary readers inside `assess` (the archive
scan, two `action_ledger.all()` calls, `collect_observations`,
`build_informed_retry`) had no degrade path either.

**The trap the fix had to avoid.** Suppressing those exceptions naively would
have been worse than the crash: the fold would see only the clean prefix, the
restored state would look `valid`, and `assess` would return `mode=resume` -
advising the caller to resume work over a log that cannot be verified. The fix
therefore *marks* rather than silences: `_mark_unreadable` sets
`StateStatus.INVALID` with `unprojectable_at_sequence` pointing at the sequence
storage refused, and `_decide` escalates on that.

**Fix.** Under `on_unprojectable="degrade"`, read the readable prefix and pair it
with the sequence the storage layer refused; mark the resulting state INVALID so
the decision escalates rather than resumes. The `raise` path is untouched - the
write path's default still fails loudly, which
[`test_restore_raise_still_fails_loudly_on_a_poisoned_log`](../tests/test_recovery_audit_degrade.py)
guards.

**Regression test.** [`tests/test_recovery_audit_degrade.py`](../tests/test_recovery_audit_degrade.py), 5 tests. Pre-fix:
exit 1, `CorruptedRecord` escaped `assess`
([`F2_regression_before_fix.txt`](evidence/phase5/F2_regression_before_fix.txt)). Post-fix: exit 0, 5 passed.

---

## Verdict against the acceptance criterion

| Measure | Result |
|---|---|
| Scenarios run | 8 of 8 |
| UNSAFE (pre-fix) | 1 (S5a, finding F1) |
| UNSAFE (post-fix) | **0** |
| Unknown by design | 1 (S7) |
| Unsafe duplicate side effects after fixes | **0** |
| Harness smoke checks | 46 PASS / 0 FAIL |
| Judged assertions across all scenarios | 88 / 88 passing |
| Evidence tree `[FAIL]` labels | 0 |

**Claim made.** CONTINUUM implements at-least-once execution with mandatory
reconciliation, and when it cannot prove an effect is settled it refuses to
repeat the effect and escalates. That claim is supported by the matrix above.

**Claim deliberately not made.** Exactly-once execution. The S2 and S3 rows show
the protocol relying on an external probe to settle an uncertain effect, and an
at-least-once system that probes correctly is not an exactly-once system. S7 is
the honest limit case: some runs cannot be settled from durable state at all,
and the correct behaviour is to say so.

---

## Reproducing

```bash
cd /private/tmp/audit-recovery

# The 8 scenarios (writes evidence/phase4/)
PYTHONPATH=src .venv/bin/python recovery_audit/scenarios/run_scenarios.py

# The harness smoke suite (writes evidence/phase3/)
PYTHONPATH=src .venv/bin/python recovery_audit/harness/smoke.py

# The two regression tests
PYTHONPATH=src .venv/bin/python -m pytest -q \
    tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py
```

`PYTHONPATH=src` is required. The worktree carries an editable-install `.pth`
pointing at the *main checkout's* `src`, which is a moving target - without the
override, tests silently exercise a different tree than the one being audited.

The scenario traces are deterministic: three consecutive runs of the suite
produce byte-identical evidence files.
