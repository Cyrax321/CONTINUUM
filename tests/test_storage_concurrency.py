"""Concurrency guarantees, exercised with real threads and real processes.

The claim under test is narrow and important: **two writers racing to append to
the same run never receive the same sequence number, and never silently
overwrite each other.** One wins, the other is told it lost.

These tests are the reason the engine takes an IMMEDIATE lock and keeps a
UNIQUE constraint on ``(run_id, sequence)``. Without both, a race produces a
forked chain that verifies clean, the worst possible failure, because it looks
correct.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Progress, Run, SemanticState, UnknownSideEffect
from continuum.state.semantic import project
from continuum.storage import ConcurrentWriteError, SQLiteStorage


def test_threads_racing_on_one_connection_get_unique_sequences(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))

        def append(index: int) -> int:
            return store.append_event("run_1", EventType.WORK_COMPLETED, {"i": index}).sequence

        with ThreadPoolExecutor(max_workers=8) as pool:
            sequences = sorted(pool.map(append, range(50)))

        assert sequences == list(range(1, 51))
        assert store.verify_events("run_1").ok


def test_threads_on_separate_connections_do_not_fork_the_chain(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as setup:
        setup.create_run(Run(run_id="run_1", goal="g"))

    with SQLiteStorage(db) as setup:
        setup.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 30})

    def worker(index: int) -> int:
        with SQLiteStorage(db) as store:
            return store.append_event("run_1", EventType.WORK_COMPLETED, {"i": index}).sequence

    with ThreadPoolExecutor(max_workers=6) as pool:
        sequences = sorted(pool.map(worker, range(30)))

    assert sequences == list(range(2, 32))
    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert report.ok
        assert report.trusted_through["run_1"] == 31
        # every concurrent append is counted exactly once, none lost or doubled
        assert project("run_1", store.read_events("run_1")).progress.completed == 30


_CHILD = """
import sys
from continuum.events import EventType
from continuum.storage import SQLiteStorage

db, label, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
written = 0
with SQLiteStorage(db) as store:
    for i in range(count):
        store.append_event("run_1", EventType.WORK_COMPLETED, {"by": label, "i": i})
        written += 1
print(written)
"""


def test_separate_processes_append_to_one_chain_without_corruption(tmp_path: Path) -> None:
    """The real crash-recovery scenario: a restarted agent writes to the same file."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))

    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(_CHILD))

    env_path = str(Path(__file__).resolve().parents[1] / "src")
    children = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), label, "15"],
            # Inherit the parent environment; only PYTHONPATH is added, so the
            # child imports continuum from src/. A bare env= drops SystemRoot on
            # Windows and the child dies on `import _overlapped` during startup.
            env={**os.environ, "PYTHONPATH": env_path},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for label in ("alpha", "beta")
    ]
    results = [child.communicate() for child in children]

    for (out, err), child in zip(results, children, strict=True):
        assert child.returncode == 0, f"child failed: {err}"
        assert out.strip() == "15"

    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert report.ok, report.violations
        assert store.last_sequence("run_1") == 30
        events = store.read_events("run_1")
        assert [e.sequence for e in events] == list(range(1, 31))
        # both writers' work is present; neither overwrote the other
        authors = {e.payload["by"] for e in events}
        assert authors == {"alpha", "beta"}


def test_optimistic_concurrency_detects_a_lost_update(tmp_path: Path) -> None:
    """Two readers plan from the same state; the slower one must be refused."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

        observed = store.last_sequence("run_1")  # both writers read 1

        store.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=observed)
        with pytest.raises(ConcurrentWriteError):
            store.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=observed)

        assert store.last_sequence("run_1") == 2


def test_a_failed_write_leaves_no_partial_row(tmp_path: Path) -> None:
    """Rollback must be complete: a rejected append leaves the chain untouched."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        before = store.read_events("run_1")

        with pytest.raises(ConcurrentWriteError):
            store.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=99)

        assert store.read_events("run_1") == before
        assert store.last_sequence("run_1") == 1
        assert store.verify_events("run_1").ok


