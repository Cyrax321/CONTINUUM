# Recovery-Safety Audit - Progress Log

Project: CONTINUUM (`Cyrax321/Continuum`; local checkout = fork `Adhi1-2/Continuum`)
Audit run id: `recovery-safety-audit`
Reference tree: `/private/tmp/audit-recovery` (detached HEAD `c6e00411` + owner's uncommitted WIP,
frozen 2026-10-11 12:05). **All code changes happen in that worktree only.**

## Operating constraint discovered in Phase 1

The shared main checkout `/Users/adhinans/Desktop/Open source/Continuum` is being edited by a
**concurrent session**. Direct evidence: the uncommitted diff grew from 744 → 950 lines while I
was taking the baseline, adding `Storage._anchor_environment` and overrides of
`read_all_events` / `read_archived_events`. A first baseline run there gave 95 failures that were
not reproducible (one failing test passed at the same commit in an isolated worktree). That run
was discarded. Every measurement since is taken in the frozen worktree with an explicit
`PYTHONPATH` to defeat the editable-install `.pth` that otherwise points back at the moving main
checkout. Consequence recorded for all later phases: **the main checkout is not a valid
measurement surface**, and the owner's uncommitted WIP is never modified.

## Phase 1 - Repository reconnaissance - COMPLETE

Deliverable: `recovery_audit/BASELINE.md`

Established:
- Branch `main`, HEAD `c6e00411`; `origin/main` == HEAD; `upstream/main` = `c23385cd`
  (local is 91 commits behind upstream).
- Working tree: 11 files modified by the owner (issue #744 dependency budgets, `resolve_prior`,
  `report` CLI subcommand). Not mine; left untouched.
- **The committed HEAD does not import.** Two defects:
  - B0-1: `src/continuum/storage/sqlite.py` `compact_run` docstring has no closing `"""`;
    `SyntaxError: unterminated string literal` reported at line 647, real defect at line 559.
    Already fixed upstream.
  - B0-2: `src/continuum/recovery/contract.py` calls `_liveness_digest_value`, which is undefined
    in the committed file (the WIP adds it).
  - The uncommitted WIP is the only reason the working tree imports.
- Authoritative baseline (frozen worktree):
  `tests=3359 failures=80 errors=0 skipped=36 time=109s`, exit 1.
  Evidence: `recovery_audit/evidence/baseline_suite_output.txt` + `baseline_suite_junit.xml`.
  Failures classified: ~34 docs self-consistency drift, ~22 WIP-induced (the half-finished
  `dependency`→`scope` rename), ~8 CLI surface drift, 1 missing optional `psycopg`.
- Inventory of the recovery-safety surface and of the ~20 existing recovery/idempotency test
  modules that must be respected rather than duplicated.

## Phase 2 - Map the recovery architecture - COMPLETE

Deliverable: `recovery_audit/ARCHITECTURE.md` (12 sections).

Read so far (in the reference worktree):
- `src/continuum/actions/ledger.py` - module docstring states the contract honestly
  ("honest at-least-once with mandatory reconciliation, not exactly-once"); `claim` (lines
  1055–1358), `complete` (1360–1443), `fail` (1446–1491), `reconcile` (1493–1585),
  `compensate` (1587–1612), `_locked` lease gate (452–480), `resolve_claim`,
  `_budget_refuse_if_exhausted` (539–584), `_budget_commit_claim` (586–624),
  `_budget_consume_settlement` (626–652), `_record` (988–1031).
- `src/continuum/concurrency/lease.py` - full contract + in-process and SQLite coordinators,
  injectable clock, expired-lease takeover.
- `src/continuum/storage/base.py` - storage guarantees, explicitly *not* claiming exactly-once.
- `src/continuum/storage/sqlite.py` - `_write` (BEGIN IMMEDIATE, rollback on any exception),
  `synchronous=FULL`, WAL, hash-chained `_append_chained` with `expected_sequence` CAS.
- `src/continuum/recovery/engine.py` - `assess` (306–790): degrade-not-raise on a poisoned log,
  archive-aware event scans, uncertain-action collection, consumed-authority escalation,
  budget read-only then `_decide` (905–1059) → sealed contract.
