"""Remote-agent ledger writes must not self-certify as deterministic (#653)."""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "remote.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


def _assert_external(action_run_events: list, run_id: str, db_path: str) -> None:
    from continuum.models import Origin
    from continuum.provenance_map import derived_provenance_for_events

    assert action_run_events, "surface must record action events"
    assert {e.source for e in action_run_events} == {Origin.EXTERNAL_AGENT}
    with SQLiteStorage(db_path) as store:
        derived = derived_provenance_for_events(store.read_events(run_id))
    assert derived is Origin.EXTERNAL_AGENT


def test_mcp_ledger_writes_carry_external_agent(db: str) -> None:
    """MCP-asserted effects must derive external_agent, not deterministic (#653)."""
    from continuum.mcp.server import ContinuumMCP

    mcp = ContinuumMCP(storage=SQLiteStorage(db))
    try:
        ledger = mcp.ledger("run_1")
        outcome = ledger.claim("notify.customer", {"order_id": "O-9"})
        ledger.complete(outcome.key)
        with SQLiteStorage(db) as store:
            action_events = [
                e for e in store.read_events("run_1") if e.type is EventType.ACTION_RECORDED
            ]
    finally:
        mcp.close()
    _assert_external(action_events, "run_1", db)


def test_sidecar_ledger_writes_carry_external_agent(db: str) -> None:
    """Sidecar-asserted effects must derive external_agent, not deterministic (#653)."""
    from continuum.serve.server import SidecarServer

    server = SidecarServer(storage=SQLiteStorage(db))
    try:
        ledger = server._ledger("run_1")
        outcome = ledger.claim("notify.customer", {"order_id": "O-9"})
        ledger.complete(outcome.key)
        with SQLiteStorage(db) as store:
            action_events = [
                e for e in store.read_events("run_1") if e.type is EventType.ACTION_RECORDED
            ]
    finally:
        server.close()
    _assert_external(action_events, "run_1", db)