def test_concurrent_version_commits_do_not_duplicate_a_version(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        from continuum.models import Goal

        def commit(index: int) -> int:
            state = SemanticState(
                run_id="run_1",
                goal=Goal(description="g"),
                progress=Progress(completed=index),
            )
            return store.put_version(state, reason=f"worker {index}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            versions = sorted(pool.map(commit, range(1, 13)))

        assert versions == list(range(0, 12))
        assert list(store.list_versions("run_1")) == list(range(0, 12))


@pytest.mark.skipif(
    mp.get_start_method(allow_none=True) == "fork", reason="fork start method not required"
)
def test_reads_are_not_blocked_by_an_open_write(tmp_path: Path) -> None:
    """WAL mode: an inspector can read a run while the agent is mid-transaction."""
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as writer:
        writer.create_run(Run(run_id="run_1", goal="g"))
        writer.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

        with SQLiteStorage(db) as reader:
            assert reader.last_sequence("run_1") == 1
            writer.append_event("run_1", EventType.WORK_COMPLETED, {})
            assert reader.last_sequence("run_1") == 2


# --- exactly-once needs the run lease (issue #345) --------------------------- #


def _seed(db: Path) -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})


def test_concurrent_claims_on_one_key_are_not_serialised_by_the_ledger(
    tmp_path: Path,
) -> None:
    """A ledger built *without* a lease still does not serialise (issue #345).

    Pinned deliberately rather than left to be discovered in production. The
    ledger folds the log to decide whether a key was already claimed, and
    nothing stands between that read and the append, so the result depends
    purely on thread timing:

    - a claimant that reads before anyone writes is told to proceed, and several
      can reach that conclusion at once
    - a claimant that reads after a slot is opened raises ``UnknownSideEffect``,
      because an unsettled attempt is genuinely ambiguous

    Which of those a given thread gets is not decidable in advance, so this test
    asserts the shape of the outcome rather than a fixed count.

    This is now the opt-out path, not the only path: passing ``lease`` makes the
    ledger acquire the run lease itself, which
    ``test_a_lease_aware_ledger_admits_exactly_one_concurrent_claimant`` holds to
    an exact count. The unleased default is kept because a single-process caller
    has nothing to serialise against, and it must keep behaving as it always did.

    If a future change makes claiming atomic in storage, the assertion below
    becomes ``== 1``. That is the signal to tighten it, not to delete it.
    """
    from continuum.actions.ledger import ActionLedger

    db = tmp_path / "race.db"
    _seed(db)

    def claim(_: int) -> str:
        with SQLiteStorage(db) as store:
            try:
                outcome = ActionLedger(store, "run_1").claim("charge", {"amt": 100}, key="k")
            except UnknownSideEffect:
                # A prior claimant's unsettled slot was visible. Refusing here is
                # correct behaviour, not a failure.
                return "ambiguous"
            return "proceed" if outcome.fresh else "dedup"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))

    assert set(outcomes) <= {"proceed", "dedup", "ambiguous"}, outcomes
    assert outcomes.count("proceed") >= 1, f"someone must be allowed to work: {outcomes}"
    # More than one go-ahead is possible and is exactly why the lease is needed.
    # The chain still verifies: these are honestly-recorded duplicate attempts,
    # not corruption, which is why no integrity check can catch them.
    with SQLiteStorage(db) as store:
        assert store.verify_events("run_1").ok


def test_the_run_lease_restores_exactly_once_under_concurrency(tmp_path: Path) -> None:
    """The documented remedy, proven rather than asserted.

    Wrapping the claim in the run's lease collapses eight simultaneous claimants
    to a single go-ahead, which is what makes docs/multi_agent_isolation.md's
    "one run, one owner at a time" sufficient for exactly-once.
    """
    from continuum.actions.ledger import ActionLedger
    from continuum.concurrency import SQLiteLeaseCoordinator

    db = tmp_path / "leased.db"
    _seed(db)
    coordinator = SQLiteLeaseCoordinator(tmp_path / "lease.db")

    def claim(n: int) -> str:
        holder = f"agent-{n}"
        if not coordinator.acquire("run_1", holder):
            return "no-lease"
        try:
            with SQLiteStorage(db) as store:
                outcome = ActionLedger(store, "run_1").claim("charge", {"amt": 100}, key="k")
                return "fresh" if outcome.fresh else "dedup"
        finally:
            coordinator.release("run_1", holder)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))

    assert outcomes.count("fresh") == 1, f"expected one winner, got {outcomes}"


