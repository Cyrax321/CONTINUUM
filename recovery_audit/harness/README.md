# Phase 3 - Fault-Injection Harness

Deterministic harness for the CONTINUUM recovery-safety audit. Design and seam
table: [`../HARNESS_DESIGN.md`](../HARNESS_DESIGN.md).

## Files

| File | Purpose |
|---|---|
| `fault_injection.py` | The harness. `ExternalWorld`, `Crash`/`CrashPoint`/`CrashingStorage`, `Worker`, `Scenario`, `ScenarioResult`. |
| `smoke.py` | Tests the **harness**, not the library. Proves crashes land where claimed, that a crash inside a write leaves no partial write, that restart sees only durable state, and that runs are deterministic. |

The harness modifies **no production code**. `CrashingStorage` subclasses the real
`SQLiteStorage`, so WAL, `synchronous=FULL`, hash chaining and `BEGIN IMMEDIATE` +
`ROLLBACK` all apply unchanged. It lives in `recovery_audit/`, not in the repo's
`tests/` directory.

## How to run

From the audited worktree (this is the only correct place - see
[`../BASELINE.md`](../BASELINE.md); the shared main checkout moves under you):

```bash
cd /private/tmp/audit-recovery
PYTHONPATH=/private/tmp/audit-recovery/src:/private/tmp/audit-recovery \
    .venv/bin/python recovery_audit/harness/smoke.py
```

`PYTHONPATH` must come first: the editable-install `.pth` points at the shared main
checkout's `src`, and without the override the harness would import the wrong tree.
The second entry (`/private/tmp/audit-recovery`) lets `smoke.py` import
`fault_injection` from its own directory.

Exit status 0 and `HARNESS SMOKE PASSED` = the harness is sound. Captured output:
[`../evidence/phase3/harness_smoke_output.txt`](../evidence/phase3/harness_smoke_output.txt).

## What the smoke test proves

Each block is a property of the *harness*, established before any scenario trusts it:

1. **`ExternalWorld` is atomic and honest** - no window inside the fake world, so any
   duplicate landing came from the ledger. Non-idempotent keys land twice; idempotent
   keys absorb a repeat without a new side effect but still record the call. Probes
   can be configured to lie or to be unable to answer.
2. **`Crash` cannot be swallowed** - it is a `BaseException`, not an `Exception`,
   because the ledger's budget and metrics helpers catch `Exception`.
3. **`CrashingStorage` is a real SQLite storage** - subclass, not a reimplementation.
4. **Crash counts are absolute from storage creation** - arming late still names a
   point correctly. The scenario setup append is always #1.
5. **A crash *inside* a write leaves no partial write** - the write before it survives,
   the crashed write does not. This is what makes an exception a faithful SIGKILL.
6. **A crash *after* a write returns leaves that write committed** - the gap between
   two durable writes is modelled, not just the pre-commit window.
7. **SEAM-1 (post-claim, pre-effect)** - restart sees `STARTED`, the effect never
   landed, and no durability cleanup ran in the crash window. If cleanup *had* run,
   the simulation would be unsound and every verdict suspect; the harness asserts it.
8. **SEAM-2 (post-effect, pre-complete)** - the effect landed externally but the log
   still shows `STARTED`, and a blind re-claim raises `UnknownSideEffect` rather than
   re-firing.
9. **Determinism** - the same scenario run three times yields byte-identical traces.
10. **Evidence round-trips** - scenario JSON is what a later phase audits against.

## The crash model, stated plainly

A crash is **the process dying between or during the protocol's durable writes**,
such that only durable state survives. The harness does not kill a process. It:

1. raises `Crash` at a counted, named point;
2. **abandons** the worker, the ledger, and the storage object - no `close()`, no
   `__exit__`, no flush, which is exactly what a SIGKILL skips;
3. **restarts** by constructing fresh objects against the same on-disk artifacts.

Only the `ExternalWorld` survives the crash, which is correct: the external system is
a separate process and keeps whatever it has.

Two properties of the audited code make raising an exception equivalent to a SIGKILL
for the durability question, and the harness asserts both:

- `SQLiteStorage._write` issues `ROLLBACK` for **any `BaseException`**, so a crash
  inside a write is a crash before commit.
- The only `finally` on the crash path is `ActionLedger._locked`, which releases an
  in-memory lease and performs no durable work.

If either ever changes, `cleanup_ran` populates, the smoke test fails, and every
verdict the harness produced becomes suspect rather than silently wrong.

## Two ways to inject, and when to use each

| Mechanism | For | Example |
|---|---|---|
| `Worker.attempt(crash_at="after-claim" \| "after-effect" \| "after-complete")` | Windows **between** a storage write and a non-storage call - the worker's own action loop. | SEAM-1, SEAM-2 (S1, S2, S3) |
| `CrashPoint(method, n, when="inside" \| "after")` via `scenario.crash(point, fn, worker)` | Windows **inside** library code, between two storage calls. Counted absolutely from storage creation. | SEAM-3, SEAM-4 (inside `complete` / `claim`) |

Forcing a worker-step crash onto the storage surface is a category error: the window
between `claim` committing and the effect being performed contains no storage call, so
no `CrashPoint` can name it. Use `crash_at`.

## Verdicts

`ScenarioResult.verdict` is **derived, never asserted by the scenario author**:

| Verdict | Meaning |
|---|---|
| `UNSAFE` | A duplicate external side effect actually happened (`landing_order` has the same key twice). This is a bug with a reproducer. |
| `SAFE` | The effect happened at most once and recovery settled it without guessing. |
| `UNKNOWN_OUTCOME` | The outcome cannot be determined from durable state and recovery must escalate rather than guess. **Not unsafe, and not a pass** - the mission requires this be visible, because an operator who treats it as safe is the bug. |

The three-valued verdict is deliberate. A green suite must never be read as proof of
exactly-once; `UNKNOWN_OUTCOME` keeps the honest answer first-class.