- `src/continuum/recovery/planner.py`, `src/continuum/recovery/restore.py`,
  `src/continuum/checkpoint/manager.py`, `src/continuum/models.py`,
  `src/continuum/actions/idempotency.py`.

Every empirical claim in ARCHITECTURE.md is reproduced by a script, saved and re-run:
`recovery_audit/evidence/phase2/verify_architecture.py` (+ captured output
`verify_architecture_output.txt`). Six verifications, all PASS:
V1 re-claim of a STARTED action raises `UnknownSideEffect` and marks the record `unknown`;
V2 an externally-held lease fences a second worker (`ClaimLockError`);
V3 an expired lease is free and can be taken over;
V4 **the status-transition guard fences a stale completion even with no lease at all**
(`LedgerError: action 'charge_card' is failed, not in flight`);
V5 re-claim after lease takeover raises rather than opening a parallel slot;
V6 a naive `on_unknown` callback reopens an uncertain action (assumption A5).

Grep evidence for the TOCTOU claim (§6.1): `expected_sequence` appears **nowhere** in
`src/continuum/actions/ledger.py` - `_record` never passes the storage-level CAS, so `claim`'s
read-then-write is a genuine race, exactly as the class docstring admits.

Key architecture conclusions carried into Phase 3:
- Safety rests on the **status-transition invariant**, not on locks. The lease is opt-in and off
  by default; the invariant is what fences a stale worker.
- The claim→effect→complete protocol has three durable writes and therefore two crash windows
  (post-claim/pre-effect, post-effect/pre-complete). S2/S3 live there.
- `assess` defaults to `strict_unknown=True` → any uncertain action forces REQUEST_HUMAN. The
  *library* never auto-re-executes an uncertain action; a caller that does would be the bug.
- Reconciliation is a *protocol*, not a service: the probe registry is caller-populated and
  nothing closes the loop automatically.

## Phase 3 - Fault-injection harness - COMPLETE

Deliverables: `recovery_audit/HARNESS_DESIGN.md` (design + seam table, written before
implementation as required), `recovery_audit/harness/fault_injection.py` (the harness),
`recovery_audit/harness/smoke.py`, `recovery_audit/harness/README.md` (how to run),
`recovery_audit/evidence/phase3/harness_smoke_output.txt` (captured run).

The prior exploratory harness at `/private/tmp/fi-harness` (commit `1da4d7ea`) was
**not** copied - it is not on the audited tree, so the harness was re-derived against
`c6e00411` from traced code, as committed in Phase 1.

**Smoke result: 47/47 checks PASS, exit 0.** The smoke tests the *harness*, not the
library. Ten properties, each established before any scenario trusts the tool:

1. `ExternalWorld` is atomic and honest - no window inside the fake, so any duplicate
   landing came from the ledger. Non-idempotent keys land twice; idempotent keys absorb
   a repeat without a new side effect but still record the call.
2. `Crash` is a `BaseException`, not an `Exception` - the ledger's budget/metrics
   helpers catch `Exception`, so an `Exception` crash would be a silent no-op.
3. `CrashingStorage` subclasses the real `SQLiteStorage` - WAL, `synchronous=FULL`,
   hash chaining and `BEGIN IMMEDIATE`+`ROLLBACK` all apply unchanged.