# --- the ledger takes the lease itself (issue #345, remaining half) ---------- #


def test_a_lease_aware_ledger_refuses_to_claim_a_run_leased_elsewhere(tmp_path: Path) -> None:
    """The regression guard for issue #345, stated without a race.

    This is the whole fix reduced to its deterministic core: agent-a owns the
    run, so agent-b's ledger must refuse to claim rather than open a second slot
    for the same key. Remove the lock from ``ActionLedger`` and this fails on
    every run and every platform, which is what
    ``test_a_lease_aware_ledger_admits_exactly_one_concurrent_claimant`` cannot
    promise: eight real threads on one SQLite file are largely serialised by the
    GIL and file locking anyway, so the unleased path also tends to produce one
    winner. Timing is a poor witness. A held lease is a certain one.

    The log is checked as well as the exception, because refusing loudly while
    still appending would satisfy ``pytest.raises`` and defeat the purpose.
    """
    from continuum.actions.ledger import ActionLedger, ClaimLockError
    from continuum.concurrency import InMemoryLeaseCoordinator

    db = tmp_path / "refused.db"
    _seed(db)
    coordinator = InMemoryLeaseCoordinator()
    assert coordinator.acquire("run_1", "agent-a")

    with SQLiteStorage(db) as store:
        before = len(store.read_events("run_1"))
        intruder = ActionLedger(store, "run_1", lease=coordinator, holder_id="agent-b")
        with pytest.raises(ClaimLockError, match="agent-a"):
            intruder.claim("charge", {"amt": 100}, key="k")

        assert len(store.read_events("run_1")) == before, "a refused claim must write nothing"
        assert intruder.get("k") is None

        # The refusal is about the lease, not the key: once agent-a is done the
        # same ledger claims normally.
        coordinator.release("run_1", "agent-a")
        assert intruder.claim("charge", {"amt": 100}, key="k").fresh


def test_a_lease_aware_ledger_admits_exactly_one_concurrent_claimant(
    tmp_path: Path,
) -> None:
    """``lease=`` moves the serialization from the caller into the ledger.

    The same eight-thread race as
    ``test_concurrent_claims_on_one_key_are_not_serialised_by_the_ledger``, with
    the only difference being that each ledger is handed the shared coordinator.
    Exactly one claimant is told to proceed, and exactly one ``STARTED`` slot is
    written, however the threads interleave.

    The barrier matters. Opening the SQLite connection is far slower than the
    claim itself, so without a rendezvous the threads arrive one at a time and
    the race never happens. Every thread therefore pays that cost first and only
    then waits, so all eight enter ``claim`` together.

    The losers split into two safe refusals, and both are correct:

    - ``locked``: the run was leased to someone else, so this ledger did not read
      or write at all
    - ``ambiguous``: the lease was free by the time this thread reached it, and
      the winner's slot was already open and unsettled. ``UnknownSideEffect`` is
      the right answer to "may I repeat an effect whose outcome nobody knows?",
      and it is the same answer a sequential caller gets

    Neither refusal performs the charge, which is the guarantee. This test shows
    the mechanism end to end; the load-bearing regression assertion lives in
    ``test_a_lease_aware_ledger_refuses_to_claim_a_run_leased_elsewhere``, which
    does not depend on scheduling.
    """
    from continuum.actions.ledger import ActionLedger, ClaimLockError
    from continuum.concurrency import SQLiteLeaseCoordinator

    db = tmp_path / "self_leased.db"
    _seed(db)
    coordinator = SQLiteLeaseCoordinator(tmp_path / "lease.db")
    start = threading.Barrier(8)

    def claim(n: int) -> str:
        with SQLiteStorage(db) as store:
            ledger = ActionLedger(store, "run_1", lease=coordinator, holder_id=f"agent-{n}")
            start.wait(timeout=10)
            try:
                return "fresh" if ledger.claim("charge", {"amt": 100}, key="k").fresh else "dedup"
            except ClaimLockError:
                return "locked"
            except UnknownSideEffect:
                return "ambiguous"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, range(8)))

    assert outcomes.count("fresh") == 1, f"expected exactly one winner, got {outcomes}"
    assert set(outcomes) <= {"fresh", "locked", "ambiguous"}, outcomes
    with SQLiteStorage(db) as store:
        opened = [
            event
            for event in store.read_events("run_1")
            if event.type is EventType.ACTION_RECORDED and event.payload.get("status") == "started"
        ]
        assert len(opened) == 1, f"one go-ahead must open one slot, got {len(opened)}"
        assert store.verify_events("run_1").ok


