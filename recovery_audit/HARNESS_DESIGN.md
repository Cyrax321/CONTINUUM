# Phase 3 - Fault-Injection Harness: Design

**Audited tree:** `/private/tmp/audit-recovery` (detached `c6e00411` + owner WIP, frozen).
**Harness:** `recovery_audit/harness/` (shipped with the audit, not added to `tests/`).
**Status:** design complete → implementation next.

The mission requires a design *before* implementation that identifies the **real production
seams**, uses test-only hooks or controlled fake dependencies, and avoids production changes
made solely for test convenience. This is that design.

---

## 1. What "a crash" actually is here

A CONTINUUM worker performing an external side effect executes this sequence against real code
(`src/continuum/actions/ledger.py`):

```
caller          claim(action_type, arguments)
                  └─ resolve_claim()          read: fold ACTION_* events
                  └─ _record()                WRITE #1: ACTION_RECORDED   [STARTED]
                  └─ _budget_commit_claim()   write: budgets JSON (best-effort)
caller          ── perform the external effect ──        ← outside the library
caller          complete(key, external_id, result)
                  └─ _require(key)            read: fold
                  └─ _record()                WRITE #2: ACTION_RECORDED   [COMPLETED]
                  └─ _budget_consume_settlement()  write: budgets JSON
```

A crash is **the process dying between or during those steps**, such that only durable state
survives. Two things survive: the SQLite event log and the budgets JSON. Everything in the
Python heap - the `ActionLedger`, the DB connection, the in-memory lease - is gone.

**Simulation model.** The harness does not kill a process. It abandons the worker objects
(*no* `close()`, *no* `__exit__`, *no* finalizer runs) and reconstructs fresh ones against the
same on-disk artifacts. That is exactly what a restart does, and it is what makes the harness
faithful rather than theatrical. Two properties of the audited code make an *exception raised
at a chosen point* equivalent to a SIGKILL for the durability question:

1. `SQLiteStorage._write` wraps every mutation in `BEGIN IMMEDIATE` … `ROLLBACK on any
   exception` (`sqlite.py:201–216`). An exception inside a write therefore leaves **no partial
   write** - the same as a crash that arrived before commit.
2. The only `finally` on the crash path is `ActionLedger._locked` (`ledger.py:466–479`), and it
   releases an in-memory lease only. It performs no durable work, so running it (or not) cannot
   change what survives.

The harness asserts both properties hold for every seam it injects into, and fails loudly if a
`finally`/`__exit__` with a durability side effect ever appears on a crash path - that would make
the simulation unsound, and it is exactly the regression an audit must catch.

---

## 2. The seams, and where each lives in real code

Seams are chosen from the protocol's three durable writes and the gaps around them, plus the
concurrency and corruption surfaces mapped in `ARCHITECTURE.md` §11.

| ID | Seam | Real code location | What a crash/interleave there leaves on disk |
|---|---|---|---|
| **SEAM-1** | After `claim`'s ACTION_RECORDED commits, before the external effect | `claim` returns at `ledger.py:1330`; caller then acts | STARTED record, no completion. **The canonical unknown-outcome state.** |
| **SEAM-2** | After the effect succeeds, before `complete` commits | caller → `complete` at `ledger.py:1360` | Effect **landed externally**, log shows STARTED. **S3's whole premise.** |
| **SEAM-3** | *Inside* `complete`, between `_record` and `_budget_consume_settlement` | `ledger.py:1446–1452` | COMPLETED is durable; the settlement counter is not. Budget/log divergence. |
| **SEAM-4** | *Inside* `claim`, between `_record` and `_budget_commit_claim` | `ledger.py:1325–1330` | STARTED durable; claim slot never counted. Over-cap amplification. |
| **SEAM-5** | Effect attempted, response lost, caller calls `fail(certain=False)` | `fail` at `ledger.py:1446` | UNKNOWN + `side_effect_uncertain`. The honest timeout path. |
| **SEAM-6** | Two workers race `claim` on one key with **no lease** | `claim` read-then-write; `expected_sequence` never passed (grep-verified) | Two STARTED records for one identity → **double execution**. |
| **SEAM-7** | Same identity across two runs, unscoped key | `claim` → `resolve_claim` foreign branch, `ledger.py:1102–1116` | Cross-run dedupe or `UnknownSideEffect`. |
| **SEAM-8** | Corrupt / truncated event payload at a storage boundary | `_row_to_event` → `CorruptedRecord` (`sqlite.py:795–833`) | Poisoned log; `assess` must degrade, not raise. |
| **SEAM-9** | Reconciliation evidence wrong or unavailable | `reconcile` at `ledger.py:1493`; probe registry caller-populated | The loop that must stay open, or be closed wrongly. |
| **SEAM-10** | `on_unknown` callback reopens an uncertain action | `claim` at `ledger.py:1313–1323` | UNKNOWN resolved by caller assertion, no evidence. |
| **SEAM-11** | Lease expiry mid-flight, second worker takes over | `lease.py`; `claim` → `_locked` | Takeover without a fencing token. |

