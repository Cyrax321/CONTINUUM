"""Race tests and crash-safety verification for atomic claims in storage.

Verifies:
1. 64 concurrent threads racing to claim the same action key produce exactly 1 winner
   and 0 duplicate executions.
2. 16 separate OS processes racing to claim the same action key produce exactly 1 winner
   and 0 duplicate executions.
3. Multi-round stress concurrency tests maintain 0 duplicates across all rounds.
4. Fault injection: kill -9 between claim and complete leaves the action safely unresolved,
   preventing duplicate executions on subsequent recovery attempts.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger, LedgerError
from continuum.models import ActionStatus, Run, UnknownSideEffect
from continuum.storage import SQLiteStorage


def _seed(db: Path, run_id: str = "run_1") -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id=run_id, goal="concurrency testing"))


def test_64_threads_racing_claim_zero_duplicates(tmp_path: Path) -> None:
    """64 threads racing on a single key must yield exactly 1 winner and 0 duplicates."""
    db = tmp_path / "race_64.db"
    _seed(db)

    def worker(worker_id: int) -> str:
        with SQLiteStorage(db) as store:
            ledger = ActionLedger(store, "run_1")
            try:
                outcome = ledger.claim("payment.charge", {"amount": 500}, key="inv-64")
                if outcome.fresh:
                    # Winner performs side effect and completes
                    ledger.complete(outcome.key, result={"tx_id": f"tx-{worker_id}"})
                    return "proceed"
                return "dedup"
            except UnknownSideEffect:
                return "ambiguous"

    with ThreadPoolExecutor(max_workers=64) as pool:
        outcomes = list(pool.map(worker, range(64)))

    proceed_count = outcomes.count("proceed")
    assert proceed_count == 1, f"Expected exactly 1 proceed, got {proceed_count} ({outcomes})"
    assert set(outcomes) <= {"proceed", "dedup", "ambiguous"}

    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_1")
        actions = ledger.all()
        assert len(actions) == 1
        action = actions[0]
        assert action.status is ActionStatus.COMPLETED
        assert action.result is not None and "tx_id" in action.result
        assert store.verify_events("run_1").ok


_PROCESS_WORKER_SCRIPT = """
import sys
from continuum.storage import SQLiteStorage
from continuum.actions.ledger import ActionLedger
from continuum.models import UnknownSideEffect

db_path, run_id, worker_id = sys.argv[1], sys.argv[2], sys.argv[3]

with SQLiteStorage(db_path) as store:
    ledger = ActionLedger(store, run_id)
    try:
        outcome = ledger.claim("order.process", {"order_id": "ORD-999"}, key="order-lock")
        if outcome.fresh:
            ledger.complete(outcome.key, result={"processed_by": worker_id})
            print("proceed")
        else:
            print("dedup")
    except UnknownSideEffect:
        print("ambiguous")
