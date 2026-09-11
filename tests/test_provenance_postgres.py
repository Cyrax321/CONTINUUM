"""Provenance graph compaction survival on PostgreSQL (#597, step 3).

Mirrors the SQLite archive test against a live Postgres: compact moves the
prefix to events_archive and the graph rebuilt via read_all_events keeps
every node and edge, including finding-to-evidence links. Skips cleanly
without CONTINUUM_TEST_POSTGRES_DSN; CI exercises it via a service
container, following tests/test_storage_postgres.py.
"""

from __future__ import annotations

import os

import pytest

from continuum.events import EventType
from continuum.models import Run
from continuum.provenance.graph import build_provenance_graph
from continuum.storage.postgres import PostgresStorage

DSN = os.environ.get("CONTINUUM_TEST_POSTGRES_DSN")


def _psycopg_available() -> bool:
    try:
        import psycopg  # type: ignore[import-not-found]  # noqa:  F401
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


def test_graph_survives_compaction_via_archive(storage: PostgresStorage) -> None:
    run_id = "run_pg_compact_graph"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    finding = storage.append_event(
        run_id,
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "c", "caused_by": [ev.event_id]},
    )
    dec = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d", "decision_id": "dec1", "caused_by": [finding.event_id]},
    )
    from continuum.state.semantic import project

    storage.put_version(project(run_id, storage.read_events(run_id)))
    graph_before = build_provenance_graph(storage.read_all_events(run_id))
    edges_before = {(p, c) for p, cs in graph_before.edges.items() for c in cs}
    assert (ev.event_id, finding.event_id) in edges_before
    assert (finding.event_id, dec.event_id) in edges_before

    report = storage.compact_run(run_id)
    assert report["archived"] >= 1

    graph_after = build_provenance_graph(storage.read_all_events(run_id))
    edges_after = {(p, c) for p, cs in graph_after.edges.items() for c in cs}
    assert edges_after == edges_before
    assert len(graph_after.nodes) == len(graph_before.nodes)
    assert storage.verify_events(run_id).ok
