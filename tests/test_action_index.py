"""The action index projection (issue #216).

Cross-run idempotency lookups used to fold every run's full event log,
O(total logged events) per unscoped claim miss. The index is a derived
projection maintained in the same transaction as each event insert; these
tests pin its correctness against the historical scan semantics.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.cli import ExitCode
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run, UnknownSideEffect
from continuum.storage import SQLiteStorage
from continuum.storage.migrations import SCHEMA_VERSION


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "idx.db")


@pytest.fixture
def store(db_path: str) -> Iterator[SQLiteStorage]:
    with SQLiteStorage(db_path) as s:
        yield s


def make_run(store: SQLiteStorage, run_id: str) -> ActionLedger:
    store.create_run(Run(run_id=run_id, goal="g"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return ActionLedger(store, run_id)


# --- maintenance -------------------------------------------------------------- #


def test_claims_are_indexed_incrementally_in_the_same_transaction(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:1")
    assert outcome.fresh is True
    rows = store._connection.execute("SELECT run_id, status FROM action_index").fetchall()
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run_1"
    assert rows[0]["status"] == ActionStatus.STARTED.value


def test_index_persists_across_reopen(db_path: str) -> None:
    with SQLiteStorage(db_path) as first:
        make_run(first, "run_1").claim("send_invoice", {}, key="invoice:1")
    with SQLiteStorage(db_path) as second:
        found = second.foreign_action(
            __import__(
                "continuum.actions.idempotency", fromlist=["idempotency_key"]
            ).idempotency_key("send_invoice", None, scope="run_1", key="invoice:1"),
            exclude_run="other",
        )
    assert found is not None
    assert found.status is ActionStatus.STARTED


def test_completion_updates_the_same_index_row(store: SQLiteStorage) -> None:
    """The index follows every ACTION_* write, not just claims: after
    complete_action the row reflects COMPLETED without a rebuild."""
    ledger = make_run(store, "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:6")
    assert ledger.complete(outcome.key, external_id="INV-6") is not None
    key = str(outcome.key)
    row = store._connection.execute(
        "SELECT status FROM action_index WHERE key = ?", (key,)
    ).fetchone()
    assert row["status"] == ActionStatus.COMPLETED.value


# --- equivalence with the historical scan --------------------------------------- #


def test_foreign_completed_lookup_matches_the_scan_semantics(store: SQLiteStorage) -> None:
    a = make_run(store, "run_1")
    b_ledger = make_run(store, "run_2")
    outcome = a.claim("send_invoice", {}, key="invoice:9", scoped_to_run=False)
    a.complete(outcome.key, external_id="INV-9")

    from continuum.actions.idempotency import idempotency_key

    key = idempotency_key("send_invoice", None, scope=None, key="invoice:9")
    indexed = store.foreign_action(key, exclude_run="run_2")

    # The legacy scan folds every other run's log; compare verdicts.
    legacy_folded = {}
    for run in store.list_runs():
        if run.run_id == "run_2":
            continue
        from continuum.actions.ledger import fold_action_events

        legacy_folded.update(fold_action_events(store.read_events(run.run_id)))
    legacy = legacy_folded.get(key)

    assert indexed is not None and legacy is not None
    assert indexed.status is ActionStatus.COMPLETED
    assert legacy.status is ActionStatus.COMPLETED
    assert indexed.external_id == legacy.external_id
    assert b_ledger is not None


def test_unscoped_duplicate_still_deduplicates_through_the_index(store: SQLiteStorage) -> None:
    a = make_run(store, "run_1")
    b = make_run(store, "run_2")
    first = a.claim("send_invoice", {}, key="invoice:5", scoped_to_run=False)
    a.complete(first.key, external_id="INV-5")
    second = b.claim("send_invoice", {}, key="invoice:5", scoped_to_run=False)
    assert second.fresh is False
    assert second.action.external_id == "INV-5"


def test_uncertain_elsewhere_still_blocks_through_the_index(store: SQLiteStorage) -> None:
    a = make_run(store, "run_1")
    b = make_run(store, "run_2")
    a.claim("send_invoice", {}, key="invoice:7", scoped_to_run=False)
    with pytest.raises(UnknownSideEffect):
        b.claim("send_invoice", {}, key="invoice:7", scoped_to_run=False)


def test_scoped_keys_never_match_other_runs(store: SQLiteStorage) -> None:
    """A run-scoped key embeds its scope, so cross-run lookup must miss even
    when the caller-supplied part is identical."""
    a = make_run(store, "run_1")
    a.claim("send_invoice", {}, key="invoice:3", scoped_to_run=True)
    b = make_run(store, "run_2")
    second = b.claim("send_invoice", {}, key="invoice:3", scoped_to_run=True)
    assert second.fresh is True


# --- drift and repair ----------------------------------------------------------- #


def test_drift_is_detected_and_rebuild_repairs_it(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    ledger.claim("send_invoice", {}, key="invoice:2")
    assert store.action_index_drift() == 0

    # Simulate corruption of the projection (not the truth).
    store._connection.execute("UPDATE action_index SET status = 'completed'")
    assert store.action_index_drift() > 0

    corrections = store.rebuild_action_index()
    assert corrections >= 1
    assert store.action_index_drift() == 0
    # The rebuilt row reflects the truth: still STARTED, not 'completed'.
    from continuum.actions.idempotency import idempotency_key

    key = idempotency_key("send_invoice", None, scope="run_1", key="invoice:2")
    refreshed = store.foreign_action(key, exclude_run="none")
    assert refreshed is not None
    assert refreshed.status is ActionStatus.STARTED


def test_spurious_rows_count_as_drift_and_are_removed(store: SQLiteStorage) -> None:
    make_run(store, "run_1")
    store._connection.execute(
        "INSERT INTO action_index(key, run_id, action_id, status, updated_seq, action_json) "
        "VALUES ('ghost', 'run_1', 'a', 'started', 999, '{}')"
    )
    assert store.action_index_drift() >= 1
    store.rebuild_action_index()
    assert store.action_index_drift() == 0


def test_a_healthy_store_with_non_action_rows_between_actions_reports_no_drift(
    store: SQLiteStorage,
) -> None:
    """The canonical fold must number a row as the incremental writer numbered it.

    A run records plenty of non-action events between its actions -- tool
    calls, evidence, findings. SQLite is immune to #1321 because both the
    incremental maintenance and the fold use the writing event's ``rowid``,
    so intervening rows move both figures together. This pins that agreement
    so a refactor that switches the SQLite fold to counting action events
    only -- which is what the Postgres engine needs -- is caught here rather
    than than silently desynchronising the two numbering scales.
    """
    ledger = make_run(store, "run_1")
    store.append_event("run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "e1", "summary": "s"})
    store.append_event("run_1", EventType.TOOL_COMPLETED, {"tool": "t", "status": 200})
    outcome = ledger.claim("send_invoice", {}, key="invoice:9")
    ledger.complete(outcome.key, external_id="invoice:9")

    assert store.action_index_drift() == 0


# --- engines without an index ---------------------------------------------------- #


class IndexlessStore(SQLiteStorage):
    """Simulates an engine without index support to pin the fallback path."""

    supports_action_index = False  # type: ignore[misc]


def test_fallback_scan_is_used_when_the_engine_has_no_index(tmp_path: Path) -> None:
    path = str(tmp_path / "legacy.db")
    with IndexlessStore(path) as store:
        a = ActionLedger(store, "run_1")
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        first = a.claim("send_invoice", {}, key="invoice:4", scoped_to_run=False)
        a.complete(first.key, external_id="INV-4")
        store.create_run(Run(run_id="run_2", goal="g"))
        store.append_event("run_2", EventType.RUN_STARTED, {"goal": "g"})
        b = ActionLedger(store, "run_2")
        second = b.claim("send_invoice", {}, key="invoice:4", scoped_to_run=False)
        assert second.fresh is False
        assert second.action.external_id == "INV-4"


# --- schema --------------------------------------------------------------------- #


def test_baseline_and_migration_both_produce_the_table(db_path: str) -> None:
    # Fresh database: baseline DDL includes the table.
    with SQLiteStorage(db_path) as store:
        names = {
            r["name"]
            for r in store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "action_index" in names


def test_schema_version_is_six() -> None:
    """Pinned so a future bump consciously revisits prior migrations."""
    assert SCHEMA_VERSION == 6


def test_v2_database_backfills_on_open(tmp_path: Path) -> None:
    """A database written before the index gains correct lookups on open."""
    legacy = """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'started',
            created_at TEXT NOT NULL, updated_at TEXT
        );
        CREATE TABLE events (
            run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL,
            event_id TEXT PRIMARY KEY, type TEXT NOT NULL, timestamp TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}', causer_event_id TEXT,
            source TEXT NOT NULL DEFAULT 'deterministic',
            prev_hash TEXT, hash TEXT, UNIQUE(run_id, sequence)
        );
        CREATE TABLE versions (run_id TEXT, version INTEGER, PRIMARY KEY (run_id, version));
        CREATE TABLE checkpoints (
            checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            version INTEGER NOT NULL, trigger TEXT NOT NULL, created_at TEXT NOT NULL,
            integrity_hash TEXT NOT NULL, body TEXT NOT NULL
        );
        CREATE TABLE continuum_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO continuum_meta(key, value) VALUES ('schema_version', '2');
    """
    db = tmp_path / "v2.db"
    raw = sqlite3.connect(db)
    raw.executescript(legacy)
    # A pre-index action event, shaped exactly like the writers produce it.
    from continuum.actions.idempotency import idempotency_key

    key = idempotency_key("send_invoice", None, scope="old", key="invoice:11")
    action = Action(run_id="old", action_type="send_invoice", status=ActionStatus.COMPLETED)
    raw.execute("INSERT INTO runs VALUES ('old', 'g', 'started', '2026-01-01', NULL)")
    raw.execute(
        "INSERT INTO events VALUES ('old', 1, 'e1', 'RUN_STARTED', '2026-01-01', ?, "
        "NULL, 'deterministic', NULL, NULL)",
        (json.dumps({"goal": "g"}),),
    )
    raw.execute(
        "INSERT INTO events VALUES ('old', 2, 'e2', 'ACTION_RECORDED', '2026-01-02', ?, "
        "NULL, 'deterministic', NULL, NULL)",
        (json.dumps({"key": key, "action": action.model_dump(mode="json")}),),
    )
    raw.commit()
    raw.close()

    with SQLiteStorage(str(db)) as store:
        found = store.foreign_action(key, exclude_run="other")
        assert found is not None
        assert found.status is ActionStatus.COMPLETED


def test_a_key_rewritten_by_another_run_is_global_last_write_wins(
    store: SQLiteStorage,
) -> None:
    """The key namespace is store-global: if run B later records the same key
    run A recorded, the index row belongs to B, drift stays zero, and lookups
    return B's record. A run-scoped fold would falsely flag A's row."""

    a = make_run(store, "run_1")
    make_run(store, "run_2")
    first = a.claim("send_invoice", {}, key="shared:1", scoped_to_run=False)
    a.complete(first.key, external_id="INV-A")

    # Run B records the identical key directly (as an interrupted attempt).
    from continuum.actions.idempotency import idempotency_key

    b_key = idempotency_key("send_invoice", None, scope=None, key="shared:1")
    action_b = Action(run_id="run_2", action_type="send_invoice", status=ActionStatus.STARTED)
    store.append_event(
        "run_2",
        EventType.ACTION_RECORDED,
        {"key": str(b_key), "action": action_b.model_dump(mode="json")},
    )

    assert store.action_index_drift() == 0
    found = store.foreign_action(str(b_key), exclude_run="nobody")
    assert found is not None
    assert found.run_id == "run_2"
    assert found.status is ActionStatus.STARTED


def test_repair_refuses_when_the_chain_fails(tmp_path: Path) -> None:
    """A tampered log must never be folded into the projection (review 221)."""
    db = str(tmp_path / "tamper.db")
    with SQLiteStorage(db) as store:
        make_run(store, "run_1")
        ActionLedger(store, "run_1").claim("send_invoice", {}, key="k-repair")
        # Tamper with the committed event in place.
        store._connection.execute(
            "UPDATE events SET payload = replace(payload, '\"started\"', '\"completed\"') "
            "WHERE type = 'ACTION_RECORDED'"
        )
    out_buf, err_buf = io.StringIO(), io.StringIO()
    from continuum.cli import main as cli_main

    code = cli_main(
        ["--db", db, "--json", "verify", "run_1", "--index", "--repair-index"],
        out=out_buf,
        err=err_buf,
    )
    assert code == ExitCode.CORRUPTED
    body = json.loads(out_buf.getvalue())
    assert body.get("action_index_repair") == "refused_chain_failed"
    # And the drift is reported as unknown rather than silently repaired.
    assert body["action_index_drift"] is None
