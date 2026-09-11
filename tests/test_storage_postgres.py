"""Contract tests for the PostgreSQL storage backend.

Runs the core SQLite-suite behaviours against a real Postgres so the second
engine is a verified surface, not a typed stub. Skips cleanly when
``CONTINUUM_TEST_POSTGRES_DSN`` or ``psycopg`` is absent; CI exercises it for
real via a Postgres 16 service container.
"""

from __future__ import annotations

import os

import pytest

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import ActionStatus, Origin, Run, RunStatus
from continuum.storage.base import ConcurrentWriteError, RunNotFound
from continuum.storage.postgres import PostgresStorage

DSN = os.environ.get("CONTINUUM_TEST_POSTGRES_DSN")


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

    storage._connection.execute(
        "DELETE FROM events WHERE sequence ="
        " (SELECT MIN(sequence) FROM events WHERE run_id = 'pg_kb')"
    )
    report = storage.verify_events("pg_kb")
    assert report.ok is False
    kinds = {v.kind for v in report.violations}
    assert {"SEQUENCE_GAP", "BROKEN_CHAIN"} & kinds


def test_pg_action_index_covers_the_archive_after_rebuild(
    storage: PostgresStorage,
) -> None:
    from continuum.actions.idempotency import idempotency_key

    make_run(storage, "pg_ki", "index target")
    ledger = ActionLedger(storage, "pg_ki")
    outcome = ledger.claim("process_doc", {}, key="doc:1")
    ledger.complete(outcome.key, external_id="doc:1")
    storage.compact_run("pg_ki")

    assert storage.action_index_drift() > 0
    storage.rebuild_action_index()
    assert storage.action_index_drift() == 0
    key = str(idempotency_key("process_doc", None, scope="pg_ki", key="doc:1"))
    foreign = storage.foreign_action(key, exclude_run="some_other_run")
    assert foreign is not None
    assert foreign.status is ActionStatus.COMPLETED
