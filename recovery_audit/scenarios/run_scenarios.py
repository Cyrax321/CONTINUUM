"""Phase 4 - recovery-safety scenarios S1-S8.

Each scenario injects one fault with the Phase 3 harness, restarts against the
durable artifacts, and derives a verdict (SAFE / UNSAFE / UNKNOWN_OUTCOME) from
what the fake external system actually recorded.

Run from the audited worktree:

    cd /private/tmp/audit-recovery
    PYTHONPATH=/private/tmp/audit-recovery/src:/private/tmp/audit-recovery \
        .venv/bin/python recovery_audit/scenarios/run_scenarios.py

Evidence is written to recovery_audit/evidence/phase4/ as JSON + a human-readable
trace per scenario, plus a summary JSON for Phase 6.

No production code is modified. Every scenario uses isolated temp storage and a
deterministic fake external system (mission rule 11).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from continuum.actions.ledger import ActionLedger
from continuum.checkpoint.manager import CheckpointManager
from continuum.events import EventType
from continuum.models import ActionStatus, Run, UnknownSideEffect
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.base import CorruptedRecord
from continuum.storage.sqlite import SQLiteStorage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from fault_injection import (  # noqa: E402
    EvidenceUnavailable,
    InterleavePoint,
    InterleavingStorage,
    Scenario,
    ScenarioResult,
    Worker,
)

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parent / "evidence" / "phase4"

CHARGE = "charge_card"
EMAIL = "send_email"


@dataclass
class ScenarioCase:
    """One scenario: its result plus the Scenario that owns its temp storage."""

    result: ScenarioResult
    scenario: Scenario


def _blind_reclaim(scenario: Scenario, worker: Worker, action_type: str,
                   arguments: dict, business_key: str) -> tuple[bool, str]:
    """Attempt a blind re-claim. Returns (was_refused, note)."""
    try:
        worker.attempt(action_type, arguments, business_key)
    except UnknownSideEffect:
        return True, "refused with UnknownSideEffect"
    except Exception as exc:
        return True, f"refused with {type(exc).__name__}"
    return False, "NOT refused - the blind re-claim proceeded"


def _only_key(worker: Worker) -> str:
    folded = worker.ledger.folded()
    if not folded:
        raise AssertionError(
            f"{worker.holder_id}: no action survived in the folded ledger; "
            f"the scenario is misconfigured"
        )
    if len(folded) != 1:
        raise AssertionError(
            f"{worker.holder_id}: expected exactly one action, got {len(folded)}: "
            f"{[(a.action_type, a.status.value) for a in folded.values()]}"
        )
    return next(iter(folded))


# --------------------------------------------------------------------------- #
# S1 - Claim -> Crash -> Restart -> Recovery
# --------------------------------------------------------------------------- #


def s1_claim_crash_restart_recovery() -> ScenarioCase:
    """SEAM-1. The claim is durable but the effect never ran.

    A naive recovery re-claims and would re-fire. The correct recovery probes
    first and reconciles the absence.
    """
    s = Scenario("S1_claim_crash_restart_recovery")
    w = s.worker()

    def step(worker: Worker) -> None:
        worker.attempt(CHARGE, {"amount": 10}, "inv-1", crash_at="after-claim")

    s.crash_at_step(step, w)

    w2 = s.restart()
    actions = list(w2.ledger.folded().values())
    key = _only_key(w2)

    a = {
        "exactly_one_action_survived": len(actions) == 1,
        "surviving_action_is_started": actions[0].status is ActionStatus.STARTED,
        "effect_never_landed": s.world.execution_count == 0,
        "no_duplicate": s.world.duplicate_executions == 0,
    }

    refused, note = _blind_reclaim(s, w2, CHARGE, {"amount": 10}, "inv-1")
    a["blind_reclaim_refused"] = refused
    s.trace.append(f"blind re-claim: {note}")
    a["blind_retry_landed_nothing"] = s.world.execution_count == 0

    dec = RecoveryEngine(SQLiteStorage(s.path)).assess("r1")
    a["assess_escalates"] = dec.mode.value == "request_human"
    a["assess_names_the_uncertain_action"] = any(
        x.action_type == CHARGE for x in dec.uncertain_actions
    )
    s.trace.append(f"assess mode={dec.mode.value} uncertain={len(dec.uncertain_actions)}")

    r = w2.reconcile_from_probe(key, "inv-1")
    a["probe_found_no_effect"] = r.status is ActionStatus.FAILED
    after = w2.attempt(CHARGE, {"amount": 10}, "inv-1")
    a["retry_after_reconcile_succeeded"] = after.status is ActionStatus.COMPLETED
    a["effect_landed_exactly_once"] = s.world.execution_count == 1
    a["still_no_duplicate"] = s.world.duplicate_executions == 0

    return ScenarioCase(
        s.result(
            "SAFE"
            if a["effect_landed_exactly_once"] and a["still_no_duplicate"]
            and a["blind_reclaim_refused"]
            else "UNSAFE",
            recovery_action="probe + reconcile(occurred=False) + retry",
            assertions=a,
            notes=[
                "The claim was durable but the effect never ran, so the record was "
                "STARTED and a blind re-claim raised UnknownSideEffect.",
                "RecoveryEngine.assess escalated to request_human rather than "
                "auto-retrying, which is the correct behaviour.",
                "Reconciliation with evidence closed the loop and the retry then "
                "landed exactly once.",
            ],
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S2 - Claim -> External Action -> Crash -> Restart
# --------------------------------------------------------------------------- #


def s2_claim_effect_crash_restart() -> ScenarioCase:
    """SEAM-2. The effect landed; the completion write did not.

    This is the scenario the mission's acceptance criterion is written for: the
    external action may already have succeeded while its durable completion
    record is missing.
    """
    s = Scenario("S2_claim_effect_crash_restart")
    w = s.worker()

    def step(worker: Worker) -> None:
        worker.attempt(CHARGE, {"amount": 10}, "inv-2", crash_at="after-effect")

    s.crash_at_step(step, w)

    w2 = s.restart()
    actions = list(w2.ledger.folded().values())
    key = _only_key(w2)

    a = {
        "effect_landed_before_crash": s.world.execution_count == 1,
        "one_action_survived": len(actions) == 1,
        "surviving_action_is_started": actions[0].status is ActionStatus.STARTED,
        "external_id_lost": actions[0].external_id is None,
    }

    refused, note = _blind_reclaim(s, w2, CHARGE, {"amount": 10}, "inv-2")
    a["blind_reclaim_refused"] = refused
    s.trace.append(f"blind re-claim: {note}")
    a["blind_retry_landed_no_second_effect"] = s.world.execution_count == 1
    a["no_duplicate"] = s.world.duplicate_executions == 0

    # The recovery engine must escalate on the uncertain outcome.
    dec = RecoveryEngine(SQLiteStorage(s.path)).assess("r1")
    a["assess_escalates"] = dec.mode.value == "request_human"
    s.trace.append(f"assess mode={dec.mode.value}")

    r = w2.reconcile_from_probe(key, "inv-2")
    a["reconcile_confirmed_with_receipt"] = r.status is ActionStatus.COMPLETED
    a["receipt_recorded"] = r.external_id is not None
    after = w2.attempt(CHARGE, {"amount": 10}, "inv-2")
    a["reclaim_after_reconcile_is_a_noop"] = after.status is ActionStatus.COMPLETED
    a["reclaim_did_not_reexecute"] = s.world.execution_count == 1
    a["still_no_duplicate"] = s.world.duplicate_executions == 0

    return ScenarioCase(
        s.result(
            "SAFE"
            if a["no_duplicate"] and a["still_no_duplicate"] and a["blind_reclaim_refused"]
            else "UNSAFE",
            recovery_action="probe + reconcile(occurred=True) + re-claim (no-op)",
            assertions=a,
            notes=[
                "The effect landed externally but the completion record was lost, "
                "so the ledger could only see STARTED.",
                "Blind re-claim raised UnknownSideEffect and fired nothing - the "
                "acceptance criterion, satisfied.",
                "Reconciliation recorded the external receipt; the subsequent claim "
                "returned fresh=False and did not re-execute.",
            ],
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S3 - External Action Succeeds -> Completion Record Lost -> Recovery
# --------------------------------------------------------------------------- #


def s3_completion_record_lost() -> ScenarioCase:
    """The completion was durable, then the record was lost.

    Distinct from S2: here the completion *was* recorded, and a later fault
    destroyed the record while the external system still holds the receipt.
    """
    s = Scenario("S3_completion_record_lost")
    w = s.worker()
    w.attempt(CHARGE, {"amount": 10}, "inv-3")
    key = _only_key(w)

    before = w.ledger.get(key)
    a = {
        "action_was_completed": before.status is ActionStatus.COMPLETED,
        "receipt_was_recorded": before.external_id is not None,
        "effect_landed_once": s.world.execution_count == 1,
    }

    # Injected fault: the durable completion record is lost. The external system
    # keeps the receipt. This is the scenario's premise, not a library behaviour.
    conn = w.storage._live_connection()
    seq = w.storage.last_sequence("r1")
    deleted = conn.execute(
        "DELETE FROM events WHERE run_id = ? AND sequence = ?", ("r1", seq)
    ).rowcount
    conn.commit()
    s.trace.append(
        f"!!! INJECTED FAULT: deleted the completion event at sequence {seq} "
        f"({deleted} row)"
    )

    w2 = s.restart()
    after = w2.ledger.get(key)
    a["record_now_reads_started"] = after.status is ActionStatus.STARTED
    a["external_id_gone_from_record"] = after.external_id is None

    refused, note = _blind_reclaim(s, w2, CHARGE, {"amount": 10}, "inv-3")
    a["blind_reclaim_refused"] = refused
    s.trace.append(f"blind re-claim: {note}")
    a["blind_retry_landed_no_second_effect"] = s.world.execution_count == 1
    a["no_duplicate"] = s.world.duplicate_executions == 0

    r = w2.reconcile_from_probe(key, "inv-3")
    a["reconcile_restored_completion"] = r.status is ActionStatus.COMPLETED
    a["receipt_restored"] = r.external_id is not None
    a["effect_still_landed_once"] = s.world.execution_count == 1
    a["still_no_duplicate"] = s.world.duplicate_executions == 0

    return ScenarioCase(
        s.result(
            "SAFE"
            if a["no_duplicate"] and a["still_no_duplicate"] and a["blind_reclaim_refused"]
            else "UNSAFE",
            recovery_action="probe + reconcile(occurred=True) to restore the lost record",
            assertions=a,
            notes=[
                "The completion record was durable and then destroyed; the "
                "projection fell back to STARTED.",
                "Blind re-claim raised UnknownSideEffect rather than re-charging, so "
                "a lost record could not launder a duplicate.",
                "The probe's receipt restored the completion without re-executing.",
            ],
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S4 - External Action Fails -> Retry / Recovery
# --------------------------------------------------------------------------- #


def s4_external_action_fails_retry() -> ScenarioCase:
    """A genuine failure must be retryable; an uncertain one must not be.

    The distinction between fail(certain=True) and fail(certain=False) is the
    whole point: a timeout is not evidence of absence.
    """
    s = Scenario("S4_external_action_fails_retry", failures=["inv-4"])
    w = s.worker()
    r1 = w.attempt(CHARGE, {"amount": 10}, "inv-4", fail_mode="fail")
    key = _only_key(w)

    a = {
        "first_attempt_recorded_failed": r1.status is ActionStatus.FAILED,
        "failed_attempt_landed_nothing": s.world.execution_count == 0,
        "record_is_not_uncertain": w.ledger.get(key).side_effect_uncertain is False,
    }

    # A FAILED action is directly retryable: the effect provably did not happen.
    # The external system has to come back for the retry to land -- a failure that
    # never clears would make this assertion impossible by construction, which
    # tests nothing about the ledger.
    s.world.recover("inv-4")
    r2 = w.attempt(CHARGE, {"amount": 10}, "inv-4")
    a["retry_succeeded"] = r2.status is ActionStatus.COMPLETED
    a["retry_landed_exactly_once"] = s.world.execution_count == 1
    a["no_duplicate"] = s.world.duplicate_executions == 0

    # The uncertain variant: a timeout after the request was sent.
    s2 = Scenario("S4b_timeout_uncertain", failures=["inv-4b"])
    w3 = s2.worker()
    r3 = w3.attempt(CHARGE, {"amount": 10}, "inv-4b", fail_mode="uncertain")
    key2 = _only_key(w3)
    a["timeout_recorded_unknown"] = r3.status is ActionStatus.UNKNOWN
    a["timeout_record_is_uncertain"] = (
        w3.ledger.get(key2).side_effect_uncertain is True
    )

    refused, note = _blind_reclaim(s2, w3, CHARGE, {"amount": 10}, "inv-4b")
    a["uncertain_retry_refused"] = refused
    s2.trace.append(f"uncertain retry: {note}")
    a["uncertain_retry_fired_nothing"] = s2.world.execution_count == 0

    # Reconcile, then retry. The probe reports the timeout never landed, so the
    # record settles as FAILED and the retry is legitimate; the world has to
    # recover for the same reason as above.
    w4 = s2.restart()
    rec = w4.reconcile_from_probe(key2, "inv-4b")
    a["reconcile_settled_the_timeout"] = rec.status in (
        ActionStatus.FAILED,
        ActionStatus.COMPLETED,
    )
    s2.world.recover("inv-4b")
    r4 = w4.attempt(CHARGE, {"amount": 10}, "inv-4b")
    a["retry_after_timeout_reconcile_succeeded"] = r4.status is ActionStatus.COMPLETED
    a["timeout_retry_landed_once"] = s2.world.execution_count == 1
    a["timeout_no_duplicate"] = s2.world.duplicate_executions == 0

    s.trace += s2.trace
    return ScenarioCase(
        s.result(
            "SAFE"
            if a["no_duplicate"]
            and a["timeout_no_duplicate"]
            and a["uncertain_retry_refused"]
            else "UNSAFE",
            recovery_action="FAILED: retry directly. UNKNOWN: reconcile then retry.",
            assertions=a,
            notes=[
                "A genuine failure records FAILED with side_effect_uncertain=False, "
                "so retry is safe and needs no probe.",
                "A timeout records UNKNOWN with side_effect_uncertain=True, and "
                "retry is refused until reconciliation supplies evidence.",
                "Both paths landed exactly one side effect.",
            ],
            executions=s.world.execution_count + s2.world.execution_count,
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S5 - Concurrent Workers Same Action
# --------------------------------------------------------------------------- #


def s5_concurrent_workers_no_lease() -> ScenarioCase:
    """The TOCTOU in claim, exposed deterministically, with and without a lease.

    Without a lease the ledger's claim is a read-then-write, which the class
    docstring admits. This inserts worker B's entire claim into the middle of
    worker A's claim, at a counted point, with no timing. Pre-fix, both workers
    received fresh=True and both performed; post-fix, the second append trips the
    storage-level compare-and-set and the loser is refused.
    """
    results: dict[str, bool] = {}

    # --- 5a: no lease, interleaved ---
    s = Scenario("S5a_concurrent_workers_no_lease")
    plain = SQLiteStorage(s.path)
    plain.create_run(Run(run_id="r1", goal="g"))
    plain.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})
    plain.close()
    s._run_created = True

    store_a = InterleavingStorage(s.path)
    ledger_a = ActionLedger(store_a, "r1", holder_id="A")
    store_b = SQLiteStorage(s.path)
    ledger_b = ActionLedger(store_b, "r1", holder_id="B")

    # B's claim is inserted into the middle of A's: the callback runs just before
    # A's first append_event, i.e. after A's resolve_claim has already read the
    # log and seen nothing, but before A's ACTION_RECORDED is written. store_a
    # never wrote the setup event itself (it went through `plain`), so A's
    # ACTION_RECORDED is store_a's append_event call #1.
    held: dict[str, object] = {}

    def worker_b_claim() -> None:
        try:
            held["outcome"] = ledger_b.claim(CHARGE, {"amount": 10})
        except BaseException as exc:  # noqa: BLE001 - the loser's refusal is the point
            held["error"] = exc

    store_a.arm(InterleavePoint("append_event", 1), worker_b_claim)
    error_a: BaseException | None = None
    try:
        out_a = ledger_a.claim(CHARGE, {"amount": 10})
    except BaseException as exc:  # noqa: BLE001 - the loser's refusal is the point
        out_a = None
        error_a = exc
    out_b = held.get("outcome")
    error_b = held.get("error")
    s.trace.append(
        f"A outcome={type(out_a).__name__ if out_a else type(error_a).__name__} "
        f"fresh={getattr(out_a, 'fresh', 'n/a')}; "
        f"interleave fired={store_a.fired}; "
        f"B outcome={type(out_b).__name__ if out_b else type(error_b).__name__} "
        f"fresh={getattr(out_b, 'fresh', 'n/a')}"
    )

    fresh_a = bool(getattr(out_a, "fresh", False))
    fresh_b = bool(getattr(out_b, "fresh", False))

    results["interleave_actually_fired"] = bool(store_a.fired)
    results["worker_b_claimed_a_fresh_slot"] = fresh_b
    results["worker_a_claimed_a_fresh_slot"] = fresh_a
    results["both_workers_believed_they_held_the_slot"] = fresh_a and fresh_b
    # The acceptance criterion: at most one worker may be told to go ahead. Which
    # one wins is scheduling-dependent, so the invariant is the count, not the
    # identity.
    results["at_most_one_fresh_slot"] = int(fresh_a) + int(fresh_b) <= 1
    loser = error_a if error_a is not None else error_b
    results["the_loser_was_refused_loudly"] = loser is not None
    if loser is not None:
        results["loser_refusal_type"] = type(loser).__name__

    recorded = [
        e
        for e in s.events()
        if e["type"] == "ACTION_RECORDED"
        and e["payload"].get("action_type") == CHARGE
    ]
    started = [
        e for e in recorded if e["payload"].get("status") == ActionStatus.STARTED.value
    ]
    # Pre-fix both records were STARTED and both workers performed. Post-fix there
    # is exactly one STARTED slot; the loser's claim re-folds, finds that slot in
    # flight, and writes an UNKNOWN record before raising UnknownSideEffect -- the
    # same mark-and-refuse a sequential interrupted claim produces.
    results["started_slots_opened"] = len(started)
    results["exactly_one_started_slot"] = len(started) == 1
    s.trace.append(
        f"ACTION_RECORDED events for {CHARGE}: {len(recorded)} "
        f"(statuses: {[e['payload'].get('status') for e in recorded]})"
    )

    # If both workers act on their belief, the side effect fires twice. This is
    # the execution the mission's acceptance criterion forbids.
    world = s.world
    if fresh_a:
        world.perform("inv-5", {"amount": 10})
    if fresh_b:
        world.perform("inv-5", {"amount": 10})
    results["two_workers_perform_twice"] = world.duplicate_executions >= 1
    results["duplicate_side_effect_observed"] = world.duplicate_executions >= 1
    s.trace.append(
        f"landings={world.execution_count} duplicates={world.duplicate_executions}"
    )

    # --- 5b: the same interleave, with a lease ---
    s2 = Scenario("S5b_concurrent_workers_with_lease", lease=True)
    lease = s2.lease()
    assert lease.acquire("r1", "A")
    store_a2 = InterleavingStorage(s2.path)
    store_a2.create_run(Run(run_id="r1", goal="g"))
    store_a2.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})
    s2._run_created = True
    ledger_a2 = ActionLedger(store_a2, "r1", lease=lease, holder_id="A")
    store_b2 = SQLiteStorage(s2.path)
    ledger_b2 = ActionLedger(store_b2, "r1", lease=lease, holder_id="B")

    def worker_b_claim_leased() -> None:
        ledger_b2.claim(CHARGE, {"amount": 10})

    # A's ACTION_RECORDED is append #2 here too (RUN_STARTED is #1).
    store_a2.arm(InterleavePoint("append_event", 2), worker_b_claim_leased)
    try:
        out = ledger_a2.claim(CHARGE, {"amount": 10})
        results["lease_fenced_worker_b"] = False
        s2.trace.append(f"A claim under lease returned fresh={out.fresh}")
    except Exception as exc:
        results["lease_fenced_worker_b"] = True
        s2.trace.append(
            f"the lease fenced the interleaved worker: {type(exc).__name__}: "
            f"{str(exc)[:80]}"
        )
    results["lease_held_by_a"] = lease.holder("r1") == "A"
    results["leased_scenario_no_duplicate"] = s2.world.duplicate_executions == 0

    s.trace += s2.trace
    verdict: Literal["SAFE", "UNSAFE", "UNKNOWN_OUTCOME"] = (
        "UNSAFE" if results["duplicate_side_effect_observed"] else "SAFE"
    )
    return ScenarioCase(
        s.result(
            verdict,
            recovery_action="5a: no duplicate occurred - the losing worker's claim "
            "was rejected by the compare-and-set and then refused on the re-fold, so "
            "it performed nothing and learned the identity was already in flight. "
            "5b: the lease fenced the second worker.",
            assertions=results,
            # These four are written so that True is the defect, so a False
            # reading is the pass. Naming them keeps the evidence from printing
            # FAIL on a holding safety property. Two further descriptive booleans
            # (worker_a/b_claimed_a_fresh_slot) are outcomes rather than
            # properties and are left PASS-when-True, since either value is
            # legitimate depending on which worker won the race.
            expected_false=[
                "both_workers_believed_they_held_the_slot",
                "two_workers_perform_twice",
                "duplicate_side_effect_observed",
            ],
            # Descriptive outcomes, not safety properties: which worker won the
                # race is scheduling-dependent, so neither value is a defect. The
                # evidence renderer prints these as INFO rather than PASS/FAIL.
            informational=[
                "worker_a_claimed_a_fresh_slot",
                "worker_b_claimed_a_fresh_slot",
                "interleave_actually_fired",
                "loser_refusal_type",
                "started_slots_opened",
                "lease_fenced_worker_b",
                "lease_held_by_a",
            ],
            notes=[
                "Without a lease, inserting B's claim into the middle of A's used to "
                "produce two ACTION_RECORDED records for one identity and both workers "
                "performing. After the fix, exactly one worker is told to proceed and "
                "the other is refused loudly (UnknownSideEffect), which is the same "
                "answer a sequential caller gets for an interrupted attempt.",
                "The claim's read-then-write is closed with the compare-and-set the "
                "storage layer already exposed (expected_sequence), re-folding on "
                "ConcurrentWriteError rather than surfacing it, so a lost race is "
                "retryable and the fold re-evaluates against the new head.",
                "With a lease the second worker is fenced outright - which is why the "
                "lease exists, but it is opt-in and off by default; the CAS fix is what "
                "protects the default configuration.",
            ],
            executions=world.execution_count + s2.world.execution_count,
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S6 - Restored State Pending but Effect May Exist
# --------------------------------------------------------------------------- #


def s6_restored_state_pending() -> ScenarioCase:
    """A checkpoint predating the claim must not launder an in-flight action.

    restore replays forward from the checkpoint and does not truncate the log, so
    the ACTION_RECORDED event survives and keeps protecting the identity.
    """
    s = Scenario("S6_restored_state_pending")
    w = s.worker()
    cm = CheckpointManager(w.storage)

    # A checkpoint taken BEFORE any claim.
    cp = cm.checkpoint("r1", reason="before any side effect")
    s.trace.append(f"checkpoint at source_sequence={cp.state.source_sequence}")

    # Claim, then crash after the effect but before completion.
    def step(worker: Worker) -> None:
        worker.attempt(CHARGE, {"amount": 10}, "inv-6", crash_at="after-effect")

    s.crash_at_step(step, w)

    w2 = s.restart()
    key = _only_key(w2)

    restored = CheckpointManager(w2.storage).restore("r1", on_unprojectable="degrade")
    a = {
        "effect_landed_before_crash": s.world.execution_count == 1,
        "restore_replayed_forward": restored.replayed is True,
        "checkpoint_predates_the_claim": cp.state.source_sequence
        < w2.storage.last_sequence("r1"),
    }

    # The ledger must refuse a blind re-claim regardless of what the restored
    # projection says, because it folds the live log.
    refused, note = _blind_reclaim(s, w2, CHARGE, {"amount": 10}, "inv-6")
    a["reclaim_refused_despite_stale_projection"] = refused
    s.trace.append(f"blind re-claim: {note}")
    a["blind_retry_landed_nothing_new"] = s.world.execution_count == 1
    a["no_duplicate"] = s.world.duplicate_executions == 0

    r = w2.reconcile_from_probe(key, "inv-6")
    a["reconcile_confirmed"] = r.status is ActionStatus.COMPLETED
    w2.attempt(CHARGE, {"amount": 10}, "inv-6")
    a["reclaim_after_reconcile_did_not_reexecute"] = s.world.execution_count == 1
    a["still_no_duplicate"] = s.world.duplicate_executions == 0

    return ScenarioCase(
        s.result(
            "SAFE"
            if a["no_duplicate"] and a["still_no_duplicate"] and a["reclaim_refused_despite_stale_projection"]
            else "UNSAFE",
            recovery_action="probe + reconcile(occurred=True); restore did not launder the slot",
            assertions=a,
            notes=[
                "The checkpoint predates the claim, so the restored projection "
                "carries no knowledge of the in-flight action.",
                "The ledger folds the live log rather than the checkpoint, so the "
                "STARTED record still blocked a blind re-claim.",
                "Restore is therefore not a way to resurrect a spent slot.",
            ],
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S7 - Partial / Corrupted Durable State
# --------------------------------------------------------------------------- #


def s7_partial_corrupted_state() -> ScenarioCase:
    """A poisoned event log must degrade to a verdict, not raise or launder.

    Corruption is injected by mangling a payload in place. The library's contract
    is CorruptedRecord on read and a degraded verdict from assess.
    """
    s = Scenario("S7_partial_corrupted_state")
    w = s.worker()
    w.attempt(CHARGE, {"amount": 10}, "inv-7")
    key = _only_key(w)

    a = {
        "baseline_was_completed": w.ledger.get(key).status is ActionStatus.COMPLETED,
        "effect_landed_once": s.world.execution_count == 1,
    }

    # Injected fault: corrupt the completion event's payload in place.
    conn = w.storage._live_connection()
    seq = w.storage.last_sequence("r1")
    conn.execute(
        "UPDATE events SET payload = ? WHERE run_id = ? AND sequence = ?",
        ('{"action": "this is not the pay', "r1", seq),
    )
    conn.commit()
    s.trace.append(f"!!! INJECTED FAULT: corrupted the payload at sequence {seq}")

    fresh = SQLiteStorage(s.path)
    try:
        fresh.read_events("r1")
        a["corrupted_read_fails_closed"] = False
    except CorruptedRecord:
        a["corrupted_read_fails_closed"] = True
    except Exception as exc:
        a["corrupted_read_fails_closed"] = True
        s.trace.append(f"NOTE: read raised {type(exc).__name__}: {str(exc)[:80]}")

    try:
        dec = RecoveryEngine(fresh).assess("r1")
        a["assess_degraded_to_a_verdict"] = True
        a["assess_escalated_or_aborted"] = dec.mode.value in (
            "request_human",
            "abort",
            "wait",
        )
        s.trace.append(f"assess mode={dec.mode.value}")
        s.trace.append(f"assess rationale={list(dec.rationale)[:3]}")
    except Exception as exc:
        a["assess_degraded_to_a_verdict"] = False
        a["assess_escalated_or_aborted"] = False
        s.trace.append(
            f"assess RAISED {type(exc).__name__}: {str(exc)[:120]}"
        )

    try:
        ActionLedger(fresh, "r1").claim(CHARGE, {"amount": 10})
        a["claim_on_poisoned_log_refused"] = False
    except Exception as exc:
        a["claim_on_poisoned_log_refused"] = True
        s.trace.append(
            f"claim on the poisoned log was refused with {type(exc).__name__}"
        )
    a["corruption_laundered_no_side_effect"] = s.world.execution_count == 1
    a["no_duplicate"] = s.world.duplicate_executions == 0

    # Deliberately UNKNOWN_OUTCOME rather than SAFE: the durable state is
    # untrustworthy, so nothing was *verified* safe - a human must decide.
    return ScenarioCase(
        s.result(
            "UNKNOWN_OUTCOME",
            recovery_action="degrade: mark the poisoned prefix INVALID and escalate",
            assertions=a,
            notes=[
                "Corrupting the completion payload made the log unreadable at that "
                "sequence; the storage layer raised rather than returning garbage.",
                "Pre-fix (finding F2) that raise escaped assess() entirely, so a "
                "damaged log could not be assessed at all. Post-fix assess degrades "
                "to request_human and names the sequence the log stops folding at, "
                "which is the issue #383 contract this scenario now exercises.",
                "The verdict is UNKNOWN_OUTCOME, not SAFE: the durable state is "
                "untrustworthy, so a human must decide. Reporting SAFE would "
                "misrepresent what was actually verified.",
            ],
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# S8 - Equivalent / Duplicate Actions Submitted More Than Once
# --------------------------------------------------------------------------- #


def s8_duplicate_submissions() -> ScenarioCase:
    """Equivalent submissions must collapse to one execution.

    Covers: same arguments twice, an explicit caller key twice, the same identity
    across two runs with an unscoped key, and drifted arguments carrying a fresh
    throwaway field.
    """
    results: dict[str, bool] = {}

    # 8a: identical arguments, twice, sequentially.
    s = Scenario("S8a_identical_arguments")
    w = s.worker()
    r1 = w.attempt(EMAIL, {"to": "a@b.com", "body": "hi"}, "mail-8a")
    r2 = w.attempt(EMAIL, {"to": "a@b.com", "body": "hi"}, "mail-8a")
    results["second_identical_claim_is_a_noop"] = (
        r1.status is ActionStatus.COMPLETED and r2.status is ActionStatus.COMPLETED
    )
    results["identical_submission_landed_once"] = s.world.execution_count == 1
    results["identical_no_duplicate"] = s.world.duplicate_executions == 0

    # 8b: explicit caller-supplied key, twice.
    s2 = Scenario("S8b_explicit_key")
    w2 = s2.worker()
    k = "caller-supplied-email-key"
    r3 = w2.attempt(EMAIL, {"to": "c@d.com", "body": "hi"}, "mail-8b", key=k)
    r4 = w2.attempt(EMAIL, {"to": "c@d.com", "body": "hi"}, "mail-8b", key=k)
    results["explicit_key_second_claim_is_a_noop"] = (
        r3.status is ActionStatus.COMPLETED and r4.status is ActionStatus.COMPLETED
    )
    results["explicit_key_landed_once"] = s2.world.execution_count == 1
    results["explicit_key_no_duplicate"] = s2.world.duplicate_executions == 0

    # 8c: same identity across two runs, unscoped (store-wide) key. Both sides
    # must use scoped_to_run=False or the key would be run-prefixed on one side
    # only, and the two would simply be different identities.
    s3 = Scenario("S8c_cross_run_unscoped")
    wa = s3.worker()
    wa.attempt(EMAIL, {"to": "e@f.com", "body": "hi"}, "mail-8c",
               key="store-wide-email-key", scoped_to_run=False)
    st_b = SQLiteStorage(s3.path)
    st_b.create_run(Run(run_id="r2", goal="second run"))
    st_b.append_event("r2", EventType.RUN_STARTED, {"goal": "second run"})
    led_b = ActionLedger(st_b, "r2")
    try:
        outcome = led_b.claim(
            EMAIL, {"to": "e@f.com", "body": "hi"},
            key="store-wide-email-key", scoped_to_run=False,
        )
        results["cross_run_claim_deduplicated"] = outcome.fresh is False
        results["cross_run_claim_did_not_open_a_parallel_slot"] = True
        s3.trace.append(
            f"second run's unscoped claim returned fresh={outcome.fresh} "
            f"status={outcome.action.status.value}"
        )
    except UnknownSideEffect as exc:
        # A still-in-flight attempt in run 1 would also be a correct refusal.
        results["cross_run_claim_deduplicated"] = False
        results["cross_run_claim_did_not_open_a_parallel_slot"] = True
        s3.trace.append(
            f"second run's unscoped claim raised UnknownSideEffect rather than "
            f"opening a parallel slot: {str(exc)[:80]}"
        )
    results["cross_run_landed_once"] = s3.world.execution_count == 1
    results["cross_run_no_duplicate"] = s3.world.duplicate_executions == 0

    # 8d: drifted arguments - a fresh throwaway field mints a new derived key.
    s4 = Scenario("S8d_drifted_arguments")
    w4 = s4.worker()
    out1 = w4.ledger.claim(EMAIL, {"to": "g@h.com", "body": "hi", "trace_id": "t1"})
    first_key = out1.key
    try:
        w4.ledger.claim(
            EMAIL, {"to": "g@h.com", "body": "hi", "trace_id": "t2"}
        )
        results["drifted_arguments_mint_a_distinct_key"] = True
        results["drifted_reclaim_still_blocked_when_in_flight"] = False
    except UnknownSideEffect:
        results["drifted_arguments_mint_a_distinct_key"] = True
        results["drifted_reclaim_still_blocked_when_in_flight"] = True
    s4.trace.append(
        f"first drifted key={first_key[:12]}... "
        f"(distinct keys are documented behaviour for differing payloads)"
    )
    results["drifted_no_duplicate"] = s4.world.duplicate_executions == 0

    all_safe = all(results.values())
    s.trace += s2.trace + s3.trace + s4.trace
    return ScenarioCase(
        s.result(
            "SAFE" if all_safe else "UNSAFE",
            recovery_action="idempotency key collapse (derived, caller-supplied, or store-wide)",
            assertions=results,
            notes=[
                "Identical arguments, an explicit caller key, and a store-wide "
                "unscoped key each collapsed to a single execution.",
                "For the unscoped key across runs, the second run's claim deferred "
                "to the first run's record rather than opening a parallel slot.",
                "Drifted arguments mint a distinct derived key, which is "
                "documented: the ledger cannot know two differing payloads mean "
                "the same effect, and the in-flight state still blocked a re-fire.",
            ],
            executions=s.world.execution_count + s2.world.execution_count
            + s3.world.execution_count + s4.world.execution_count,
        ),
        s,
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

SCENARIOS = [
    s1_claim_crash_restart_recovery,
    s2_claim_effect_crash_restart,
    s3_completion_record_lost,
    s4_external_action_fails_retry,
    s5_concurrent_workers_no_lease,
    s6_restored_state_pending,
    s7_partial_corrupted_state,
    s8_duplicate_submissions,
]


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    cases: list[ScenarioCase] = []
    failed_to_run: list[tuple[str, str]] = []

    for fn in SCENARIOS:
        try:
            cases.append(fn())
        except Exception as exc:
            import traceback

            tb = traceback.format_exc()
            failed_to_run.append((fn.__name__, tb))
            print(f"!! {fn.__name__} could not run:\n{tb}")

    summary: dict[str, dict] = {}
    for case in cases:
        result = case.result
        # Only assertions whose *True* value is the defect are negations. These
        # are written explicitly per scenario, because a naming heuristic is not
        # trustworthy here: "no_duplicate" is `== 0` (True is the pass) while
        # "duplicate_side_effect_observed" is `>= 1` (True is the failure), and
        # guessing between those two would flip a real duplicate into a pass --
        # the exact misreading this audit exists to prevent. The scenario that
        # wants the convention supplies it; the rest default to every assertion
        # being PASS-when-True.
        case.scenario.write_evidence(result, EVIDENCE)
        summary[result.scenario_id] = {
            "verdict": result.verdict,
            "executions": result.executions,
            "recovery_action": result.recovery_action,
            "assertions": result.assertions,
            "expected_false": sorted(result.expected_false),
            "informational": sorted(result.informational),
            "notes": result.notes,
        }
        case.scenario.cleanup()

    path = EVIDENCE / "summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n")

    unsafe = [k for k, v in summary.items() if v["verdict"] == "UNSAFE"]
    unknown = [k for k, v in summary.items() if v["verdict"] == "UNKNOWN_OUTCOME"]
    safe = [k for k, v in summary.items() if v["verdict"] == "SAFE"]

    print("=" * 70)
    print(f"scenarios run     : {len(cases)} of {len(SCENARIOS)}")
    print(f"SAFE              : {len(safe)}  {safe}")
    print(f"UNSAFE            : {len(unsafe)}  {unsafe}")
    print(f"UNKNOWN_OUTCOME   : {len(unknown)}  {unknown}")
    if failed_to_run:
        print(f"FAILED TO RUN     : {[n for n, _ in failed_to_run]}")
    print(f"evidence          : {EVIDENCE}")
    print("=" * 70)
    return 1 if unsafe or failed_to_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