def test_a_lease_aware_ledger_is_reentrant_for_its_own_holder(tmp_path: Path) -> None:
    """A caller that already owns the run lease is not locked out of the ledger.

    This is the pattern docs/multi_agent_isolation.md prescribes -- acquire the
    run lease, then write -- so it must keep working when the ledger is also
    lease-aware. Without the reentrancy check the ledger would refuse the one
    caller that did the right thing, because ``acquire`` reports a lease held by
    anyone (including the asker) as unavailable.

    The settle call is included because it takes the lease on the same terms: an
    agent that claims and completes inside one lease must not deadlock halfway.
    """
    from continuum.actions.ledger import ActionLedger
    from continuum.concurrency import InMemoryLeaseCoordinator

    db = tmp_path / "reentrant.db"
    _seed(db)
    coordinator = InMemoryLeaseCoordinator()
    assert coordinator.acquire("run_1", "agent-outer")

    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_1", lease=coordinator, holder_id="agent-outer")
        outcome = ledger.claim("charge", {"amt": 100}, key="k")
        assert outcome.fresh
        ledger.complete(outcome.key, external_id="ch_1")

    # The outer holder still owns the lease: the ledger must not release what it
    # did not acquire, or the caller's remaining work would be unprotected.
    assert coordinator.holder("run_1") == "agent-outer"


def test_a_lease_aware_ledger_guards_the_settle_methods_too(tmp_path: Path) -> None:
    """Settling folds the log and appends, so it races exactly like claiming.

    ``complete``, ``fail``, ``reconcile``, ``compensate`` and ``flag_for_review``
    all read-modify-write the same folded state. A lease that covered only
    ``claim`` would let a second agent overwrite the outcome of an action it does
    not own, which is the same defect one method further along.
    """
    from continuum.actions.ledger import ActionLedger, ClaimLockError
    from continuum.concurrency import InMemoryLeaseCoordinator

    db = tmp_path / "settle.db"
    _seed(db)
    coordinator = InMemoryLeaseCoordinator()

    with SQLiteStorage(db) as store:
        owner = ActionLedger(store, "run_1", lease=coordinator, holder_id="agent-owner")
        outcome = owner.claim("charge", {"amt": 100}, key="k")
        assert outcome.fresh

        # A different agent grabs the run while the owner is mid-flight.
        assert coordinator.acquire("run_1", "agent-intruder")
        for settle in (
            lambda: owner.complete(outcome.key, external_id="ch_1"),
            lambda: owner.fail(outcome.key, "nope"),
            lambda: owner.reconcile(outcome.key, occurred=True),
            lambda: owner.compensate(outcome.key),
            lambda: owner.flag_for_review(outcome.key, "why"),
        ):
            with pytest.raises(ClaimLockError):
                settle()

        coordinator.release("run_1", "agent-intruder")
        # With the run free again the owner settles normally.
        assert owner.complete(outcome.key, external_id="ch_1").external_id == "ch_1"


def test_a_lease_without_a_holder_id_is_refused_at_construction() -> None:
    """A shared default holder would silently defeat the whole mechanism.

    Two processes both defaulting to the same holder string would each see the
    other's lease as their own reentrant lease and both proceed, which is worse
    than no lease at all because it looks protected. Failing loudly at
    construction is the only version of this that cannot be got wrong quietly.
    """
    from continuum.actions.ledger import ActionLedger
    from continuum.concurrency import InMemoryLeaseCoordinator

    with (
        SQLiteStorage(":memory:") as store,
        pytest.raises(ValueError, match="holder_id is required"),
    ):
        ActionLedger(store, "run_1", lease=InMemoryLeaseCoordinator())
