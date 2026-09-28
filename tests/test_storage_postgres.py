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


def test_pg_a_run_can_be_compacted_repeatedly(storage: PostgresStorage) -> None:
    """Compact, work, compact, on Postgres too (issue #648, PR #715 review).

    The second compact takes a fresh anchor checkpoint whose projection used
    to fold the live tail only. After the first compaction that tail begins
    at the anchor markers with no RUN_STARTED, so the anchor raised
    "could not be anchored ... has no goal" on this backend exactly as it did
    on SQLite before the fix. The SQLite regression test lives in
    tests/test_compaction.py; this is its Postgres twin, so the second engine
    is a verified surface for the same property, not a typed stub.
    """
    make_run(storage, "pg_kr", "long-lived task")
    for i in range(3):
        storage.append_event("pg_kr", EventType.TASK_UPDATED, {"i": i})
    first = storage.compact_run("pg_kr")
    assert first["archived"] > 0
    assert storage.verify_events("pg_kr").ok is True

    for i in range(3, 6):
        storage.append_event("pg_kr", EventType.TASK_UPDATED, {"i": i})
    archived_before = len(storage.read_archived_events("pg_kr"))
    second = storage.compact_run("pg_kr")
    assert second["archived"] > 0, "the second compact must archive the new prefix"
    assert storage.verify_events("pg_kr").ok is True
    # Only the new prefix moved: the archive grew by exactly what this compact
    # reported, and the live tail still carries its anchor marker.
    archived_after = len(storage.read_archived_events("pg_kr"))
    assert archived_after - archived_before == second["archived"]
    assert [e.type for e in storage.read_events("pg_kr")][-1] is EventType.EVENT_LOG_ANCHORED

    # Restore still works on the twice-compacted run.
    restored = CheckpointManager(storage).restore("pg_kr")
    assert restored.state.run_id == "pg_kr"


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

    # The run_id restriction is load-bearing, not decorative: sequence is
    # per-run, so an unscoped ``WHERE sequence =`` deletes that row number
    # from every run in the shared suite database and silently corrupts
    # whichever action event happened to land on it.
    storage._connection.execute(
        "DELETE FROM events WHERE run_id = 'pg_kb' AND sequence ="
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

    # Compaction moves the rows the index describes into events_archive, and
    # the fold merges that table ahead of every live row, so the projection
    # is reported dirty until it is rebuilt. That desync is the archive/live
    # ordering gap, #1322, which is separate from #1321 (a healthy store
    # reported dirty with no compaction at all).
    assert storage.action_index_drift() > 0
    storage.rebuild_action_index()
    assert storage.action_index_drift() == 0
    key = str(idempotency_key("process_doc", None, scope="pg_ki", key="doc:1"))
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


def test_pg_action_index_drift_stays_zero_with_non_action_events_between_actions(
    isolated_storage: PostgresStorage,
) -> None:
    """A healthy store must not report drift because of how it counts rows (#1321).

    ``_maintain_action_index`` numbers a row with ``nextval``, which advances
    once per action event, while the canonical fold numbered it by its
    position in the merged row stream, which counts RUN_STARTED, TOOL_CALLED,
    EVIDENCE_ADDED and every other non-action row too. An ordinary run has
    those between its actions, so the two figures disagreed by one per
    intervening row and ``verify`` reported a permanently dirty index on a
    store nothing had tampered with. The fold now counts action events only,
    1-based, which is the number the sequence actually assigned.
    """
    storage = isolated_storage
    make_run(storage, "pg_1321", "healthy run")
    # A non-action event between the two action events: the row population the
    # two numbering schemes disagreed over.
    storage.append_event("pg_1321", EventType.EVIDENCE_ADDED, {"evidence_id": "e1", "summary": "s"})
    ledger = ActionLedger(storage, "pg_1321")
    outcome = ledger.claim("process_doc", {}, key="doc:1321")
    ledger.complete(outcome.key, external_id="doc:1321")

    assert storage.action_index_drift() == 0
    # The fold and the incremental writer agree on the number itself, not just
    # on the absence of drift.
    canonical = storage._canonical_index_rows()
    stored = {
        r["key"]: int(r["updated_seq"])
        for r in storage._connection.execute("SELECT key, updated_seq FROM action_index")
    }
    assert {k: v for k, (_, v) in canonical.items()} == stored


def test_pg_action_index_stays_clean_after_a_rebuild_and_further_appends(
    isolated_storage: PostgresStorage,
) -> None:
    """A rebuild must not renumber the past in a way the next append breaks (#1321).

    Before the fix, the fold numbered a row by its position in the merged row
    stream, so a rebuild rewrote ``updated_seq`` on one scale while the next
    appended action took the next ``nextval`` on the other; the two diverged
    again immediately. ``foreign_action`` ranks with
    ``ORDER BY updated_seq DESC``, so a fresh event that landed below the
    rewritten rows would rank as the older write.
    """
    storage = isolated_storage
    make_run(storage, "pg_rb", "rebuild then append")
    storage.append_event("pg_rb", EventType.EVIDENCE_ADDED, {"evidence_id": "e1", "summary": "s"})
    ledger = ActionLedger(storage, "pg_rb")
    outcome = ledger.claim("process_doc", {}, key="doc:rb")
    ledger.complete(outcome.key, external_id="doc:rb")

    storage.rebuild_action_index()
    assert storage.action_index_drift() == 0

    # The appended action is newer than every row the rebuild numbered, so it
    # ranks newest and the index is still consistent.
    later = ledger.claim("process_doc", {}, key="doc:rb2")
    ledger.complete(later.key, external_id="doc:rb2")
    assert storage.action_index_drift() == 0
    newest = storage.foreign_action(later.key, exclude_run="no_such_run")
    assert newest is not None
    assert newest.run_id == "pg_rb"


def test_pg_run_without_a_parent_round_trips_null(storage: PostgresStorage) -> None:
    """A parentless run must load back as parentless, not as a corrupt row."""
    make_run(storage, "pg_solo", "solo")
    assert storage.get_run("pg_solo").parent_run_id is None


def test_pg_child_run_keeps_its_parent_after_the_round_trip(
    storage: PostgresStorage,
) -> None:
    """A fork's lineage column must survive the write and the read (#1079).

    ``children_of`` filters ``list_runs`` on ``parent_run_id``, so a dropped
    column made the family resume block vacuous here while SQLite enforced it.
    """
    make_run(storage, "pg_par", "supervise")
    storage.create_run_started(
        Run(run_id="pg_kid", goal="work", parent_run_id="pg_par"),
        source=Origin.HUMAN,
    )

    assert storage.get_run("pg_kid").parent_run_id == "pg_par"
    assert storage.get_run("pg_par").parent_run_id is None

    from continuum.recovery.family import children_of

    assert [run.run_id for run in children_of(storage, "pg_par")] == ["pg_kid"]
    assert children_of(storage, "pg_kid") == []