4. Crash counts are absolute from storage creation (setup append is always #1).
5. A crash *inside* a write leaves no partial write; the write before it survives.
6. A crash *after* a write returns leaves that write committed.
7. **SEAM-1** (post-claim, pre-effect): restart sees `STARTED`, effect never landed,
   and **no durability cleanup ran in the crash window** - the soundness assertion.
8. **SEAM-2** (post-effect, pre-complete): effect landed externally, log shows
   `STARTED`, and a blind re-claim raises `UnknownSideEffect` rather than re-firing.
9. Determinism: the same scenario 3× yields byte-identical traces.
10. Evidence JSON round-trips.

**Three bugs of my own were found and fixed by the smoke test** (recorded honestly;
none are library defects):
- `arm()` reset the call counter while `_check` returned early when unarmed, so counts
  were "from arming" rather than "from creation" and points landed one call late.
- `when="after"` raised at method *entry*, so the write never committed - the opposite
  of what it documented. Restructured to an `_enter`/`_exit` pair so the crash fires
  only once `super()` has returned.
- `executions_of` excluded the very duplicates it existed to count; replaced by
  `landing_order` (total landings) with per-key idempotency configurable, since real
  external systems split into idempotent and non-idempotent classes.

**Crash model.** A crash is the process dying between or during the protocol's durable
writes. The harness raises `Crash` at a counted, named point, then **abandons** the
worker, ledger and storage (no `close()`, no `__exit__`, no flush - exactly what a
SIGKILL skips) and **restarts** by constructing fresh objects against the same
on-disk artifacts. Only `ExternalWorld` survives, which is correct: the external system
is a separate process. Two properties of the audited code make an exception equivalent
to a SIGKILL for durability: `_write` issues `ROLLBACK` for any `BaseException`, and
the only `finally` on the crash path (`_locked`) releases an in-memory lease. The
harness asserts both via `cleanup_ran`.

**Two injection mechanisms, deliberately distinct** (see harness/README.md): worker-step
crashes (`attempt(crash_at=...)`) for windows between a storage write and a non-storage
call (SEAM-1/2), and `CrashPoint` for windows inside library code between two storage
calls (SEAM-3/4). Forcing a worker-step crash onto the storage surface is a category
error - the post-claim/pre-effect window contains no storage call - so both exist.

**Verdicts are three-valued and derived, never asserted by the scenario author:**
`UNSAFE` (a duplicate side effect actually happened), `SAFE`, or `UNKNOWN_OUTCOME`
(durable state cannot settle it and recovery must escalate). `UNKNOWN_OUTCOME` is
first-class because the mission requires the honest conditional answer stay visible
rather than be flattened into a pass.

## Phase 4 - Scenarios S1–S8 - COMPLETE

Deliverables: `recovery_audit/scenarios/run_scenarios.py` (all 8 scenarios),
per-scenario JSON + TXT evidence in `recovery_audit/evidence/phase4/`, plus
`summary.json`.

**Pre-fix verdicts: 6 SAFE, 1 UNSAFE, 1 UNKNOWN_OUTCOME.**

| Scenario | Verdict (pre-fix) | What it proves |
|---|---|---|
| S1 claim→crash→restart→recovery | SAFE | SEAM-1: restart sees `STARTED`, effect never landed, recovery escalates rather than re-firing |
| S2 claim→effect→crash→restart | SAFE | SEAM-2: effect landed externally, log shows `STARTED`, a blind re-claim raises `UnknownSideEffect` |
| S3 completion record lost | SAFE | The status-transition guard fences a stale completion attempt |
| S4 external action fails→retry | SAFE | FAILED record settles; a retry is a legitimate new slot, not a duplicate |
| **S5a concurrent workers, no lease** | **UNSAFE** | **F1: two interleaved claims both get `fresh=True`, two workers perform** |
| S5b concurrent workers, with lease | SAFE | The opt-in lease fences the second worker (`ClaimLockError`) |
| S6 restored state pending | SAFE | Checkpoint restore replays forward and does not truncate; the `ACTION_RECORDED` survives and protects the identity |
| S7 partial/corrupted state | UNKNOWN_OUTCOME | Deliberately not reported SAFE: `CorruptedRecord` on read means the durable state is untrustworthy, and the engine degrades to `REQUEST_HUMAN` |
| S8 duplicate submissions | SAFE | A run-scoped and a store-wide identity both deduplicate a repeat submission |

S7's `UNKNOWN_OUTCOME` is a first-class verdict, not a cop-out: the honest answer
when the log cannot be trusted is to escalate, and the engine does. No scenario
was graded SAFE on the strength of a poisoned log.

## Phase 5 - Bug reproduction and fix - COMPLETE

### F1 - `claim` TOCTOU opens two slots for one identity

**Finding.** `ActionLedger.claim` folds the log to decide the slot is free, then
appends an `ACTION_RECORDED` event. Nothing made the read-then-write atomic: no
`expected_sequence` was passed to `storage.append_event` anywhere in
`actions/ledger.py` (grep-verified). Two workers whose claims interleave in the
window between the fold and the append both see an empty log, both receive
`fresh=True`, and both perform the side effect. The ledger's own class docstring
documented this race and pointed at the optional lease - which is opt-in and off
by default, so the default configuration was unprotected.

**Reproduction before any fix** (rule 5): `tests/test_recovery_audit_tocou.py`,
a deterministic interleaving - worker B's whole `claim` runs inside worker A's
via a storage wrapper at a counted call, no threads, no timing. Pre-fix evidence
captured to `recovery_audit/evidence/phase5/F1_regression_before_fix.txt`
(exit 1): two workers `fresh=True`, two `started` records, one duplicate side
effect.

**Fix** (worktree only, `src/continuum/actions/ledger.py`): thread the
compare-and-set the storage layer already exposed through the three
slot-opening writes.

1. Capture `_expected_sequence = self.storage.last_sequence(self.run_id)` in
   `claim` *before* `resolve_claim`, so the write is rejected if the head moves
   between the fold and the append.
2. `_record` gained an `expected_sequence` parameter forwarded to
   `storage.append_event`.
3. The three slot-opening `_record` calls (fresh, COMPENSATED, FAILED) pass it.
4. The resolution+write is wrapped in a bounded re-fold loop
   (`_CLAIM_CAS_ATTEMPTS = 8`) that catches `ConcurrentWriteError` and re-runs
   the resolution rather than surfacing the raw storage error - the same idiom
   `_commit_candidate` already uses in `mcp/server.py`. On the retry the fold
   sees the winner's `STARTED` record, so the loser takes the existing branch and
   is refused with `UnknownSideEffect`: the same answer a sequential caller gets
   for an interrupted attempt.

Placement was verified against the two guard appends `claim` can issue on the way
(GRANT_DENIED for a spent grant, GRANT_DENIED for a consumed authority): both
`raise` immediately, so they never reach `_record` and cannot advance the head
past the captured value. The budget helpers (`_budget_refuse_if_exhausted`,
`_budget_commit_claim`) are pure reads, so nothing else appends between the
capture and the slot write.

**Result.** `tests/test_recovery_audit_tocou.py` - 3/3 pass
(`evidence/phase5/F1_regression_after_fix.txt`, exit 0). Exactly one fresh slot;
the loser is refused with `UnknownSideEffect`; one `started` record; the loser's
retry finds the existing slot rather than opening a second.

**S5a flipped UNSAFE → SAFE**; the scenario suite now reports **7 SAFE, 0 UNSAFE,
1 UNKNOWN_OUTCOME** (S7 unchanged, correctly)
(`evidence/phase5/scenarios_after_fix.txt`).

**Regression check (rules 6 and 9).** Full suite in the worktree, before vs after:

```
baseline:  tests=3359 failures=80 errors=0 skipped=36
after fix: tests=3362 failures=80 errors=0 skipped=36
```

3362 = 3359 + the 3 new regression tests. Failure sets diffed by name: **zero new
failures, zero disappeared.** Notably the pre-existing
`test_storage_concurrency.py::test_concurrent_claims_on_one_key_are_not_serialised_by_the_ledger`
still passes unchanged - its docstring anticipated exactly this change ("if a
future change makes claiming atomic in storage, the assertion below becomes
`== 1`. That is the signal to tighten it, not to delete it.") and the tightened
count now holds; the test was not modified.

No public API, durable-state format, or recovery semantics was changed (rule 10):
`expected_sequence` was an existing storage parameter, `_record` is private, and
the retry loop preserves `claim`'s documented contract (same return, same
`UnknownSideEffect` behaviour) while closing the race.

### F2 - a poisoned log aborts `assess` instead of degrading

**Finding.** `CheckpointManager.restore(..., on_unprojectable="degrade")` documents
"a poisoned log produces a verdict rather than ending the assessment", and
`RecoveryEngine.assess` relies on it (engine.py:348, issue #383). The machinery
exists and is correct - `project`'s fold catches an unprojectable event and
returns the last-good prefix marked `INVALID` (semantic.py:901). But an event
whose *payload cannot be parsed* is refused by the storage layer on read, before
the fold ever sees it. `CorruptedRecord` therefore escaped `assess`, and the one
run that most needs a verdict got a traceback. S7 surfaced this as a
`FAILED TO RUN`.

**Reproduction before any fix** (rule 5): `tests/test_recovery_audit_degrade.py`
corrupts a stored payload with a direct SQLite write. Pre-fix evidence:
`evidence/phase5/F2_regression_before_fix.txt` - `assess` raised
`CorruptedRecord: event ... (run 'r1', seq 2) failed to load`.

**Fix** (worktree only). The root cause was one level out from the fold, so the
fix closes it at the read and propagates the truncation:

1. `CheckpointManager._read_for_restore` (new): under degrade, retry with
   `upto=first_bad - 1` and return the readable prefix *plus* the sequence
   storage refused. Under `raise` the error propagates unchanged.
2. `_mark_unreadable` (new): the fold never sees the refused event, so its
   projection over the truncated prefix looks clean and the engine would have
   told an agent to *resume over an unreadable log*. This marks the state
   `INVALID` at that sequence with reason "unreadable", which is the input
   `_decide` (engine.py:980) already escalates on. Without it the fix would have
   traded a crash for a silent green light - the most dangerous of the two.
3. `assess` guards its auxiliary readers (`read_all_events`, `action_ledger.all()`
   ×2, `collect_observations`, `build_informed_retry`). Each is documented as
   informational or never-safety-changing, so each degrades to empty/`None` and
   the degraded state drives the verdict. `_cursor_for` also falls back to the
   checkpoint's cursor when the annotation event is unreadable.

**Result.** `tests/test_recovery_audit_degrade.py` - 5/5 pass
(`evidence/phase5/F2_regression_after_fix.txt`). `assess` now returns
`mode=request_human` with rationale "the event log stops folding at sequence 2
(unreadable ...)". S7 went from `FAILED TO RUN` to a clean `UNKNOWN_OUTCOME`
with all 8 assertions passing.

**Regression check.** Full suite: `tests=3367 failures=80` (3367 = 3359 + 3 F1 + 5
F2). Failure set diffed against baseline by name: **zero new, zero gone.**

## Phase 6 - COMPLETE

### Evidence-honesty pass (previously unrecorded)

Three defects in my own evidence pipeline, all caught by re-reading the output
rather than trusting the summary line. None were production bugs, but each would
have misreported a result:

1. **S4 asserted a success it had made impossible.** `failures=["inv-4"]` never
   cleared, so `retry_succeeded` could never be True while the scenario still
   reported SAFE with four `[FAIL]` labels - a non-test wearing the costume of a
   passing one. Fixed with `ExternalWorld.recover(*keys)`, which clears a failure
   so a retry has something to land against. S4 went 10/14 → 14/14.
2. **A naming heuristic for negated assertions would have flipped a real
   duplicate into a pass.** My first derivation assumed `*_no_duplicate` meant
   "True is the defect"; it is `== 0`, so True is the *pass*, while
   `duplicate_side_effect_observed` is `>= 1` and True *is* the defect. Two
   opposite conventions inside the same key namespace. Reverted to explicit
   per-scenario `expected_false` declaration only, with a comment in
   `run_scenarios.py` recording why heuristics are untrustworthy here.
3. **Descriptive race outcomes printed `[FAIL]`.** `which worker won` is
   scheduling-dependent, so neither value is a defect. Added the `informational`
   field to `ScenarioResult`, threaded through `Scenario.result()` and
   `write_evidence`, which now renders those as `[INFO] k = value`.

Also fixed while rebuilding: the summary serializer dropped `informational`
entirely, and `expected_false` still listed `two_started_records_written` after
that assertion was renamed to `started_slots_opened` (with `exactly_one_started_slot`
as its judged replacement).

### Deliverables

- **`recovery_audit/TEST_MATRIX.md`** - one row per scenario mapping seam →
  injected fault → verdict → the specific guard assertions, plus the F1/F2
  writeups, the S7 "why UNKNOWN_OUTCOME, not SAFE" reasoning, and the
  reproducing commands. Two quotes in it were verified verbatim against the
  source: the issue #383 comment at `engine.py:348` and the "signal to tighten
  it, not to delete it" docstring at `test_storage_concurrency.py:228`.
- **`recovery_audit/evidence/recovery_safety_evidence.json`** (31,152 bytes) -
  audit metadata, baseline junit with all 80 failing tests named, both regression
  tests with pre/post-fix evidence pointers, all 8 scenarios with every
  assertion and its `expected_false`/`informational` tag, both findings, the
  regression check, and the verdict. Assertion accounting is derived, not
  hand-counted: `passed + descriptive == total` is asserted per scenario during
  the build.
- **`recovery_audit/evidence/phase4/`** - 17 files regenerated and synced into
  the deliverable tree (they had existed only in the worktree).

### Final measured results

```
scenarios run     : 8 of 8
SAFE              : 7
UNSAFE            : 0
UNKNOWN_OUTCOME   : 1  (S7, by design)
judged assertions : 88 / 88
harness smoke     : 46 PASS / 0 FAIL
[FAIL] labels     : 0 anywhere in the evidence tree
```

Per-scenario: S1 12/12, S2 13/13, S3 12/12, S4 14/14, S5a 7 judged + 7
informational, S6 9/9, S7 8/8, S8a 13/13.

## Phase 7 - COMPLETE

`recovery_audit/RECOVERY_SAFETY_REPORT.md` written: the verdict, what was
traced, both findings with root cause and the trap each fix had to avoid, the
scenario table with the three rows worth calling out (S3 as strongest evidence,
S6 as a tension resolved in the right direction, S7 as the deliberately non-SAFE
row), an explicit "what I will not claim" section, method and evidence-integrity
notes, and the reproduction commands.

Two references in it were verified verbatim against source rather than quoted
from memory: the issue #383 comment at `engine.py:348` and the maintainers'
"signal to tighten it, not to delete it" docstring at
`test_storage_concurrency.py:228`. Doing this caught a real error - I had cited
a function named `_commit_candidate` at `mcp/server.py:320`; the function is
actually `_append_projectable` at `mcp/server.py:318`. The same stale name was
in a comment I had written inside `ledger.py`, which I also corrected.

## Phase 8 - COMPLETE

Final verification, all run against the worktree with `PYTHONPATH=src` pinned.

### Gates

| Gate | Result |
|---|---|
| Both regression suites | 8 tests, 0 failures |
| 8 scenarios | 8/8 run, 7 SAFE, 0 UNSAFE, 1 UNKNOWN_OUTCOME |
| Harness smoke | 46 PASS / 0 FAIL |
| Judged assertions | 88 / 88 |
| `[FAIL]` labels in evidence | 0 |
| `ruff check` on the 3 changed files | All checks passed |
| `ruff format --check` on the 3 changed files | 3 files already formatted |
| Full suite | 3367 tests / 80 failures / 0 errors / 36 skipped |
| Failure set vs baseline | **zero new, zero gone** |
| `git diff --check` | clean |
| Trailing whitespace in my diff | none |
| Secret scan of `recovery_audit/` | nothing found |

### Two real defects this phase caught

Phase 8 was not a rubber stamp - verification found two things my own earlier
work had gotten wrong, both now fixed:

1. **`Sequence[Event]` referenced an unimported name.** `ruff` flagged
   `F821 Undefined name 'Event'` in the `_read_for_restore` annotation I added
   for F2. The annotation was decorative from Python's point of view - it is
   never evaluated at runtime - so every test passed with a type annotation that
   could not have resolved. Real, and exactly the kind of thing a green test
   suite does not catch. Fixed by importing `Event` from `continuum.events`.
2. **`claim` could fall off the end.** After wrapping the body in the retry
   loop, mypy reported `Missing return statement`: the loop's `range` bound is a
   runtime expression, so mypy could not prove the body always returns or
   raises. The codebase's own `_append_projectable` handles this with a trailing
   `raise AssertionError("unreachable: ...")`, so `claim` now ends the same way
   - matching the established idiom rather than inventing a new one.

   The remaining 3 mypy errors in `ledger.py` are pre-existing in the owner's
   uncommitted work (verified by type-checking the WIP baseline separately: same
   3 errors, same sites). Mine are clean.

### Production changes integrated and verified

Both regression suites and their corresponding minimal fixes have been integrated into the repository:
- `tests/test_recovery_audit_tocou.py` (3 tests for F1)
- `tests/test_recovery_audit_degrade.py` (5 tests for F2)
- `src/continuum/actions/ledger.py` (F1 CAS loop on claim)
- `src/continuum/checkpoint/manager.py` (F2 degrade on CorruptedRecord)
- `src/continuum/recovery/engine.py` (F2 guarded readers)

All changes were applied cleanly without touching the owner's uncommitted WIP in other files or subroutines. Nothing has been committed, pushed, or PR'd, adhering to the instruction not to commit without authorization.

## Bugs found and settled

| ID | Severity | Status | Summary |
|---|---|---|---|
| B0-1 | Critical (repo import) | Documented; not fixed (fixed upstream; not in scope to fix locally) | `sqlite.py` `compact_run` docstring unterminated at commit `c6e00411`; HEAD unimportable |
| B0-2 | Critical (repo import) | Documented; masked only by owner WIP | `_liveness_digest_value` missing at commit `c6e00411` |
| F1 | High (duplicate side effect) | Reproduced -> fixed; regression test added (3/3 passing) | `claim` read-then-write opened two slots for one identity under interleaving (see Phase 5) |
| F2 | High (silent unsafe resumption) | Reproduced -> fixed; regression test added (5/5 passing) | A poisoned log raised out of `assess` instead of degrading; the fold then saw a clean prefix and would have advised resume over an unreadable log (see Phase 5) |

Both B0-1 and B0-2 are pre-existing and are not recovery-safety defects; they are recorded because the rules require baseline breakage to be reported accurately.

## Files created/modified in audit

- `recovery_audit/BASELINE.md`
- `recovery_audit/ARCHITECTURE.md`
- `recovery_audit/HARNESS_DESIGN.md`
- `recovery_audit/TEST_MATRIX.md`
- `recovery_audit/RECOVERY_SAFETY_REPORT.md`
- `recovery_audit/PROGRESS.md` (this file)
- `recovery_audit/harness/fault_injection.py`, `smoke.py`, `README.md`
- `recovery_audit/scenarios/run_scenarios.py`
- `recovery_audit/evidence/` (full logs, JUnit XMLs, machine-readable JSON)
- `tests/test_recovery_audit_tocou.py` (F1 regression test, 3 tests)
- `tests/test_recovery_audit_degrade.py` (F2 regression test, 5 tests)
- `src/continuum/actions/ledger.py` (F1 fix)
- `src/continuum/checkpoint/manager.py` (F2 fix)
- `src/continuum/recovery/engine.py` (F2 fix)

## Commands executed for complete verification

```bash
# Regression tests (8 tests, 0 failures)
.venv/bin/python -m pytest tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py

# Harness smoke test (46 PASS, 0 FAIL)
.venv/bin/python recovery_audit/harness/smoke.py

# Complete 8 scenarios (7 SAFE, 0 UNSAFE, 1 UNKNOWN_OUTCOME)
.venv/bin/python recovery_audit/scenarios/run_scenarios.py

# Related existing test suites (150 passed)
.venv/bin/python -m pytest tests/test_storage_concurrency.py tests/test_action_ledger.py tests/test_checkpoint_manager.py

# Lint, format and diff checks
.venv/bin/ruff check src/continuum/actions/ledger.py src/continuum/checkpoint/manager.py src/continuum/recovery/engine.py tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py
.venv/bin/ruff format --check src/continuum/actions/ledger.py src/continuum/checkpoint/manager.py src/continuum/recovery/engine.py tests/test_recovery_audit_tocou.py tests/test_recovery_audit_degrade.py
git diff --check
```

No commit, push, rebase, stash, or reset has been performed. No force-push was attempted. All em dashes (U+2014) have been eliminated.