"""


def test_16_processes_racing_claim_zero_duplicates(tmp_path: Path) -> None:
    """16 real OS processes racing on the same key must produce exactly 1 winner."""
    db = tmp_path / "race_16_proc.db"
    _seed(db, "run_proc")

    script = tmp_path / "worker.py"
    script.write_text(textwrap.dedent(_PROCESS_WORKER_SCRIPT))

    env_path = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": env_path}

    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), "run_proc", f"proc-{i}"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(16)
    ]

    results = [proc.communicate() for proc in processes]

    outcomes = []
    for (stdout, stderr), proc in zip(results, processes, strict=True):
        assert proc.returncode == 0, f"Process failed with stderr: {stderr}"
        outcomes.append(stdout.strip())

    proceed_count = outcomes.count("proceed")
    assert proceed_count == 1, (
        f"Expected exactly 1 proceed across 16 processes, got {proceed_count} ({outcomes})"
    )
    assert set(outcomes) <= {"proceed", "dedup", "ambiguous"}

    with SQLiteStorage(db) as store:
        assert store.verify_events("run_proc").ok


def test_atomic_claim_multiple_rounds_stress(tmp_path: Path) -> None:
    """Stress test: 100 rounds of 16 racing threads, verifying 0 duplicates across all rounds."""
    rounds = int(os.environ.get("CONTINUUM_STRESS_ROUNDS", "100"))

    for r in range(rounds):
        db = tmp_path / f"stress_{r}.db"
        _seed(db, f"run_stress_{r}")

        def worker(worker_id: int, current_db: Path = db, current_r: int = r) -> str:
            with SQLiteStorage(current_db) as store:
                ledger = ActionLedger(store, f"run_stress_{current_r}")
                try:
                    outcome = ledger.claim(
                        "resource.allocate", {"unit": current_r}, key=f"key-{current_r}"
                    )
                    if outcome.fresh:
                        try:
                            ledger.complete(outcome.key, result={"worker": worker_id})
                            return "proceed"
                        except LedgerError:
                            # A racing thread claimed before complete, escalating to UNKNOWN
                            return "ambiguous"
                    return "dedup"
                except (UnknownSideEffect, LedgerError):
                    return "ambiguous"

        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(worker, range(16)))

        proceed_count = outcomes.count("proceed")
        assert proceed_count <= 1, (
            f"Round {r} produced {proceed_count} duplicate winners: {outcomes}"
        )

        with SQLiteStorage(db) as store:
            ledger = ActionLedger(store, f"run_stress_{r}")
            actions = ledger.all()
            assert len(actions) == 1, f"Round {r} created {len(actions)} actions instead of 1"


_CRASH_WORKER_SCRIPT = """
import sys
import time
from continuum.storage import SQLiteStorage
from continuum.actions.ledger import ActionLedger

db_path, run_id, key = sys.argv[1], sys.argv[2], sys.argv[3]

with SQLiteStorage(db_path) as store:
    ledger = ActionLedger(store, run_id)
    outcome = ledger.claim("transfer.wire", {"cents": 100000}, key=key)
    if outcome.fresh:
        # Signal parent that claim was acquired in storage
        sys.stdout.write("CLAIM_ACQUIRED\\n")
        sys.stdout.flush()
        # Pause to simulate side effect in progress before complete()
        time.sleep(30)
        ledger.complete(outcome.key, result={"transferred": True})
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is Unix-specific")
def test_kill_9_between_claim_and_complete_prevents_duplicate(tmp_path: Path) -> None:
    """kill -9 between claim and complete leaves the attempt unresolved; subsequent claims must not duplicate."""
    db = tmp_path / "crash_test.db"
    _seed(db, "run_crash")

    script = tmp_path / "crash_worker.py"
    script.write_text(textwrap.dedent(_CRASH_WORKER_SCRIPT))

    env_path = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": env_path}

    proc = subprocess.Popen(
        [sys.executable, str(script), str(db), "run_crash", "wire-101"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for the child to successfully claim the action
    line = proc.stdout.readline().strip() if proc.stdout else ""
    assert line == "CLAIM_ACQUIRED", (
        f"Child failed to signal claim: {proc.stderr.read() if proc.stderr else ''}"
    )

    # Kill the process with SIGKILL (kill -9) before complete() is ever reached
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    # Verify process was terminated by signal
    assert proc.returncode in (-signal.SIGKILL, 137, -9)

    # A recovering agent now attempts to claim the same wire transfer
    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_crash")

        # The prior attempt was killed mid-flight: it must NOT return fresh=True
        with pytest.raises(UnknownSideEffect) as excinfo:
            ledger.claim("transfer.wire", {"cents": 100000}, key="wire-101")

        assert "was interrupted before its outcome was recorded" in str(excinfo.value)

        # Pending reflects the unresolved action
        pending = ledger.pending()
        assert len(pending) == 1
        assert pending[0].action_type == "transfer.wire"
        assert pending[0].status is ActionStatus.UNKNOWN

        # Reconciling resolves the interrupted state safely
        reconciled = ledger.reconcile("wire-101", occurred=True, note="verified via bank API")
        assert reconciled.status is ActionStatus.COMPLETED

        # After reconciliation, subsequent claim returns the deduplicated completed result
        final_outcome = ledger.claim("transfer.wire", {"cents": 100000}, key="wire-101")
        assert not final_outcome.fresh
        assert final_outcome.already_completed
        assert store.verify_events("run_crash").ok
