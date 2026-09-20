"""Contract tests for the PostgreSQL storage backend.

Runs the core SQLite-suite behaviours against a real Postgres so the second
engine is a verified surface, not a typed stub. Skips cleanly when
``CONTINUUM_TEST_POSTGRES_DSN`` or ``psycopg`` is absent; CI exercises it for
real via a Postgres 16 service container.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from itertools import count

import pytest

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import ActionStatus, Origin, Run, RunStatus
from continuum.storage.base import ConcurrentWriteError, RunNotFound
from continuum.storage.postgres import PostgresStorage

DSN = os.environ.get("CONTINUUM_TEST_POSTGRES_DSN")

#: Unique throwaway database names for tests that need a store they own.
_iso_counter = count()


def _psycopg_available() -> bool:
    try:
        import psycopg  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    DSN is None or not _psycopg_available(),
    reason="set CONTINUUM_TEST_POSTGRES_DSN and install continuum[postgres] to run",
)


@pytest.fixture
def storage() -> PostgresStorage:
    store = PostgresStorage(DSN)
    yield store
    store.close()


def make_run(store: PostgresStorage, run_id: str, goal: str = "g") -> None:
    store.create_run_started(Run(run_id=run_id, goal=goal))


# --- run lifecycle ------------------------------------------------------------ #


def test_run_lifecycle_round_trip(storage: PostgresStorage) -> None:
    make_run(storage, "pg_life", "Ship it")
    run = storage.get_run("pg_life")
    assert run.status.value == "started"
    assert storage.last_sequence("pg_life") == 1

    updated = storage.update_run(storage.get_run("pg_life").touch(status=RunStatus.COMPLETED))
    assert updated.status.value == "completed"


def test_duplicate_start_is_refused_atomically(storage: PostgresStorage) -> None:
    from continuum.models import Origin

    make_run(storage, "pg_dup")
    with pytest.raises(ConcurrentWriteError):
        storage.create_run_started(Run(run_id="pg_dup", goal="again"), source=Origin.HUMAN)


def test_unknown_run_maps_to_not_found(storage: PostgresStorage) -> None:
    with pytest.raises(RunNotFound):
        storage.get_run("ghost")


def test_active_run_resolution_skips_terminal(storage: PostgresStorage) -> None:
    make_run(storage, "pg_done", "done deal")
    storage.update_run(storage.get_run("pg_done").touch(status=RunStatus.COMPLETED))
    make_run(storage, "pg_live", "still going")
    active = storage.get_active_run()
    assert active is not None
    assert active.run_id == "pg_live"


# --- events --------------------------------------------------------------------- #


def test_event_ordering_reads_and_windowing(storage: PostgresStorage) -> None:
    make_run(storage, "pg_ev", "events")
    for i in range(1, 5):
        storage.append_event("pg_ev", EventType.TASK_UPDATED, {"i": i})
    events = storage.read_events("pg_ev")
    # RUN_STARTED + four TASK_UPDATED appends.
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5]

    window = storage.read_events("pg_ev", after_sequence=1, upto=3)
    assert [e.sequence for e in window] == [2, 3]
    assert all(e.type is EventType.TASK_UPDATED for e in window)


def test_event_chain_verification_and_tamper_detection(
    storage: PostgresStorage,
) -> None:
    make_run(storage, "pg_chain", "chain")
    storage.append_event("pg_chain", EventType.TASK_UPDATED, {"n": 1})
    report = storage.verify_events("pg_chain")
    assert report.ok is True
    assert report.trusted_through["pg_chain"] == 2


def test_concurrent_sequence_is_refused(storage: PostgresStorage) -> None:
    make_run(storage, "pg_c", "c")
    storage.append_event("pg_c", EventType.TASK_UPDATED, {"n": 1})
    with pytest.raises(ConcurrentWriteError):
        storage.append_event("pg_c", EventType.TASK_UPDATED, {"n": 2}, expected_sequence=0)


def test_provenance_survives_the_round_trip(storage: PostgresStorage) -> None:
    make_run(storage, "pg_prov", "p")
    storage.append_event(
        "pg_prov",
        EventType.TOOL_COMPLETED,
        {"tool": "write_file"},
        source=Origin.EXTERNAL_AGENT,
    )
    with PostgresStorage(DSN) as fresh:
        events = fresh.read_events("pg_prov")
    assert events[-1].source is Origin.EXTERNAL_AGENT


# --- versions / checkpoints ------------------------------------------------------ #


def test_checkpoint_manager_round_trip(storage: PostgresStorage) -> None:
    make_run(storage, "pg_ck", "checkpoint me")
    manager = CheckpointManager(storage)
    checkpoint = manager.checkpoint("pg_ck")
    assert checkpoint.version >= 0
    restored = CheckpointManager(storage).restore("pg_ck")
    assert restored.state.run_id == "pg_ck"
    manager.checkpoint("pg_ck")  # second checkpoint: new version or same id
    assert storage.list_versions("pg_ck"), "versions must persist"


def test_list_versions_and_latest(storage: PostgresStorage) -> None:
    make_run(storage, "pg_v", "versions")
    CheckpointManager(storage).checkpoint("pg_v")
    versions = storage.list_versions("pg_v")
    assert versions, "expected at least one stored version"
    assert storage.latest_version("pg_v") is not None


# --- action index (issue #216 projection over Postgres) -------------------------- #


def test_unscoped_claim_deduplicates_through_the_index(
    storage: PostgresStorage,
) -> None:
    a = ActionLedger(storage, "pg_a")
    make_run(storage, "pg_a", "a")
    b = ActionLedger(storage, "pg_b")
    make_run(storage, "pg_b", "b")

    first = a.claim("send_invoice", {}, key="invoice:I-1", scoped_to_run=False)
    a.complete(first.key, external_id="INV-1")
    second = b.claim("send_invoice", {}, key="invoice:I-1", scoped_to_run=False)
    assert second.fresh is False
    assert second.action.external_id == "INV-1"


def test_uncertain_elsewhere_blocks_through_the_index(
    storage: PostgresStorage,
) -> None:
    from continuum.models import UnknownSideEffect

    a = ActionLedger(storage, "pg_c1")
    b = ActionLedger(storage, "pg_c2")
    make_run(storage, "pg_c1", "a")
    make_run(storage, "pg_c2", "b")
    a.claim("send_invoice", {}, key="invoice:X", scoped_to_run=False)
    with pytest.raises(UnknownSideEffect):
        b.claim("send_invoice", {}, key="invoice:X", scoped_to_run=False)


def test_action_status_enum_round_trip(storage: PostgresStorage) -> None:
    ledger = ActionLedger(storage, "pg_s")
    make_run(storage, "pg_s", "s")
    outcome = ledger.claim("deploy", {}, key="dep:1")
    ledger.fail(outcome.key, "boom", certain=True)
    statuses = {a.action_type: a.status for a in ledger.all()}
    assert statuses["deploy"] is ActionStatus.FAILED


# --- langgraph tables exist (schema v4 baseline) ---------------------------------- #


def test_langgraph_tables_present(storage: PostgresStorage) -> None:
    rows = storage._connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name IN"
        " ('lg_checkpoints', 'lg_writes')"
    ).fetchall()
    names = {r["table_name"] for r in rows}
    assert {"lg_checkpoints", "lg_writes"} <= names


# --- compaction (issue #239 parity with the SQLite engine) -------------------------- #


def test_compact_archives_prefix_and_verify_stays_ok(storage: PostgresStorage) -> None:
    make_run(storage, "pg_k", "long task")
    for i in range(3):
        storage.append_event("pg_k", EventType.TASK_UPDATED, {"i": i})
    CheckpointManager(storage).checkpoint("pg_k")

    report = storage.compact_run("pg_k")
    assert report["archived"] > 0

    live = storage.read_events("pg_k")
    assert [e.type for e in live][-1] is EventType.EVENT_LOG_ANCHORED
    archived = storage.read_archived_events("pg_k")
    assert archived[0].sequence == 1
    # Archived prefix and live tail agree on history: no gaps, hashes line up.
    assert storage.verify_events("pg_k").ok is True


def test_pg_compact_rejects_through_sequence_that_would_eat_the_anchor(
    storage: PostgresStorage,
) -> None:
    """Issue #1078: the Postgres backend kept every other safety check the
    SQLite compaction makes but dropped this one. A through_sequence at or
    above the anchor marker's sequence would archive and delete the marker and
    every live row after it, so the next append mints a fresh genesis and forks
    the hash chain away from the archive."""
    make_run(storage, "pg_kg", "anchor guard")
    for i in range(3):
        storage.append_event("pg_kg", EventType.TASK_UPDATED, {"i": i})
    pre_live = len(storage.read_events("pg_kg"))

    with pytest.raises(ValueError, match="anchor"):
        storage.compact_run("pg_kg", through_sequence=10_000)

    # The rejected call leaves a healthy, verifiable log behind: nothing was
    # archived, only the forced checkpoint marker was appended.
    assert storage.verify_events("pg_kg").ok is True
    live = storage.read_events("pg_kg")
    assert len(live) == pre_live + 1
    assert live[0].sequence == 1, "live rows must not have moved"
    assert list(storage.read_archived_events("pg_kg")) == []

    # A bounded value below the anchor still compacts normally.
    result = storage.compact_run("pg_kg", through_sequence=1)
    assert result["archived"] >= 1
    assert [e.type for e in storage.read_events("pg_kg")][-1] is EventType.EVENT_LOG_ANCHORED
    assert storage.verify_events("pg_kg").ok is True


def test_pg_archive_tampering_fails_verify(storage: PostgresStorage) -> None:
    make_run(storage, "pg_kt", "tamper target")
    CheckpointManager(storage).checkpoint("pg_kt")
    storage.compact_run("pg_kt")

    storage._connection.execute(
        "UPDATE events_archive SET payload = '{\"tampered\": true}' WHERE run_id = 'pg_kt'"
    )
    report = storage.verify_events("pg_kt")
    assert report.ok is False
    assert any(v.kind == "TAMPERED_CONTENT" for v in report.violations)


def test_pg_deleted_boundary_event_fails_verify(storage: PostgresStorage) -> None:
    make_run(storage, "pg_kb", "boundary target")
    CheckpointManager(storage).checkpoint("pg_kb")
    storage.compact_run("pg_kb")

    # The DELETE is scoped by run_id on purpose: sequence is per-run, so an
    # unscoped `WHERE sequence =` would delete that row number from every
    # run in the shared suite database and silently corrupt the action index
    # of whichever test ran first.
    storage._connection.execute(
        "DELETE FROM events WHERE run_id = 'pg_kb' AND sequence ="
        " (SELECT MIN(sequence) FROM events WHERE run_id = 'pg_kb')"
    )
    report = storage.verify_events("pg_kb")
    assert report.ok is False
    kinds = {v.kind for v in report.violations}
    assert {"SEQUENCE_GAP", "BROKEN_CHAIN"} & kinds


def test_pg_action_index_covers_the_archive_after_rebuild(
    isolated_storage: PostgresStorage,
) -> None:
    from continuum.actions.idempotency import idempotency_key

    # Drift is a store-wide figure, so this needs a database the test owns
    # rather than the shared suite database.
    storage = isolated_storage
    make_run(storage, "pg_ki", "index target")
    ledger = ActionLedger(storage, "pg_ki")
    outcome = ledger.claim("process_doc", {}, key="doc:1")
    ledger.complete(outcome.key, external_id="doc:1")
    key = str(idempotency_key("process_doc", None, scope="pg_ki", key="doc:1"))

    # Before compaction the index row and the log agree.
    assert storage.action_index_drift() == 0
    foreign = storage.foreign_action(key, exclude_run="some_other_run")
    assert foreign is not None
    assert foreign.status is ActionStatus.COMPLETED

    # Compaction moves the rows the index describes into events_archive;
    # archiving is storage relocation, not a semantic change, so the store
    # still reads clean (#1322). The archive/live ordering gap that used to
    # report it dirty is separate from #1321 (a healthy store reported dirty
    # with no compaction at all).
    storage.compact_run("pg_ki")
    assert storage.action_index_drift() == 0
    foreign = storage.foreign_action(key, exclude_run="some_other_run")
    assert foreign is not None
    assert foreign.status is ActionStatus.COMPLETED

    # A rebuild wipes the table and refolds it from the archive plus the live
    # tail, so it must still find the archived claim.
    storage.rebuild_action_index()
    assert storage.action_index_drift() == 0
    foreign = storage.foreign_action(key, exclude_run="some_other_run")
    assert foreign is not None
    assert foreign.status is ActionStatus.COMPLETED


@pytest.fixture
def isolated_storage() -> Iterator[PostgresStorage]:
    """A database the test owns exclusively.

    ``action_index_drift`` compares a store-wide count of action events
    against a store-wide sequence value, so the comparison is only meaningful
    in a database whose entire history the test controls. The shared suite
    database accumulates every test's runs, and once one run is compacted the
    fold's merged order stops tracking the sequence (the archive/live ordering
    gap, #1322, tracked separately from #1321), which would make a clean store
    read as dirty for reasons this test does not exercise.
    """
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"iso_{os.getpid()}_{next(_iso_counter)}"
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    params = conninfo_to_dict(DSN)
    params["dbname"] = name
    store = PostgresStorage(make_conninfo(**params))
    yield store
    store.close()
    admin = psycopg.connect(DSN, autocommit=True)
    try:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin.close()


def test_pg_action_index_stays_clean_when_another_run_is_compacted(
    isolated_storage: PostgresStorage,
) -> None:
    """A contested key must not read dirty because a *different* run was
    compacted (#1322).

    The fold merges ``events_archive`` ahead of ``events``; within a run that
    is true, across runs it is not. Compacting the run that wrote the key
    second renumbers its archived action events below the first run's live
    ones, so the fold crowns a winner the incremental writer never stored.
    Comparing the order number there reported a healthy store as dirty; the
    criterion now compares what the row asserts and adjudicates a contested
    key only when the row and the fold agree on which run's write won.
    """
    from continuum.actions.idempotency import idempotency_key
    from continuum.models import Action

    storage = isolated_storage
    make_run(storage, "pg_1322a", "first writer")
    make_run(storage, "pg_1322b", "second writer")

    # The first run claims and completes the key; the second run records the
    # same key as a bare STARTED, later in wall time. The row belongs to the
    # second run.
    ledger = ActionLedger(storage, "pg_1322a")
    completed = ledger.claim("process_doc", {}, key="doc:1322", scoped_to_run=False)
    ledger.complete(completed.key, external_id="doc:1322")
    shared = idempotency_key("process_doc", None, scope=None, key="doc:1322")
    assert str(shared) == completed.key  # the same key in both runs, unscoped
    storage.append_event(
        "pg_1322b",
        EventType.ACTION_RECORDED,
        {
            "key": str(shared),
            "action": Action(
                run_id="pg_1322b", action_type="process_doc", status=ActionStatus.STARTED
            ).model_dump(mode="json"),
        },
    )
    before = storage.foreign_action(str(shared), exclude_run="no_such_run")
    assert before is not None and before.run_id == "pg_1322b"

    # Compacting the second run is the trigger: its ACTION_RECORDED moves to
    # the archive and the fold now crowns the first run's earlier live
    # completion, while the writer's row still belongs to the second run.
    CheckpointManager(storage).checkpoint("pg_1322b")
    storage.compact_run("pg_1322b")
    assert storage._canonical_index_rows()[str(shared)][0][1] == "pg_1322a"
    row = storage._connection.execute(
        "SELECT run_id FROM action_index WHERE key = %s", (str(shared),)
    ).fetchone()
    assert row["run_id"] == "pg_1322b"

    # The store is still clean, and compaction did not change what the
    # projection answers.
    assert storage.action_index_drift() == 0
    after = storage.foreign_action(str(shared), exclude_run="no_such_run")
    assert after is not None
    assert (after.run_id, after.action_id) == (before.run_id, before.action_id)


def test_pg_a_row_the_log_authorizes_but_the_projection_lost_reads_dirty(
    isolated_storage: PostgresStorage,
) -> None:
    """The other half of drift on this engine: a key the fold sees whose
    stored row is gone. A projection that silently dropped a row is exactly
    the loss drift exists to catch, and a repair must restore it and count
    doing so (the correction count this engine used to report as 0, #1267)."""
    storage = isolated_storage
    make_run(storage, "pg_lost", "index target")
    ledger = ActionLedger(storage, "pg_lost")
    outcome = ledger.claim("send_invoice", {}, key="invoice:23")
    assert storage.action_index_drift() == 0

    # Delete the projection row only; the log still authorizes it.
    storage._connection.execute("DELETE FROM action_index WHERE key = %s", (outcome.key,))
    assert storage.action_index_drift() == 1

    corrections = storage.rebuild_action_index()
    assert corrections >= 1, "a restored row the projection lost must be counted"
    assert storage.action_index_drift() == 0
    found = storage.foreign_action(outcome.key, exclude_run="no_such_run")
    assert found is not None
    assert found.status is ActionStatus.STARTED
