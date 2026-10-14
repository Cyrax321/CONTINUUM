"""Phase 2 verification scripts - evidence for ARCHITECTURE.md claims.

Each function reproduces one empirical claim made in recovery_audit/ARCHITECTURE.md.
Run from the reference worktree:

    cd /private/tmp/audit-recovery
    PYTHONPATH=/private/tmp/audit-recovery/src .venv/bin/python \
        recovery_audit/evidence/phase2/verify_architecture.py

NOTE: this file lives in the audit artifact tree, NOT in the repo's tests/ directory.
It is a verification script, not a pytest module.
"""

from __future__ import annotations

import datetime
import os
import tempfile

from continuum.actions import ActionLedger, ActionOutcome, ClaimLockError
from continuum.concurrency.lease import InMemoryLeaseCoordinator
from continuum.events import EventType
from continuum.models import ActionStatus, Run, UnknownSideEffect
from continuum.storage import SQLiteStorage

LINE = "=" * 72


def _store() -> tuple[str, SQLiteStorage]:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "audit.db")
    st = SQLiteStorage(path)
    st.create_run(Run(run_id="r1", goal="g"))
    st.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})
    return path, st


def v1_reclaim_of_started_raises() -> None:
    """ARCHITECTURE.md §2.2: re-claiming a STARTED action raises and marks UNKNOWN."""
    print(LINE)
    print("V1  re-claim of an interrupted action")
    _, st = _store()
    led = ActionLedger(st, "r1")
    out = led.claim("send_email", {"to": "a@b.com", "body": "hi"})
    print(f"  1st claim fresh={out.fresh} status={out.action.status.value} key={out.key[:12]}")
    try:
        ActionLedger(st, "r1").claim("send_email", {"to": "a@b.com", "body": "hi"})
        raise SystemExit("  FAIL: no exception raised")
    except UnknownSideEffect as e:
        print(f"  2nd claim raised UnknownSideEffect")
        print(f"  action_key={e.action_key and e.action_key[:12]} action_id={e.action_id[:12]}")
    pending = [(a.action_type, a.status.value) for a in ActionLedger(st, "r1").pending()]
    print(f"  pending() -> {pending}")
    assert pending == [("send_email", "unknown")], pending
    print("  PASS")


def v2_lease_fences_second_worker() -> None:
    """ARCHITECTURE.md §6.2: an externally-held run lease fences a second worker."""
    print(LINE)
    print("V2  externally-held lease fences a second worker")
    _, st = _store()
    lease = InMemoryLeaseCoordinator()
    assert lease.acquire("r1", "worker-A")
    a = ActionLedger(st, "r1", lease=lease, holder_id="worker-A")
    o = a.claim("charge_card", {"amount": "10"})
    print(f"  A claimed fresh={o.fresh}; lease holder now={lease.holder('r1')}")
    try:
        ActionLedger(st, "r1", lease=lease, holder_id="worker-B").claim(
            "charge_card", {"amount": "10"}
        )
        raise SystemExit("  FAIL: B was not fenced")
    except ClaimLockError as e:
        print(f"  B blocked by ClaimLockError: {str(e)[:70]}")
    print("  PASS")


def v3_lease_expiry_allows_takeover() -> None:
    """ARCHITECTURE.md §6.3: an expired lease is free and may be taken over."""
    print(LINE)
    print("V3  lease expiry allows takeover (no fencing token)")

    class Clock:
        def __init__(self) -> None:
            self.t = 0

        def __call__(self) -> datetime.datetime:
            return datetime.datetime(2026, 1, 1, 0, 0, self.t, tzinfo=datetime.timezone.utc)

    clock = Clock()
    lease = InMemoryLeaseCoordinator(
        clock=clock, default_ttl=datetime.timedelta(seconds=5)
    )
    assert lease.acquire("r1", "A")
    print(f"  A holds={lease.holder('r1')}")
    clock.t = 10
    print(f"  after expiry holder={lease.holder('r1')}")
    assert lease.holder("r1") is None
    assert lease.acquire("r1", "B")
    print("  B took over: True")
    print("  PASS")


