"""Smoke test for the fault-injection harness.

Run from the repo root of the audited worktree:

    cd /private/tmp/audit-recovery
    PYTHONPATH=/private/tmp/audit-recovery/src:.venv/bin/python \
        recovery_audit/harness/smoke.py

Exit status 0 = the harness is sound and deterministic. This file tests the
HARNESS, not the library: a library bug found here would be re-run as a real
Phase 4 scenario, not reported from this file.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

from continuum.events import EventType
from continuum.models import ActionStatus, Run
from continuum.storage.sqlite import SQLiteStorage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fault_injection import (  # noqa: E402
    Crash,
    CrashPoint,
    CrashingStorage,
    EvidenceUnavailable,
    ExternalWorld,
    Scenario,
    Worker,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# --------------------------------------------------------------------------- #


def smoke_external_world_is_atomic_and_honest() -> None:
    print("\n== ExternalWorld: atomic landing, honest probe, no window ==")
    w = ExternalWorld()
    ext = w.perform("invoice-1", {"amount": 10})
    check("perform lands and returns an id", ext.startswith("ext_invoice-1"))
    check("occurred records the first receipt", w.occurred["invoice-1"] == ext)
    check("probe reports the truth", w.receipt("invoice-1") == ext)
    check("probe reports absence honestly", w.receipt("never-sent") is None)
    check("one attempt recorded", len(w.attempts) == 1, str(w.attempts))
    check("execution_count counts landings", w.execution_count == 1)
    check("distinct_keys counts keys", w.distinct_keys == 1)

    # The dangerous class: a non-idempotent external system called twice.
    w.perform("invoice-1", {"amount": 10})
    check("a repeat perform lands again", w.landings_of("invoice-1") == 2)
    check("duplicate_executions is 1", w.duplicate_executions == 1)
    check("the duplicate is visible in the trace",
          "DUPLICATE SIDE EFFECT" in "\n".join(w.trace))
    check("attempts_of counts calls", w.attempts_of("invoice-1") == 2)

    # The safe class: an idempotent external system absorbs a repeat call.
    wid = ExternalWorld(idempotent=["pay-1"])
    a = wid.perform("pay-1", {"amount": 10})
    b = wid.perform("pay-1", {"amount": 10})
    check("idempotent repeat returns the same receipt", a == b)
    check("idempotent repeat lands nothing new", wid.execution_count == 1)
    check("idempotent repeat is recorded as a call", wid.attempts_of("pay-1") == 2)

    failing = ExternalWorld(failures=["bad-1"])
    try:
        failing.perform("bad-1", {})
    except RuntimeError:
        check("configured failure raises", True)
    else:
        check("configured failure raises", False, "no exception")
    check("failed perform does not land", "bad-1" not in failing.occurred)

    liar = ExternalWorld(liars=["x"])
    liar.perform("x", {})
    check("liar reports the opposite of the truth", liar.receipt("x") is None)

    blind = ExternalWorld(unavailable=["y"])
    try:
        blind.receipt("y")
    except EvidenceUnavailable:
        check("unavailable probe raises", True)
    else:
        check("unavailable probe raises", False, "no exception")


def smoke_crash_is_not_swallowed() -> None:
    print("\n== Crash is a BaseException and cannot be swallowed ==")
    check("Crash is a BaseException", issubclass(Crash, BaseException))
    check(
        "Crash is NOT an Exception (budget/metrics handlers swallow Exception)",
        not issubclass(Crash, Exception),
    )


def smoke_crashing_storage_is_a_real_sqlite_storage() -> None:
    print("\n== CrashingStorage inherits real durability behaviour ==")
    check(
        "CrashingStorage subclasses SQLiteStorage",
        issubclass(CrashingStorage, SQLiteStorage),
    )
    with tempfile.TemporaryDirectory() as d:
        st = CrashingStorage(os.path.join(d, "s.db"))
        try:
            check("unarmed storage appends normally", st.append_event is not None)
        finally:
            st.close()


def smoke_counts_are_absolute_from_creation() -> None:
    print("\n== crash counts are absolute from storage creation ==")
    with tempfile.TemporaryDirectory() as d:
        st = CrashingStorage(os.path.join(d, "s.db"))
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})
        # Arm AFTER the setup append. The count must still be absolute: the next
        # append is call #2, so a crash point of 2 must fire on it.
        st.arm(CrashPoint("append_event", 2, when="inside"))
        fired = False
        try:
            st.append_event("r1", EventType.RUN_COMPLETED, {"k": "v"})
        except Crash:
            fired = True
        check("arming after setup still counts absolutely", fired)


def smoke_crash_before_commit_leaves_no_partial_write() -> None:
    print("\n== a Crash before a write commits leaves no partial write ==")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        st = CrashingStorage(path)
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})  # call #1
        st.arm(CrashPoint("append_event", 2, when="inside"))
        try:
            st.append_event("r1", EventType.RUN_COMPLETED, {"k": "v"})  # call #2
        except Crash:
            pass
        st2 = SQLiteStorage(path)
        try:
            types = [e.type for e in st2.read_events("r1")]
        finally:
            st2.close()
        check(
            "the pre-write crashed call did not land",
            EventType.RUN_COMPLETED not in types,
            str([t.value for t in types]),
        )
        check(
            "the write before the crash survived",
            EventType.RUN_STARTED in types,
        )


def smoke_crash_after_a_write_leaves_it_durable() -> None:
    print("\n== a Crash after a write returns leaves that write committed ==")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.db")
        st = CrashingStorage(path)
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})  # #1
        st.arm(CrashPoint("append_event", 2, when="after"))
        try:
            st.append_event("r1", EventType.RUN_COMPLETED, {"k": "v"})  # #2
        except Crash:
            pass
        st2 = SQLiteStorage(path)
        try:
            types = [e.type for e in st2.read_events("r1")]
        finally:
            st2.close()
        check(
            "the completed write DID land (after-return crash)",
            EventType.RUN_COMPLETED in types,
            str([t.value for t in types]),
        )


def smoke_worker_completes_without_a_crash() -> None:
    print("\n== Worker: clean claim -> effect -> complete ==")
    s = Scenario("smoke_clean")
    try:
        w = s.worker()
        r = w.attempt("charge_card", {"amount": 10}, "inv-1")
        check("worker completed the action", r.status is ActionStatus.COMPLETED)
        check("worker recorded the external id", r.external_id is not None)
        check("the effect landed once", s.world.execution_count == 1)
        check("the ledger is settled", w.status(r.key) is ActionStatus.COMPLETED)
        check("no duplicate", s.world.duplicate_executions == 0)
    finally:
        s.cleanup()


def smoke_crash_after_claim_before_effect() -> None:
    """SEAM-1: the canonical unknown-outcome state."""
    print("\n== SEAM-1: crash after claim, before the effect ==")
    s = Scenario("smoke_seam1")
    try:
        w = s.worker()

        def step(worker: Worker) -> None:
            worker.attempt("charge_card", {"amount": 10}, "inv-1", crash_at="after-claim")

        s.crash_at_step(step, w)
        check("the crash fired", "!!! CRASH" in "\n".join(s.trace))

        w2 = s.restart()
        check("restart built a fresh worker", w2 is not w)
        check("restart built a fresh storage object", w2.storage is not w.storage)
        actions = list(w2.ledger.folded().values())
        check("exactly one action survived the crash", len(actions) == 1, str(actions))
        a = actions[0]
        check(
            "the surviving action is STARTED, not COMPLETED",
            a.status is ActionStatus.STARTED,
            str(a.status),
        )
        check("the effect never landed", s.world.execution_count == 0)
        check(
            "no durability cleanup ran in the crash window",
            not w.storage.cleanup_ran,
            str(w.storage.cleanup_ran),
        )
    finally:
        s.cleanup()


def smoke_crash_after_effect_before_complete() -> None:
    """SEAM-2 / S3: the effect landed externally, the completion record is lost."""
    print("\n== SEAM-2: crash after the effect, before complete() ==")
    s = Scenario("smoke_seam2")
    try:
        w = s.worker()

        def step(worker: Worker) -> None:
            worker.attempt("charge_card", {"amount": 10}, "inv-1", crash_at="after-effect")

        s.crash_at_step(step, w)
        check("the effect DID land before the crash", s.world.execution_count == 1)

        w2 = s.restart()
        actions = list(w2.ledger.folded().values())
        check("one action survived", len(actions) == 1)
        check(
            "the surviving action is still STARTED",
            actions[0].status is ActionStatus.STARTED,
            str(actions[0].status),
        )
        # The naive recovery: blindly retry. This is what the audit must catch.
        try:
            w2.attempt("charge_card", {"amount": 10}, "inv-1")
        except Exception as exc:
            check(
                "a blind re-claim of an interrupted action raises",
                type(exc).__name__ == "UnknownSideEffect",
                f"{type(exc).__name__}: {exc}",
            )
        else:
            check("a blind re-claim raises", False, "no exception raised")
        check(
            "the blind retry did NOT land a second side effect",
            s.world.execution_count == 1,
        )
    finally:
        s.cleanup()


def smoke_determinism_three_runs_identical() -> None:
    print("\n== determinism: the same scenario run 3x gives an identical trace ==")
    traces: list[list[str]] = []
    for _ in range(3):
        s = Scenario("smoke_det")
        try:
            w = s.worker()

            def step(worker: Worker) -> None:
                worker.attempt(
                    "charge_card", {"amount": 10}, "inv-1", crash_at="after-claim"
                )

            s.crash_at_step(step, w)
            s.restart()
            traces.append(list(s.trace) + list(s.world.trace))
        finally:
            s.cleanup()
    uniq = {json.dumps(t) for t in traces}
    check("three traces are identical", len(uniq) == 1)
    if len(uniq) != 1:
        for i, t in enumerate(traces):
            print(f"    run {i}: {t}")


def smoke_evidence_writer_round_trips() -> None:
    print("\n== evidence JSON round-trips ==")
    from fault_injection import ScenarioResult

    r = ScenarioResult(
        scenario_id="smoke_evidence",
        verdict="SAFE",
        executions=1,
        recovery_action="reconcile",
        assertions={"a": True, "b": False},
        notes=["n"],
        trace=["t"],
    )
    with tempfile.TemporaryDirectory() as d:
        s = Scenario("unused")
        try:
            p = s.write_evidence(r, Path(d))
        finally:
            s.cleanup()
        data = json.loads(p.read_text())
        check("verdict written", data["verdict"] == "SAFE")
        check("assertions written", data["assertions"] == {"a": True, "b": False})


def main() -> int:
    for fn in (
        smoke_external_world_is_atomic_and_honest,
        smoke_crash_is_not_swallowed,
        smoke_crashing_storage_is_a_real_sqlite_storage,
        smoke_counts_are_absolute_from_creation,
        smoke_crash_before_commit_leaves_no_partial_write,
        smoke_crash_after_a_write_leaves_it_durable,
        smoke_worker_completes_without_a_crash,
        smoke_crash_after_claim_before_effect,
        smoke_crash_after_effect_before_complete,
        smoke_determinism_three_runs_identical,
        smoke_evidence_writer_round_trips,
    ):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"HARNESS SMOKE FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("HARNESS SMOKE PASSED - harness is sound and deterministic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
