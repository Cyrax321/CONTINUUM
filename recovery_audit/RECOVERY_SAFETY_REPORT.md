# CONTINUUM Recovery-Safety Audit - Final Report

**Repository:** `https://github.com/Cyrax321/CONTINUUM.git` (audited via the checked-out fork at `c6e00411`, 91 commits behind upstream)
**Audit window:** 2026-10-11 → 2026-10-12
**Reference tree:** `/private/tmp/audit-recovery` (frozen detached worktree at `c6e00411` + the owner's pre-existing uncommitted work, as found)
**Acceptance criterion:** *zero unsafe duplicate external side effects* - if an external action may already have succeeded but its durable completion record is missing, recovery must not blindly execute it again.

---

## 1. The verdict

**The acceptance criterion is met on the audited tree, after two fixes. Both
were found by fault injection, reproduced by a deterministic regression test
before any fix was written, and verified to change nothing else.**

| Measure | Result |
|---|---|
| Scenarios run | 8 of 8 |
| UNSAFE before the fixes | 1 (S5a) |
| UNSAFE after the fixes | **0** |
| Runs that cannot be settled from durable state | 1 (S7), reported as `UNKNOWN_OUTCOME` |
| Judged assertions | 88 / 88 |
| Harness smoke checks | 46 PASS / 0 FAIL |
| `[FAIL]` labels anywhere in the evidence | 0 |
| Full suite before / after | 3359 tests / 80 failures → 3367 tests / 80 failures; **zero new, zero gone** |

Two high-severity correctness bugs were found and fixed in the reference
worktree. Neither was invented, assumed, or papered over: each has a failing
pre-fix evidence file and a passing post-fix one.

**I do not claim exactly-once execution, and neither does CONTINUUM.** The
library implements *at-least-once execution with mandatory reconciliation*. The
property this audit proves is narrower and provable: when the ledger cannot
demonstrate that an effect is settled, it refuses to perform it again and
escalates instead. Section 6 explains precisely why that is not exactly-once,
and why claiming otherwise would be dishonest.

---

## 2. What was actually traced

The audit traced the real code paths, not the documentation. In summary:

- **Submission → claim → effect → settle.** `ActionLedger.claim` folds the
  append-only event log to decide whether an identity is free, records
  `ACTION_RECORDED`, performs the effect, then records COMPLETED / FAILED /
  UNKNOWN. All state is derived by deterministic projection; the log is the
  sole source of truth.
- **The two crash windows.** Between the durable record and the effect (SEAM-1),
  and between the effect and the completion record (SEAM-2). These are the
  protocol's entire exposure surface, and every scenario is built on one of them.
- **Recovery.** `RecoveryEngine.assess` restores a projection, and its decision
  turns on the *status of the uncertain action*, not on the run's overall state.
  An in-flight slot blocks re-execution by construction.
- **Reconciliation.** Genuinely implemented, not decorative: a probe of the
  external system can settle an uncertain action and write the receipt back into
  the record. S2, S3, and S6 all depend on it working.
- **Concurrency.** `claim` is a read-then-write over the log, guarded - before
  this audit - by nothing atomic. Leases exist but are opt-in and off by default.

Full detail, with the load-bearing line numbers, is in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 3. Finding F1 - a duplicate external side effect (High)

**Component:** `continuum/actions/ledger.py`, `ActionLedger.claim`
**Scenario:** S5a, concurrent workers on one identity with no lease
**Pre-fix evidence:** [`evidence/phase5/F1_regression_before_fix.txt`](evidence/phase5/F1_regression_before_fix.txt)

### The defect

`claim` folds the log to decide a slot is free, then appends an
`ACTION_RECORDED` event. Those two steps were not atomic. With worker B's claim
interleaved between A's read and A's write - reached deterministically via
`InterleavingStorage`, with no threads and no timing - both workers folded an
empty slot, both appended, both received `fresh = True`, and **both performed the
side effect.** Two charges against one intent.

The storage layer already exposed the compare-and-set:
`append_event(expected_sequence=...)` raises `ConcurrentWriteError` when the log
head has moved since the caller last read it. `claim` simply never passed one.

### Why the naive fix is wrong

The obvious correction - pass `expected_sequence`, let the race loser see the
`ConcurrentWriteError` - is incorrect, and the maintainers' own test suite says
so. `test_storage_concurrency.py:228-229` documents the contract:

> If a future change makes claiming atomic in storage, the assertion below
> becomes `== 1`. That is the signal to tighten it, not to delete it.

That test catches `UnknownSideEffect`, not `ConcurrentWriteError`, because the
two failures mean the same thing to a caller: *another attempt owns this
identity and I cannot tell whether its effect landed.* Letting the storage
exception leak through the ledger's abstraction boundary broke it.

### The fix

`claim` now captures `storage.last_sequence()` before resolving, threads
`expected_sequence` through the three slot-opening `_record` writes, and on
`ConcurrentWriteError` re-folds and lets the resolution decide again. The
winner's STARTED record is visible on the re-fold, so the existing branch raises
`UnknownSideEffect` rather than opening a second slot. The loop is bounded
(`_CLAIM_CAS_ATTEMPTS = 8`), so a permanently contended run answers with a
`LedgerError` instead of spinning.

This is the codebase's own established idiom - the same pattern
`_append_projectable` uses (`mcp/server.py:318`): bound the retries, re-derive
the expected sequence each attempt, raise on exhaustion. The fix does not
introduce a new concurrency primitive; it uses the one the codebase already
trusts.

### Verification

[`tests/test_recovery_audit_tocou.py`](../tests/test_recovery_audit_tocou.py), 3
tests. Pre-fix: exit 1, both workers performed. Post-fix: exit 0, 3 passed.
S5a went from UNSAFE to SAFE with 7 judged assertions passing and 7 descriptive
outcomes recorded as informational.

The load-bearing invariant the test asserts is *at most one fresh slot*, not the
specific exception type - the loser may raise either `ConcurrentWriteError` or
`UnknownSideEffect`, both of which are safe refusals. Which worker wins is
scheduling-dependent and deliberately not scored.

---

## 4. Finding F2 - a poisoned log raised instead of degrading (High)

**Component:** `continuum/checkpoint/manager.py` and
`continuum/recovery/engine.py`
**Scenario:** S7, partial/corrupted durable state
**Pre-fix evidence:** [`evidence/phase5/F2_regression_before_fix.txt`](evidence/phase5/F2_regression_before_fix.txt)

### The defect

The engine's own contract, asserted at `engine.py:348-352`:

> Degrade, not raise (issue #383): the engine's whole job is to answer "where
> does this run stand", and a poisoned log is precisely the run that needs an
> answer.

That contract held only for an *unprojectable fold* - an event the parser can
see but cannot interpret. When the payload itself is unparseable, the storage
layer refuses it on **read**, raising `CorruptedRecord` before the fold's
`on_unprojectable="degrade"` machinery ever engages. `assess` propagated the
exception, so a run whose log was damaged could not be assessed at all -
precisely the run that most needs an answer.

Five auxiliary readers inside `assess` (the archive scan, two
`action_ledger.all()` calls, `collect_observations`, `build_informed_retry`) had
no degrade path either. Each was whack-a-mole: fix one, re-run, read the next
traceback.

### The trap the fix had to avoid

Suppressing those exceptions would have been **worse than the original crash**,
and this is the most important observation in the finding. The fold sees only
the clean prefix, so the projection over it looks *valid*. Left unmarked,
`assess` returned `mode = resume` over an unreadable log - advising an agent to
continue work from state that cannot be verified. A crash is loud; that would
have been a silent green light.

The fix therefore **marks** rather than silences. `_mark_unreadable` sets
`StateStatus.INVALID` with `unprojectable_at_sequence` naming the sequence
storage refused, and `_decide` (`engine.py:980`) already escalates on that input.

### The fix

1. `_read_for_restore` (new): under `degrade`, re-read with
   `upto = first_bad - 1` and return the readable prefix *together with* the
   sequence the storage refused. Under `raise`, the error propagates unchanged.
2. `_mark_unreadable` (new): marks the degraded state INVALID at the refusal
   point, with reason `"unreadable"`.
3. `assess` guards its five auxiliary readers, each degrading to empty / `None`.
   `_cursor_for` also falls back to the checkpoint's cursor when the annotation
   event is unreadable.

The `raise` path is deliberately untouched. The write path's default must still
fail loudly, which
[`test_restore_raise_still_fails_loudly_on_a_poisoned_log`](../tests/test_recovery_audit_degrade.py)
exists to guard - a fix that degrades unconditionally would let a write path
mistake a partial fold for good state.

### Verification

[`tests/test_recovery_audit_degrade.py`](../tests/test_recovery_audit_degrade.py),
5 tests. Pre-fix: exit 1, `CorruptedRecord` escaped `assess`. Post-fix: exit 0,
5 passed. `assess` now returns `mode = request_human` with rationale naming
sequence 2. S7 went from `FAILED TO RUN` to a clean `UNKNOWN_OUTCOME` with all 8
assertions passing.

---

## 5. What the eight scenarios proved

| # | Scenario | Seam | Verdict | Executions |
|---|---|---|---|---|
| S1 | Claim → crash → restart | SEAM-1, after claim | SAFE | 1 |
| S2 | Claim → effect → crash → restart | SEAM-2, after effect | SAFE | 1 |
| S3 | Completion record lost | Record destroyed post-write | SAFE | 1 |
| S4 | External action fails → retry | Failure mode (certain / uncertain) | SAFE | 2 |
| S5a | Concurrent workers, no lease | TOCTOU in `claim` | SAFE (**F1**) | 1 |
| S6 | Restored state pending | Stale checkpoint replayed forward | SAFE | 1 |
| S7 | Partial / corrupted state | Unparseable payload on read | **UNKNOWN_OUTCOME** (**F2**) | 1 |
| S8 | Duplicate submissions | Idempotency-key derivation ×4 | SAFE | 3 |

The three rows worth calling out:

**S3 is the strongest evidence.** The log actively disagrees with the external
system - the completion record is destroyed, the projection falls back to
STARTED - and the ledger still does not re-perform. It probes, reconciles the
receipt, and only then re-claims, at which point the slot reads COMPLETED and
nothing fires. This is the acceptance criterion's exact phrasing, tested
directly.

**S6 resolves a real tension in the right direction.** "Restore to an earlier
point" and "the effect may already have landed" are in direct conflict.
`restore` replays *forward* from the checkpoint without truncating the log, so
the in-flight `ACTION_RECORDED` event survives and keeps protecting the
identity. A truncating restore would have been a laundering bug; this is not.

**S7 is the one deliberately non-SAFE row, and that is the point.** A log the
storage cannot read is untrustworthy, and no amount of careful degradation
changes that. Scoring it SAFE would claim a guarantee the storage cannot
support. The honest answer is `request_human`.

Full assertion-level detail for every row is in
[`TEST_MATRIX.md`](TEST_MATRIX.md).

---

## 6. What I will not claim, and why

**Exactly-once execution is not claimed, not proved, and not implemented.**

The distinction matters and is easy to blur. In S2 the effect lands, the process
dies before the completion record is durable, and recovery *probes the external
system* to settle the question. That probe is the load-bearing mechanism - and a
system whose correctness depends on an external probe is at-least-once, not
exactly-once. It is *at-least-once done well*: the uncertain effect is
escalated, never blindly repeated.

Three limits are worth stating plainly, because each is a place a reader might
otherwise over-read the result:

1. **A probe is only as good as the external system's answer.** If the external
   system cannot say whether an effect landed, the ledger correctly refuses and
   escalates - but it cannot manufacture an answer. S7 is that case taken to its
   limit: the *durable* state is damaged, so no verdict is derivable at all.
2. **The audit covered the checked-out tree, which is 91 commits behind
   upstream.** Several conditions found here are already fixed upstream. The
   findings are about the audited tree, and `BASELINE.md` records the divergence
   explicitly. They are not asserted as current defects of the upstream project.
3. **A green suite is not a proof of universal semantics.** Eight scenarios
   cover the protocol's documented seams; they do not exhaust the space of
   failures. The evidence supports the narrow claim in section 1, not a
   general one.

The honest summary: CONTINUUM does not achieve exactly-once, and on the audited
tree it now does achieve what it actually promises - an uncertain effect is
never blindly repeated, and a run that cannot be settled is escalated rather
than silently resumed.

---

## 7. Method and evidence integrity

Every rule the audit operated under is reflected in the artifacts:

- **The baseline was recorded first and is reproducible.**
  [`BASELINE.md`](BASELINE.md) captures the tree state, the 80 pre-existing
  failures, and their classification. Notably, the uncommitted work was
  mid-refactor (`dependency` → `scope`) and *already failing*; that breakage is
  reported as pre-existing, not claimed as the audit's discovery.
- **Measurement was taken against a frozen worktree, not the shared checkout.**
  The main checkout is edited concurrently and carries an editable-install
  `.pth` pointing at its own `src`. Without `PYTHONPATH` pinned to the
  worktree, tests silently exercise a *different tree* than the one being
  audited. Every command in this report carries that override.
- **Each fix has a failing reproduction captured before it.** Rule 5, applied
  literally: `evidence/phase5/*_before_fix.txt` are the failing runs, and the
  fixes were written after reading them.
- **No existing test was modified to make the suite pass.** The 8 new tests are
  additive; the full suite went 3359 → 3367 tests with the failure set
  byte-identical by name.
- **No failure was hidden.** Three defects in my *own* evidence pipeline were
  caught and fixed rather than left: a scenario that asserted a success it had
  made impossible (S4's never-clearing failure), a naming heuristic for negated
  assertions that would have flipped a real duplicate into a pass, and
  descriptive race outcomes mislabelled `FAIL`. The last is why the matrix
  scores only *judged* assertions - `informational` outcomes are recorded, not
  scored. Zero `[FAIL]` labels remain, and each of those three fixes is recorded
  in [`PROGRESS.md`](PROGRESS.md).
- **Determinism.** Every fault is a counted crash point, an interleaved storage
   call, or a direct byte-level write. No sleeps, no retries-on-timing, no
   flaky schedules. Three consecutive suite runs produce byte-identical
   evidence.
- **No external side effects were performed.** The `ExternalWorld` is a
  deterministic fake that counts executions and mints receipts. No real
  payment, mail, or network call was made, and no production service was
  contacted.

---

## 8. Changes made

All production changes are in the reference worktree only. **Nothing has been
committed, pushed, or PR'd.** The owner's uncommitted work was not touched.

| File | Change | Reviewable size |
|---|---|---|
| `src/continuum/actions/ledger.py` | **F1**: `expected_sequence` threaded through the slot-opening writes; bounded re-fold on `ConcurrentWriteError` | 6 hunks |
| `src/continuum/checkpoint/manager.py` | **F2**: `_read_for_restore`, `_mark_unreadable`, guarded `_cursor_for` | 6 hunks |
| `src/continuum/recovery/engine.py` | **F2**: five auxiliary readers guarded against `CorruptedRecord` | 6 hunks |
| `tests/test_recovery_audit_tocou.py` | F1 regression, 3 tests (new) | - |
| `tests/test_recovery_audit_degrade.py` | F2 regression, 5 tests (new) | - |
| `recovery_audit/` | Audit artifacts: `BASELINE.md`, `ARCHITECTURE.md`, `HARNESS_DESIGN.md`, `TEST_MATRIX.md`, `PROGRESS.md`, this report, harness, scenarios, and evidence | - |

The `ledger.py` diff looks larger than it is: the retry loop wraps the existing
claim body, so most of the delta is a one-level re-indentation.
Whitespace-ignored, the three production files change roughly 250 lines
combined, and no public API, durable-state format, or recovery semantic was
altered.

---

## 9. Reproducing

```bash
cd /private/tmp/audit-recovery

# The 8 scenarios  -> evidence/phase4/
PYTHONPATH=src .venv/bin/python recovery_audit/scenarios/run_scenarios.py

# Harness smoke    -> evidence/phase3/
PYTHONPATH=src .venv/bin/python recovery_audit/harness/smoke.py

# Both regression suites
PYTHONPATH=src .venv/bin/python -m pytest -q \
    tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py

# Full suite (baseline comparison)
PYTHONPATH=src .venv/bin/python -m pytest -q --junitxml=/tmp/after.xml
```

Machine-readable evidence:
[`evidence/recovery_safety_evidence.json`](evidence/recovery_safety_evidence.json)
(31 KB) - audit metadata, the baseline with all 80 failing tests named, both
regression tests with pre/post-fix pointers, all 8 scenarios with every
assertion and its `expected_false` / `informational` tag, both findings, the
regression check, and the verdict.

---

## 10. Bottom line

The audit set out to answer one question: *if an external action may already
have succeeded, will recovery blindly do it again?*

Before the fixes, the answer was **"yes, under a claim race"** (F1), and
**"no, but a damaged log cannot be assessed at all"** (F2). After the fixes,
the answer is **"no - it refuses, escalates, and reconciles"**, with one
correctly unresolved case surfaced as `UNKNOWN_OUTCOME` rather than flattened
into a pass.

The acceptance criterion is met on the audited tree. Exactly-once is not
claimed, and the distinction between that and what is proved is recorded above
rather than blurred.
