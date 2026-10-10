"""The action index carries the plaintext ``rendered_key`` (issue #1417).

Memory writes claim under an explicit key, so the projection's ``key`` is a
digest and the tenant namespace survives only in the event payload's
``rendered_key``. ``continuum forget`` enumerates by tenant, which makes the
column the difference between an indexed read and a fold over every run's full
log. These tests pin that the index carries it, that an upgraded store
backfills it, and that tenant enumeration agrees with the event-scan fallback.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage
from continuum.storage.base import Storage


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "rendered_key.db")


@pytest.fixture
def store(db_path: str) -> Iterator[SQLiteStorage]:
    with SQLiteStorage(db_path) as s:
        yield s


def make_run(store: SQLiteStorage, run_id: str) -> ActionLedger:
    store.create_run(Run(run_id=run_id, goal="g"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    return ActionLedger(store, run_id)


def _write(ledger: ActionLedger, rendered: str) -> None:
    ledger.claim("mem_write", {}, key=rendered, scoped_to_run=False)


# --- the index carries the plaintext identity --------------------------------- #


def test_memory_write_indexes_its_rendered_key(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    _write(ledger, "mem:pgvector_main:acme:rec-1")

    rows = store._connection.execute("SELECT key, rendered_key FROM action_index").fetchall()
    assert len(rows) == 1
    # The ledger key is hashed, so the plaintext lives in the new column and
    # not in the identity the index is keyed on.
    assert rows[0]["key"] != "mem:pgvector_main:acme:rec-1"
    assert rows[0]["rendered_key"] == "mem:pgvector_main:acme:rec-1"


def test_non_memory_write_has_a_null_rendered_key(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    ledger.claim("send_invoice", {}, key="invoice:1")

    rows = store._connection.execute("SELECT rendered_key FROM action_index").fetchall()
    # An action without a plaintext identity stays NULL: the column is an
    # opt-in attribute of memory writes, not a column every row must fill.
    assert [r["rendered_key"] for r in rows] == [None]


# --- tenant enumeration -------------------------------------------------------- #


def test_enumeration_separates_tenants(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    _write(ledger, "mem:pgvector_main:acme:rec-1")
    _write(ledger, "mem:pgvector_main:acme:rec-2")
    _write(ledger, "mem:pgvector_main:globex:rec-1")

    acme = store.enumerate_tenant_memory("acme")
    assert sorted(hit["record_key"] for hit in acme) == ["rec-1", "rec-2"]
    assert all(hit["rendered_key"].split(":")[2] == "acme" for hit in acme)

    globex = store.enumerate_tenant_memory("globex")
    assert [hit["record_key"] for hit in globex] == ["rec-1"]

    assert store.enumerate_tenant_memory("nobody") == []


def test_enumeration_covers_both_key_shapes_and_keeps_colons(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    # mem:{store}:{tenant}:{record_key...}
    _write(ledger, "mem:pgvector_main:acme:doc:section:1")
    # memory:{store}:{tenant}:{namespace}:{record_key...}
    _write(ledger, "memory:pgvector_main:acme:long_term:doc:section:2")

    hits = store.enumerate_tenant_memory("acme")
    assert sorted(hit["record_key"] for hit in hits) == [
        "doc:section:1",
        "doc:section:2",
    ]
    # The shapes differ in where the record key starts, and a tenant that owns
    # no records in one of them must not inherit the other's namespace.
    assert store.enumerate_tenant_memory("long_term") == []


def test_enumeration_scopes_to_a_run(store: SQLiteStorage) -> None:
    _write(make_run(store, "run_1"), "mem:pgvector_main:acme:rec-1")
    _write(make_run(store, "run_2"), "mem:pgvector_main:acme:rec-2")

    assert [hit["record_key"] for hit in store.enumerate_tenant_memory("acme", run_id="run_1")] == [
        "rec-1"
    ]
    assert sorted(hit["record_key"] for hit in store.enumerate_tenant_memory("acme")) == [
        "rec-1",
        "rec-2",
    ]


def test_enumeration_reports_status(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    outcome = ledger.claim("mem_write", {}, key="mem:pg:acme:rec-1", scoped_to_run=False)
    ledger.complete(outcome.action.action_id, result={"ok": True})

    hits = store.enumerate_tenant_memory("acme")
    assert len(hits) == 1
    assert hits[0]["status"] == "completed"


def test_indexed_enumeration_matches_the_event_scan(store: SQLiteStorage) -> None:
    """The indexed read must agree with the base-class fold it replaces."""
    _write(make_run(store, "run_1"), "mem:pgvector_main:acme:rec-1")
    _write(make_run(store, "run_2"), "memory:pgvector_main:acme:cold:rec-2")
    _write(make_run(store, "run_3"), "mem:pgvector_main:globex:rec-3")

    for tenant in ("acme", "globex", "nobody"):
        indexed = store.enumerate_tenant_memory(tenant)
        scanned = Storage.enumerate_tenant_memory(store, tenant)
        assert sorted((h["run_id"], h["rendered_key"]) for h in indexed) == sorted(
            (h["run_id"], h["rendered_key"]) for h in scanned
        )


# --- upgrades and rebuilds ----------------------------------------------------- #


def _stamp_legacy_v6(db_path: str) -> None:
    """Rewind a store to the pre-``rendered_key`` shape the upgrade must fix."""
    raw = sqlite3.connect(db_path)
    # SQLite requires dropping indexes that reference the column before dropping it.
    raw.execute("DROP INDEX IF EXISTS action_index_rendered_key")
    raw.execute("ALTER TABLE action_index DROP COLUMN rendered_key")
    raw.execute("UPDATE continuum_meta SET value = '6' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()


def test_migration_backfills_rendered_key_on_upgrade(db_path: str) -> None:
    with SQLiteStorage(db_path) as store:
        ledger = make_run(store, "run_1")
        _write(ledger, "mem:pgvector_main:acme:rec-1")
        _write(ledger, "mem:pgvector_main:globex:rec-2")

    _stamp_legacy_v6(db_path)

    # Reopening runs the forward migration: the column returns populated, so a
    # store written before it existed answers the same tenant query a fresh
    # one does.
    with SQLiteStorage(db_path) as store:
        assert [hit["record_key"] for hit in store.enumerate_tenant_memory("acme")] == ["rec-1"]
        # The projection still agrees with the log after the rewrite.
        assert store.action_index_drift() == 0


def test_rebuild_preserves_rendered_key(store: SQLiteStorage) -> None:
    ledger = make_run(store, "run_1")
    _write(ledger, "mem:pgvector_main:acme:rec-1")

    assert store.rebuild_action_index() == 0
    assert [hit["record_key"] for hit in store.enumerate_tenant_memory("acme")] == ["rec-1"]


def test_compaction_survives_enumeration(store: SQLiteStorage) -> None:
    """Archived memory writes still enumerate after compaction."""
    ledger = make_run(store, "run_1")
    _write(ledger, "mem:pgvector_main:acme:rec-1")
    store.compact_run("run_1")

    assert [hit["record_key"] for hit in store.enumerate_tenant_memory("acme")] == ["rec-1"]