def v4_status_guard_fences_without_lease() -> None:
    """ARCHITECTURE.md §2.3: the status-transition guard fences a stale write with no lease."""
    print(LINE)
    print("V4  status guard fences a stale completion with NO lease")
    _, st = _store()
    led = ActionLedger(st, "r1")
    o = led.claim("charge_card", {"amount": "10"})
    ActionLedger(st, "r1").reconcile(o.key, occurred=False, note="probe found no charge")
    status = ActionLedger(st, "r1").get(o.key).status
    print(f"  B reconciled not-occurred -> status={status.value}")
    try:
        ActionLedger(st, "r1").complete(o.key, external_id="ext-A", result={"ok": True})
        raise SystemExit("  FAIL: stale completion landed")
    except Exception as e:
        print(f"  A's late complete blocked by {type(e).__name__}: {str(e)[:90]}")
    print("  PASS")


def v5_reclaim_after_takeover_does_not_open_parallel_slot() -> None:
    """ARCHITECTURE.md §6.4: after takeover, the new holder cannot open a second slot."""
    print(LINE)
    print("V5  re-claim after lease expiry raises rather than duplicating the slot")

    class Clock:
        def __init__(self) -> None:
            self.t = 0

        def __call__(self) -> datetime.datetime:
            return datetime.datetime(2026, 1, 1, 0, 0, self.t, tzinfo=datetime.timezone.utc)

    clock = Clock()
    lease = InMemoryLeaseCoordinator(
        clock=clock, default_ttl=datetime.timedelta(seconds=5)
    )
    _, st = _store()
    lease.acquire("r1", "worker-A")
    a = ActionLedger(st, "r1", lease=lease, holder_id="worker-A")
    o = a.claim("charge_card", {"amount": "10"})
    print(f"  A claimed fresh={o.fresh}")
    clock.t = 10
    print(f"  holder after expiry={lease.holder('r1')}")
    lease.acquire("r1", "worker-B")
    try:
        ActionLedger(st, "r1", lease=lease, holder_id="worker-B").claim(
            "charge_card", {"amount": "10"}
        )
        raise SystemExit("  FAIL: a parallel slot was opened")
    except UnknownSideEffect as e:
        print(f"  B's re-claim raised UnknownSideEffect (no duplicate slot)")
    print("  PASS")


def v6_on_unknown_reopens_uncertain_action() -> None:
    """ARCHITECTURE.md §9 A5: a naive on_unknown callback reopens an uncertain action."""
    print(LINE)
    print("V6  a naive on_unknown callback reopens an uncertain action")
    _, st = _store()
    led = ActionLedger(st, "r1")
    o = led.claim("charge_card", {"amount": "10"})
    key = o.key

    def naive(existing):
        return ActionOutcome(
            key=key,
            action=existing.model_copy(
                update={"status": ActionStatus.FAILED, "side_effect_uncertain": False}
            ),
            fresh=True,
        )

    out = ActionLedger(st, "r1").claim("charge_card", {"amount": "10"}, on_unknown=naive)
    print(f"  on_unknown resolved -> fresh={out.fresh} status={out.action.status.value}")
    assert out.fresh is True
    print("  PASS (documents assumption A5: the callback is trusted, not checked)")


if __name__ == "__main__":
    for fn in (
        v1_reclaim_of_started_raises,
        v2_lease_fences_second_worker,
        v3_lease_expiry_allows_takeover,
        v4_status_guard_fences_without_lease,
        v5_reclaim_after_takeover_does_not_open_parallel_slot,
        v6_on_unknown_reopens_uncertain_action,
    ):
        fn()
    print(LINE)
    print("ALL PHASE-2 VERIFICATIONS PASSED")