SEAM-6 and SEAM-11 are **interleavings**, not crashes; the harness models them with two
worker objects sharing one store and a deterministic ordering, not with a raise.

---

## 3. Components

### 3.1 `ExternalWorld` - the deterministic fake external system

The thing that must never be double-executed. It is a strict accounting ledger, not a mock:

```python
class ExternalWorld:
    attempts: list[Attempt]     # every call, in order, with its arguments
    occurred: dict[str, str]    # business key -> external id, only if the effect LANDED
    failures: set[str]          # business keys configured to fail
    latency_order: list[str]    # deterministic per-key completion ordering
```

- `perform(business_key, payload)` records the attempt, then either lands (`occurred[key] = id`)
  or raises, per configuration. Landing is **immediate and atomic** - there is no window in the
  fake world, by design, so the *only* windows come from the ledger's real crash points. This
  keeps the attribution of any double-execution unambiguous: if `occurred` grows twice for one
  key, the ledger did it.
- `receipt(business_key)` is the probe the reconciler calls: it reports whether the effect
  landed, truthfully, from `occurred`. A scenario can configure the probe to **lie or be
  unavailable** (`receipt` raising `EvidenceUnavailable`) to exercise SEAM-9.
- Every method appends to an ordered, human-readable `trace`, which is the scenario's execution
  log and the evidence artifact.

`ExternalWorld` is deliberately *not* a `MagicMock`. A mock returns a `MagicMock` from any
method the test forgets to configure, which silently passes; this object raises `TypeError` on
any unconfigured behaviour. (Rationale: my own memory notes a real bug class in this repo where
`MagicMock` postgres tests never validated SQL text and only the live suite caught it.)

### 3.2 `Crash` and `CrashingStorage` - controlled fault injection

`Crash(BaseException)` - a `BaseException`, not `Exception`, so it is not swallowed by the
library's broad `except Exception` handlers (the budget helpers and `_count_claim` all swallow
`Exception`; a crash that got swallowed would be a silent no-op and the scenario would prove
nothing).

`CrashingStorage` subclasses `continuum.storage.base.Storage` and delegates every method to a
real `SQLiteStorage`. It raises `Crash` on the **Nth occurrence of a named method**
(`append_event`, `read_events`, `read_all_events`, …) and otherwise passes through untouched.
This is the mission's sanctioned "controlled fake dependency": a test-only wrapper over the
production storage contract, added to the audit tree, not the library.

Because `_write` rolls back on any exception, a `Crash` raised *inside* `append_event` is
"the write never became durable"; a `Crash` raised *between* two storage calls (counted on the
*Nth* call where the crash lands after call N−1 returns) is "a gap between two durable writes".
That distinction is what lets SEAM-3 and SEAM-4 be expressed precisely.

`CrashingStorage` is also where the harness *asserts the simulation's soundness*: it records
which methods ran `finally`/`__exit__` blocks, and the harness fails any scenario whose crash
path had a durability side effect in cleanup.

### 3.3 `Worker` - a faithful minimal caller

```python
class Worker:
    """The smallest caller that reproduces a real agent's action loop."""
    def attempt(self, action_type, arguments, *, key=None, on_unknown=None,
                fail_mode="raise", reconcile_probe=None) -> AttemptResult
```

It calls `claim` → `world.perform` → `complete` / `fail` in that order, with the crash point
chosen by the scenario. It is deliberately stupid: it has **no retry logic of its own**, because
the point is to observe what *the library* does on restart, not what a clever caller might
improvise. Any retry is an explicit, visible scenario step, so a finding cannot be hidden behind
caller cleverness (mission rule 7).

`fail_mode` selects what the worker does when the external call raises: `"raise"` (propagate),
`"fail"` (`ledger.fail(key, err)`), or `"uncertain"` (`ledger.fail(key, err, certain=False)`) -
the three real caller behaviours, which produce materially different durable states.

### 3.4 `restart()` - the crash, and the recovery

```python
def restart(self) -> "Worker":
    """Abandon all in-memory state; rebuild against the durable artifacts."""
```

This is the crux. The scenario calls `worker.crash_at(SEAM)` (which arms `CrashingStorage`),
runs the operation, catches the `Crash`, and then **discards the worker, the ledger, and the
storage object entirely** - no `close()`, no `__exit__`, no `finally` in the harness that would
flush anything. `restart()` constructs a fresh `SQLiteStorage` on the *same file path*, a fresh
`ActionLedger`, and a fresh `Worker`. Only the `ExternalWorld` survives (it is the outside
world; it does not reset on our crash), which is correct: the external system is a separate
process and keeps whatever it has.

