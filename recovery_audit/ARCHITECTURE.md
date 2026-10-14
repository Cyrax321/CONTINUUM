# Phase 2 - The Recovery Architecture of CONTINUUM

Reference tree: `/private/tmp/audit-recovery` (detached HEAD `c6e00411` + owner's uncommitted WIP).
All line numbers below are from that tree. Where the uncommitted WIP changes a line number
relative to the commit, it is noted.

This document is derived by reading the code and call chains, then **empirically verifying** the
behaviours claimed. Every "verified" statement below was executed as a script against the
reference tree; the scripts are reproduced in `recovery_audit/evidence/` where applicable.

---

## 1. What CONTINUUM actually is

CONTINUUM is a **durable-execution / recovery layer for long-running AI agents**, not a
general-purpose workflow engine. Its persistence model is an append-only, hash-chained event log
per run, from which all state is derived by deterministic projection. There is no mutable
per-action row store; an "action record" is the *fold* of every `ACTION_RECORDED` /
`ACTION_RECONCILED` / `ACTION_COMPENSATED` event for a key, last-write-wins
(`fold_action_events`, `src/continuum/actions/ledger.py:87-102`).

The design is explicitly and repeatedly documented as **at-least-once with mandatory
reconciliation, not exactly-once**. From the action-ledger module docstring
(`src/continuum/actions/ledger.py:36-39`):

> This is honest at-least-once with mandatory reconciliation, not exactly-once.
> The distinction is documented rather than marketed away.

And from the storage contract (`src/continuum/storage/base.py:20-24`):

> **Not exactly-once.** A crash between an external side effect and its ledger
> write leaves the ledger behind reality. That gap is what the action ledger
> (Phase 6) reconciles; storage cannot close it alone.

This is the correct framing and it matters for the whole audit: the acceptance criterion
("zero unsafe duplicate external side effects") is satisfied *only if* the reconcile-before-replay
discipline is actually enforced on every path. The audit therefore focuses on enforcement, not on
the impossible goal of closing the gap.

---

## 2. Action lifecycle: submission to durable completion

### 2.1 The three-write protocol

```
key = ledger.claim(...)        # (1) write intent   -> event ACTION_RECORDED, status STARTED
result = do_the_thing()        # (2) perform the external effect (NOT durable, NOT atomic with 1 or 3)
ledger.complete(key, ...)      # (3) record outcome -> event ACTION_RECORDED, status COMPLETED
```

(`src/continuum/actions/ledger.py:11-15`, restated in the module docstring.)

The crash windows the ledger itself names (`ledger.py:17-26`):

| Crash point | Durable state | External effect | Recovery reading |
|---|---|---|---|
| before (1) | nothing | did not run | retry is safe |
| between (1) and (2) | `STARTED` | **may or may not have run** | **unknown** - the ledger refuses to guess |
| between (2) and (3) | `STARTED` | **definitely ran** | indistinguishable from the row above *from the ledger alone* |
| after (3) | `COMPLETED` | ran | repeat call returns the stored result |

The indistinguishability of rows 2 and 3 is the load-bearing insight of the entire design. It is
why the safe response to a `STARTED` record on recovery is *never* "replay it".

### 2.2 Step (1) - `claim` (`src/continuum/actions/ledger.py:1055-1358`)

`claim` resolves the identity first (`resolve_claim`, `ledger.py:732`), then:

1. **Foreign-run check** for unscoped (`scoped_to_run=False`) keys: a `COMPLETED` record in
   *another* run is returned as the answer (`ledger.py:1105-1111`); a `STARTED`/`UNKNOWN` foreign
   record **raises `UnknownSideEffect`** rather than opening a parallel slot (`ledger.py:1109-1117`).
2. **Grant / authority resurrection guards** - a consumed single-use authority is refused with
   `GrantDenied` before anything is recorded (`ledger.py:1132-1158`, scanning both `GRANT_DENIED`
   history and `AUTHORITY_CONSUMED` events). Value-based, shape-agnostic, so a token nested in
   `{"payment": {"auth_token": ...}}` is still caught (`ledger.py:1123-1131`).
3. **Budget gate** - an over-cap attempt is refused *before* the record is written
   (`ledger.py:1248-1250`, `_budget_refuse_if_exhausted`). The counter is advanced only *after*
   the event is durable (`_budget_commit_claim`, `ledger.py:599-624`), so a refused claim
   consumes no slot. This ordering is deliberate and documented (issue #1168).
4. Records `Action(status=STARTED)` as an `ACTION_RECORDED` event.

The branch that matters most for this audit (`ledger.py:1323-1358`):

```python
# STARTED or UNKNOWN: a previous attempt was interrupted.
if on_unknown is not None:
    resolved = on_unknown(existing)
    if resolved is not None:
        self._record(resolved.key, resolved.action, EventType.ACTION_RECONCILED)
        return resolved

uncertain = existing.model_copy(
    update={"status": ActionStatus.UNKNOWN, "side_effect_uncertain": True}
)
self._record(key, uncertain)
raise UnknownSideEffect(...)
```

**Verified empirically.** Re-claiming an action that is still `STARTED` raises
`UnknownSideEffect`, flips the record to `UNKNOWN` with `side_effect_uncertain=True`, and carries
`action_key`/`action_id` on the exception so the caller knows *what* to reconcile. Output of the
verification script:

```
1st claim fresh: True status: started
2nd claim raised UnknownSideEffect: action 'send_email' (key 46d211bba6f4...) was interrupted
   action_key: 46d211bba6f4 action_id: action_e3f14
pending: [('send_email', 'unknown')]
```

### 2.3 Step (3) - settlement (`complete` / `fail` / `reconcile` / `compensate`)

All four are guarded by **status-transition invariants**, and this is the second load-bearing
safety mechanism. Each method refuses to settle an action that is not in a state it may settle:

| Method | Accepts pre-state | Refuses |
|---|---|---|
| `complete` (`ledger.py:1360`) | `STARTED`, `COMPLETED` | everything else - explicitly including `UNKNOWN` |
| `fail` (`ledger.py:1446`) | `STARTED`, `FAILED` | everything else, including `UNKNOWN` |
| `reconcile` (`ledger.py:1493`) | **any** - it is the evidence-bearing path | nothing |
| `compensate` (`ledger.py:1587`) | `COMPLETED`, `COMPENSATED` | `STARTED`/`UNKNOWN` - "no verified outcome to undo" |

The refusals are not pedantry; they are fencing. `complete`'s docstring (`ledger.py:1321-1334`)
explains that completing an `UNKNOWN` action "erased the recovery blocker, wrote no note, and left
an `ACTION_RECORDED` event indistinguishable from an ordinary first-time success. An auditor could
not tell that an uncertain charge had been resolved by assertion."

`reconcile` is the **only** sanctioned route for resolving an uncertain outcome, and it always
writes `ACTION_RECONCILED` with a caller-supplied note, so the correction is visible in the log.

**Verified empirically - fencing works even with no lease at all.** A stale worker whose action
was reconciled to `FAILED` cannot revive it via `complete`:

```
A claimed (no lease): True
B reconciled not-occurred -> status: failed
A's late complete blocked by LedgerError: action 'charge_card' is failed, not in flight, so
completing it would assert an outcome nothing has verified.
```

This is important: the lease is *not* the only fence. The status-transition guard is the primary
fence, and it holds in the default (leaseless) configuration.

### 2.4 Crash detection and restart

There is no process supervisor inside the library. "Crash detection" is: on the next start, a new
`SQLiteStorage` handle is opened on the same file, and the recovery engine *derives* what
happened from the log. A crash is simply the absence of the settlement event.

Restart is therefore a fresh construction, which is exactly what makes it testable
deterministically: `SQLiteStorage(path)` on an existing file re-reads the committed log. WAL mode
with `PRAGMA synchronous=FULL` (`sqlite.py:168-172`) means a committed event survives process
death; an uncommitted transaction is rolled back by `_write` (`sqlite.py:205-216`, `BEGIN
IMMEDIATE` + explicit `ROLLBACK` on any exception).

---

## 3. Durable states and allowed transitions

From `ActionStatus` (`src/continuum/models.py:130-141`) plus the guards in §2.3.

```
                          ┌──────────────────────────────────────────┐
                          │            (action submitted)            │
                          └────────────────────┬─────────────────────┘
                                                 │  claim() writes ACTION_RECORDED
                                                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                          STARTED                             │
   │  intent is durable; the effect may or may not have happened  │
   └──┬──────────────┬───────────────┬───────────────┬───────────┬──┘
      │ complete     │ fail(certain) │ fail(certain= │ re-claim  │ reconcile(occurred=?)
      │              │               │  False)       │ by anyone │  (evidence required)
      ▼              ▼               ▼               ▼           ▼
  ┌────────┐   ┌──────────┐   ┌──────────────┐  ┌─────────┐  ┌──────────┐
  │COMPLETED│  │  FAILED  │   │   UNKNOWN    │  │ UNKNOWN │  │COMPLETED │
  └───┬────┘  └────┬─────┘   │ side_effect_ │  └────┬────┘  │ or FAILED│
      │            │         │ uncertain=1  │       │       └──────────┘
      │ compensate │ re-claim│ └──────┬──────┘       │ (raises UnknownSideEffect,
      ▼            │ (fresh) │        │              │  does NOT open a slot)
  ┌────────────┐   │        │        │ reconcile    │
  │COMPENSATED │◄──┘        │        └──────────────┘
  └─────┬──────┘            │ re-claim allowed: effect was undone
        │                   │
        └───────────────────┘
   ┌───────────────┐
   │ REQUIRES_REVIEW│  ← flag_for_review(); automatic reconciliation has already failed once;
   └───────────────┘    planner sends it to a person, never back through the same machinery
   ┌────────┐
   │EXPIRED │ ← declared in the enum; see §9 for where it is and is not honoured
   └────────┘
```

**Transition table (verified from code, not from docs):**

| From | To | Via | Guard / evidence required |
|---|---|---|---|
| – | `STARTED` | `claim` | identity resolution; budget gate; authority not consumed |
| `STARTED` | `COMPLETED` | `complete` | caller asserts the effect ran |
| `STARTED` | `FAILED` | `fail(certain=True)` | caller asserts the effect did **not** run |
| `STARTED` | `UNKNOWN` | `fail(certain=False)` | effect status genuinely unknown (e.g. timeout) |
| `STARTED` | `UNKNOWN` | any `claim` on a `STARTED` record | automatic, then raises |
| `UNKNOWN` | `COMPLETED` | `reconcile(occurred=True)` | **external evidence** + note |
| `UNKNOWN` | `FAILED` | `reconcile(occurred=False)` | **external evidence** + note |
| `COMPLETED` | `COMPENSATED` | `compensate` | caller asserts the effect was undone |
| `FAILED`/`COMPENSATED` | `STARTED` | `claim` | allowed: no live effect stands in the way |
| any | `REQUIRES_REVIEW` | `flag_for_review` | escalation |

**The invariant that makes this safe:** `UNKNOWN` has exactly two exits, both through
`reconcile`, both requiring the caller to supply a note asserting what the outside world said.
There is **no** `UNKNOWN → STARTED` transition. A `claim` on an `UNKNOWN` record raises rather
than reopening the slot (`ledger.py:1323`).

---

## 4. The recovery decision - `RecoveryEngine.assess`

`src/continuum/recovery/engine.py:306-790`. `assess` is deliberately side-effect-free: it *reads*
the log and produces a sealed `RecoveryContract` plus a `RecoveryDecision`. It never re-executes
anything.

Flow:

```
assess(run_id)
  │
  ├─ CheckpointManager.restore(run_id, replay=True, on_unprojectable="degrade")
  │     └─ latest checkpoint + incremental projection of events after it
  │        (checkpoint/manager.py:282-343). A poisoned log yields the last-good
  │        prefix marked INVALID instead of raising (issue #383).
  │
  ├─ read_all_events (archive-aware: walks the archived prefix too, so
  │    compaction cannot launder confirmations, provenance, risk or
  │    consumed-authority evidence - the "#553 archive-blindness family")
  │
  ├─ REVIEW_CONFIRMED scan → confirmed_components (a human confirmation clears
  │    self-certification; an agent cannot confirm its own work, issue #201)
  │
  ├─ StateValidator.validate(...) → component statuses
  │
  ├─ ActionLedger(...).all() → uncertain = statuses in
  │    {UNKNOWN, STARTED, REQUIRES_REVIEW}      (engine.py:433-443)
  │
  ├─ admissibility check (checkpoint may not reference results computed after it)
  │
  ├─ optional domain validation rules (read-only; can only escalate)
  │
  ├─ optional recovery-ledger attempt budgets - READ-ONLY here; "spending the
  │    allowance is the caller's job, never the assessment's" (engine.py:516-564)
  │
  ├─ plan_repairs(...) → RepairPlan        (recovery/planner.py:250-337)
  │    one RECONCILE_ACTION step per uncertain action,
  │    requires_human=True when strict_unknown (the default)
  │
  ├─ risk policy → most cautious proposed mode
  ├─ consumed-authority scan → escalates, never downgrades (engine.py:680-715)
  │
  └─ _decide(...) → max over all proposals by SEVERITY   (engine.py:905-1059)
       proposals: unprojectable log > uncertain side effects > validation
       > plan.requires_human > exhausted budgets > liveness breach (WAIT)
       > risk > invalid goal > inadmissible checkpoint
```

The verdict ordering is the third load-bearing mechanism: **the most cautious proposal wins**
(`engine.py:1051`). An uncertain side effect outranks every repairable thing ("until we know
whether the world was modified, no further work is safe", `engine.py:957-962`). In the default
`strict_unknown=True` configuration, even one `UNKNOWN`/`STARTED` action forces
`RecoveryMode.REQUEST_HUMAN`.

`RecoveryMode` (`models.py:143+`), ascending caution: `RESUME` < `REPAIR_AND_RESUME` < `WAIT` <
`ROLLBACK` < `REQUEST_HUMAN` < (plus `REPLAN`).

---

## 5. Reconciliation - is it actually implemented?

**Partially, and that is the honest answer.** Three layers must be distinguished:

1. **The ledger protocol** - fully implemented and enforced. `UNKNOWN` has no exit except
   `reconcile`, which demands a note. `claim` refuses to replay. This part is real and tested.

2. **The reconciler registry** - `src/continuum/actions/reconciliation.py` provides a registry
   that maps an `action_type` to a probe callable. A probe is *a read against the external
   system*. The registry exists; whether a given action type has a registered probe depends
   entirely on the caller's configuration. **Out of the box, most action types have no probe.**

3. **The loop that calls the probe** - CONTINUUM does **not** run an autonomous reconciler. The
   engine surfaces `RECONCILE_ACTION` steps in the plan and escalates to `REQUEST_HUMAN` under
   the default policy. A caller (`continuum serve`, an agent harness, or a human via
   `continuum reconcile`) must execute the step.

So: **reconciliation is a protocol and an escalation mechanism, not an automatic closer.** Under
the default strict policy this is the safe failure mode - it blocks rather than guesses. The
residual risk is not "the system replays blindly" but "the system waits for a person, and a
person or a non-strict caller may then replay blindly". Both are exercised as scenarios S2/S3/S6.

**When the external system cannot answer**, the engine has no fallback: the action stays
`UNKNOWN`, the plan keeps demanding a human, and the verdict stays at `REQUEST_HUMAN`. There is
no timeout-driven auto-replay, no "assume it didn't happen" heuristic. Verified in the harness
via a `probe_broken` external world (see Phase 4, S2).

**Absent vs unknown.** The design does distinguish these, but only through the *caller's* probe:
`reconcile(occurred=False)` means "a check confirmed absence", `occurred=True` means "a check
confirmed presence". Nothing in the ledger can validate either claim - the docstring says so
plainly (`ledger.py:1520-1523`: "The caller is trusted to have real evidence, because nothing
here can check the outside world on its behalf."). A probe that returns the wrong answer is an
*external-system* defect, and the audit reports it as a guarantee that depends on the external
system.

---

## 6. Concurrency, leases, and the claim race

### 6.1 The claim race is real and documented

`ActionLedger`'s class docstring (`ledger.py:386-388`):

> Single-writer per run. Deduplication is a claim-then-check against the folded log, not an
> atomic compare-and-set, so two processes claiming the same key at the same instant both read
> "no prior slot" and both open one.

This is a genuine TOCTOU: `resolve_claim` reads the folded log, then `_record` appends. Between
them, another writer can append. Storage does offer `expected_sequence` as a CAS
(`sqlite.py:446-449`), but `ActionLedger._record` does not pass it, so the append is not guarded
by a sequence expectation.

### 6.2 The optional lease

`LeaseCoordinator` (`src/continuum/concurrency/lease.py`) is the offered fix, and it is **opt-in
and off by default** (`ledger.py:418-426`: "Without `lease` the behaviour is exactly as before:
unsynchronised... That remains the default because the single-process path has nothing to
serialise against.").

Two implementations share one contract: `InMemoryLeaseCoordinator` (one process) and
`SQLiteLeaseCoordinator` (a dedicated sidecar DB so separate processes coordinate through the
filesystem). Both take an injectable clock, so expiry is testable without sleeping.

**Verified empirically.** With an *externally-held* run lease, a second worker is fenced:

```
A claimed (lease still held externally): True
lease holder now: worker-A
B blocked by ClaimLockError: run 'r1' is leased to 'worker-A'; 'worker-B' must not write
to the action ledger while another agent owns the run. Retry once the lease is free.
```

### 6.3 Two limits of the lease, both documented in the code

1. **It is per-`run_id`, not per-action.** An unscoped claim (`scoped_to_run=False`) racing the
   same key from *another* run is not covered by this run's lease (`ledger.py:412-415`).
2. **It is not a fencing token.** The lease proves *who* may write, not *that a stale holder's
   write should be accepted. Once a lease expires, `acquire` lets a new holder take over
   (verified: `holder after expiry: None`, `B can take over: True`). A write from the expired
   holder is then accepted by the lease layer - **but is caught by the status-transition guard**
   (§2.3, verified). So fencing is provided by the ledger's state machine, with the lease as a
   first line of defence.

### 6.4 What the lease does NOT cover

- Expired-lease takeover does not itself cancel the in-flight work of the dead holder. If the
  holder is merely *slow* rather than dead, its `complete` can still land - unless a second
  worker has already settled the record, in which case the status guard refuses it. **The safety
  property therefore reduces to: whoever settles second loses.** That is correct, but it means
  the *effect* can be performed twice if both workers perform it before either settles - which is
  exactly the TOCTOU in §6.1, and the subject of scenario S5.

---

## 7. Durability and corruption behaviour

### 7.1 Durability

- `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, `PRAGMA foreign_keys=ON`,
  `PRAGMA busy_timeout=30000` (`sqlite.py:166-172`). Commit is synchronous; `append_event`
  returning means the event survives process death.
- Writes take `BEGIN IMMEDIATE` and roll back on any exception (`sqlite.py:205-216`), so a
  crashed writer cannot leave a half-appended event.
- Sequence allocation is atomic: `_append_chained` reads the current max sequence inside the
  write transaction and assigns `current + 1` (`sqlite.py:438-449`), and `expected_sequence`
  gives callers an opt-in CAS that raises `ConcurrentWriteError` on mismatch.

### 7.2 Corruption

- Malformed event payloads raise `CorruptedRecord` on read, with the run, sequence and event id
  in the message (`sqlite.py:801-811`). Reading is refused rather than returning untrusted
  state - satisfying the "don't silently convert corruption into usable state" requirement.
- Missing out-of-band blobs (the payload-offload path) are likewise `CorruptedRecord`
  (`sqlite.py:809-812`).
- `verify_events` re-audits the hash chain end to end, including the archived prefix after
  compaction, and reports `IntegrityViolation` rows rather than a boolean (`sqlite.py:843-960`).
- The engine's answer to an unprojectable log is `degrade, not raise` (issue #383): the fold
  stops at the last good event, that prefix is marked `INVALID`, and `_decide` turns it into
  `REQUEST_HUMAN` (`engine.py:356-361`, `engine.py:930-940`). A poisoned log still produces a
  verdict.

### 7.3 What is NOT atomic

The ledger's own write and the budget-counter write are **two separate writes**
(`ledger.py:1255-1258` and `_budget_commit_claim` at `ledger.py:599`). The ordering is deliberate
(record first, count afterwards) and the failure mode is documented and safe-by-construction:
losing the accounting is bad, losing a claim the ledger has already recorded is worse, so the
count half is fail-open while the gate half is fail-closed (`ledger.py:613-620`). A crash between
them leaves the log authoritative and the counter under-counting, which can *over*-allow later
attempts but never duplicates an effect.

Similarly, the external effect (step 2) is not atomic with either ledger write - which is the
entire reason the `UNKNOWN` state exists.

---

## 8. Idempotency and duplicate-submission semantics

`idempotency_key` (`src/continuum/actions/idempotency.py:152-200`) supports two identity models:

1. **Derived key** - `stable_hash({scope, type, arguments-with-volatile-stripped})`. Default.
   `volatile` names fields to strip (a `trace_id` must not change the identity).
2. **Caller-supplied key** - `stable_hash({scope, type, key})`, Stripe-style. Overrides argument
   hashing entirely, documented with the reasoning that "sending the same reminder twice is two
   sends, not one" (`idempotency.py:174-177`).

`scope` shapes the key only. With the default per-run scope, two runs performing the same logical
operation get different keys and do **not** deduplicate against each other. With
`scoped_to_run=False` the key is store-global, and `claim` then consults other runs
(`ledger.py:1105-1117`): `COMPLETED` elsewhere answers the claim, `STARTED`/`UNKNOWN` elsewhere
raises.

**Drift-tolerant fallback** (`_identity_match`, `ledger.py:858-968`): when the caller supplies no
explicit key and no exact key matches, the ledger tries to match on *identity tokens* - strong
resource-identifying strings extracted from the arguments, with weak tokens and stopwords
excluded (`idempotency.py:203-320`). This is what lets a retried claim with slightly different
arguments still find the prior attempt. It is deliberately conservative:
- locations must agree ("same leaves, different directories, is different work", issue #365);
- a stored record with *more* tokens only matches if the extras are derived from the shared ones;
- an empty `known` set matches nothing.

**Duplicate submissions are not assumed equivalent.** Two actions with similar payloads are only
equivalent if either their arguments hash identically (modulo declared `volatile`), or an explicit
key is supplied, or the token-based fallback finds a containment relationship. Absent those, they
are two separate slots - the correct default for non-idempotent operations.

---

## 9. Safety assumptions identified

Each is stated in the code; each is a limit on the guarantee, not a defect to fix.

| # | Assumption | Where stated | Consequence if violated |
|---|---|---|---|
| A1 | The caller performs the effect between `claim` and `complete`, and never before `claim` returns | `ledger.py:11-15` | The `UNKNOWN` window is the only protection; performing the effect first makes the record lie |
| A2 | `reconcile`'s caller has real evidence about the outside world | `ledger.py:1520-1523` | A false `occurred` assertion is recorded as truth; nothing here can check |
| A3 | Single writer per run, or an opt-in lease | `ledger.py:386-388`, `418-426` | Concurrent claims on one key can both open slots (TOCTOU) |
| A4 | The lease is per-`run_id`; cross-run unscoped claims are not covered | `ledger.py:412-415` | Two runs claiming one store-global key can both proceed |
| A5 | `on_unknown`, if supplied, resolves with evidence | `ledger.py:1323-1330` | A naive callback can reopen an uncertain action (see S2) |
| A6 | Storage is honest: `synchronous=FULL`, append-only, hash-chained | `base.py:9-16`, `sqlite.py:166-172` | All bets off; the log is the only source of truth |
| A7 | Reconcilers/probes are caller-registered per action type | `actions/reconciliation.py` | Unregistered action types have no automatic path out of `UNKNOWN` |
| A8 | The default `strict_unknown=True` policy is not disabled | `engine.py:956-962` | Lenient mode turns `REQUEST_HUMAN` into `WAIT` and permits automatic steps |

### One observation on `ActionStatus.EXPIRED`

`models.py:140` declares `EXPIRED`, but the four settlement methods accept only the states listed
in §2.3. An `EXPIRED` record reaching `claim` therefore falls through to the `STARTED or UNKNOWN`
branch at `ledger.py:1323` and is treated as *uncertain* - the conservative reading, since an
expired action's effect is genuinely unknown. This is safe-by-accident rather than
safe-by-design: it works because the fallthrough is the cautious branch. Noted as an observation,
not filed as a defect, because the behaviour is correct.

---

## 10. Direct answers to the Phase 2 questions

**What state is persisted before the external call?** An `ACTION_RECORDED` event holding the full
`Action` with `status=STARTED`, the idempotency key, the action id, and (optionally) pinning,
grant and origin digest. It is durable once `append_event` returns.

**What happens if the process crashes after claiming?** The `STARTED` record persists. On
restart, `pending()` returns it; `assess` classifies it as uncertain; the plan issues a
`RECONCILE_ACTION` step; the verdict becomes `REQUEST_HUMAN` under the default policy. Any
`claim` on that key raises `UnknownSideEffect` and marks it `UNKNOWN`.

**How are expired claims and worker leases handled?** Leases expire on a TTL and a new holder may
take over (`lease.py:100-113`). There is no per-action claim expiry - an action record never
expires on its own; `EXPIRED` exists in the enum but is not written by the ledger's settlement
paths.

**Can two workers execute the same action concurrently?** Yes, in the default leaseless
configuration, because `claim` is a read-then-write TOCTOU (§6.1). With an externally-held run
lease, the second is fenced by `ClaimLockError` (verified). Across runs on a store-global key,
the lease does not help; the `STARTED` foreign record does, by raising.

**What prevents duplicate submissions?** The idempotency key (exact, or the token-based drift
fallback), plus the refusal to re-open a `STARTED`/`UNKNOWN` slot. Not a database unique
constraint - the log fold plus the raise.

**What is the source of truth for whether an external effect occurred?** The external system. The
ledger is explicitly *not* the source of truth for that question; it is the record of what was
attempted and what was verified. `base.py:20-24` states this directly.

**Is reconciliation actually implemented, or only assumed?** The protocol is implemented and
enforced. The *probe registry* exists but is caller-populated. The *automatic loop* is not
implemented - by design, the engine escalates rather than guessing. See §5.

**What happens when the external system cannot answer whether an action succeeded?** The action
stays `UNKNOWN`, the verdict stays `REQUEST_HUMAN`, indefinitely. No auto-replay, no
assume-absent heuristic.

**Can recovery distinguish an absent effect from an unknown outcome?** Only via the caller's
probe: `reconcile(occurred=False)` is "confirmed absent", `UNKNOWN` is "not known". The ledger
cannot verify the distinction and does not pretend to.

**What happens if the durable state is corrupted or only partially written?** Committed events
survive; uncommitted transactions roll back. Corrupt payloads raise `CorruptedRecord` on read
(refusing to return untrusted state). An unprojectable log degrades to the last-good prefix
marked `INVALID` and yields `REQUEST_HUMAN` rather than an exception. `verify_events` reports the
specific violations. See §7.

---

## 11. Surfaces chosen for fault injection (Phase 3 inputs)

From the architecture above, the seams where a crash is both possible and consequential:

| Seam | Why it matters |
|---|---|
| after `claim` commits, before the effect | opens the `UNKNOWN` window |
| after the effect, before `complete` commits | the effect happened, the record did not |
| inside `complete`, between `_record` and `_budget_consume_settlement` | accounting vs log divergence |
| `fail(certain=False)` after a sent request | ambiguous failure vs confirmed absence |
| concurrent `claim` on one key (leaseless) | the TOCTOU |
| concurrent `claim` with an expired lease takeover | stale-worker fencing |
| store-global key across two runs | A4 |
| corrupt/truncated event payload at a storage boundary | S7 |
| `reconcile` evidence being wrong or unavailable | S2/S3/S6 |
| `on_unknown` reopening an uncertain action | A5 |

**No production seam is added for test convenience.** The harness uses the library's real
interfaces (`SQLiteStorage`, `ActionLedger`, `LeaseCoordinator` with an injectable clock) plus one
test-only `ExternalWorld` double. Faults are injected by raising at named points in the worker's
own protocol, not by patching production code.

---

## 12. Files traced, with the load-bearing lines

| File | Lines read | Role |
|---|---|---|
| `actions/ledger.py` | 1-200, 380-700, 925-1358, 1360-1640 | the protocol, the guards, the race |
| `actions/idempotency.py` | 47-320, 549-700 | identity derivation |
| `actions/reconciliation.py` | whole | reconciler registry |
| `recovery/engine.py` | 254-420, 430-530, 560-790, 905-1059 | `assess`, `_decide` |
| `recovery/planner.py` | 178-345 | `plan_repairs` |
| `recovery/contract.py` | 77-370 | sealed contract |
| `recovery/restore.py` | whole | restore through the precondition gate |
| `concurrency/lease.py` | whole | lease contract + both coordinators |
| `storage/base.py` | 1-200 | the honest guarantees |
| `storage/sqlite.py` | 163-250, 374-560, 795-960 | durability, corruption, chain audit |
| `checkpoint/manager.py` | 276-345 | restore + incremental projection |
| `models.py` | 130-160, 1421-1444 | `ActionStatus`, `UnknownSideEffect` |