The harness asserts the reset is real: the restarted worker must see `continuum.__file__` and
storage state coming from the on-disk artifacts, and must **not** share any object identity with
the pre-crash worker. If it did, the "restart" would be reading cached memory and every PASS
would be meaningless.

### 3.5 `ScenarioResult` - the verdict

```python
@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    verdict: Literal["SAFE", "UNSAFE", "UNKNOWN_OUTCOME"]
    executions: int              # how many times the effect actually landed
    recovery_action: str         # what the restarted worker was forced to do
    trace: list[str]             # ordered, deterministic execution log
    assertions: dict[str, bool]  # every named assertion and its outcome
```

`verdict` is derived, not asserted by the scenario author:
- `executions > 1` → **UNSAFE** (a duplicate external side effect actually happened)
- `executions == 1` and the restarted worker was able to settle safely → **SAFE**
- `executions <= 1` but the restarted worker cannot determine the outcome and must escalate →
  **UNKNOWN_OUTCOME** - not unsafe, but the acceptance criterion demands this be *visible*,
  because an operator who treats it as safe is the bug.

The three-valued verdict is the design's answer to the mission's core warning: a green test
suite must never be read as proof of exactly-once. `UNKNOWN_OUTCOME` makes the honest answer a
first-class result rather than a failure to be flattened.

---

## 4. Determinism guarantees

The harness is deterministic or it is worthless. Concretely:

1. **No wall-clock, no sleeps, no threads.** Timing-based races are replaced by counted
   injection points and explicit interleavings. A scenario re-run gives a byte-identical `trace`.
2. **Counted, not timed, crashes.** `Crash` fires on the Nth call to a named method. N is
   derived per-scenario from the seam, never from observing the clock.
3. **`ExternalWorld.latency_order`** replaces network timing: the scenario declares which key
   completes first, so interleavings are chosen, not raced.
4. **One store per scenario**, in a `tempfile.TemporaryDirectory` the scenario owns; the audit
   never touches a real database (mission rule 11).
5. **Lease clocks are injected** (`InMemoryLeaseCoordinator(clock=...)`) - never real time.
6. **The trace is the artifact.** Every scenario writes its `trace` to
   `recovery_audit/evidence/phase4/<scenario_id>.txt`; the raw, ordered execution log is what a
   reader checks the verdict against.

Determinism is itself tested: the smoke test runs one scenario three times and asserts the three
traces are identical. If that assertion ever fails the harness is broken, not the library.

---

## 5. What the harness deliberately does NOT do

- **No production code changes.** Zero edits to `src/`. Every hook is a wrapper in the audit
  tree over an existing public/private seam. (The private `_record` is reached by wrapping
  `storage.append_event`, which is a public storage method - so no monkeypatching of library
  internals is required for SEAM-3/SEAM-4.)
- **No `MagicMock`** anywhere on the safety surface, for the reason in §3.1.
- **No skipped tests, no `pytest.mark.xfail`, no weakened assertions.** An unsafe scenario
  reports UNSAFE; the harness never converts a safety failure into a non-failure.
- **No real network, no real filesystem outside the temp dir, no real credentials.**
- **No retry cleverness in `Worker`.** Visible, explicit, and scenario-controlled.

---

## 6. Why this design can find real bugs

The seams are derived from traced code, and three of them already carry a documented
expectation of failure - which is what makes them worth instrumenting rather than decorative:

- **SEAM-6** targets the TOCTOU that `ActionLedger`'s own class docstring admits to and that
  grep-verified absence of `expected_sequence` in `actions/ledger.py` confirms. Expected: UNSAFE
  under no-lease concurrency, and the harness should show the exact double-write.
- **SEAM-3/SEAM-4** target the budget counter/log divergence that issue #1168's ordering was
  written to prevent; the crash *between* the two writes is the case the ordering cannot cover.
- **SEAM-2** is S3, and `ARCHITECTURE.md` V1/V4 already prove the *library* behaves correctly
  here (re-claim raises `UnknownSideEffect`; the status guard fences a stale completion without
  any lease). The harness's job is to prove the *full loop* - worker + world + restart - cannot
  be talked into re-firing, and to make the UNSAFE/UNKNOWN_OUTCOME distinction observable.

The harness is therefore falsifiable in both directions: a scenario returning SAFE is evidence,
a scenario returning UNSAFE is a bug with a reproducer, and a scenario returning
UNKNOWN_OUTCOME is the audit's honest finding that the guarantee is conditional on a human.
