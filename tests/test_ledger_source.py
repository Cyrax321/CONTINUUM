"""Remote-agent ledger writes carry EXTERNAL_AGENT (#653).

MCP and sidecar servers stamp their direct appends EXTERNAL_AGENT but built
their ledgers with the deterministic default, so agent-asserted effects
laundered to trusted in derived provenance. Both now construct their ledger
with AGENT_SOURCE. Stacked on the #612 mechanism.
"""

from __future__ import annotations

from pathlib import Path

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.provenance_map import derived_provenance_for_events
from continuum.storage import SQLiteStorage


def _run(db: str) -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})


def test_mcp_completed_action_derives_external_agent(tmp_path: Path) -> None:
    from continuum.mcp.server import AuthorizationPolicy, build_server

    db = str(tmp_path / "mcp.db")
    _run(db)
    server, ctx = build_server(
        storage=SQLiteStorage(db), policy=AuthorizationPolicy(["test-client"])
    )
    outcome = ctx.ledger("run_1").claim("send_invoice", {}, key="invoice:1")
    ctx.ledger("run_1").complete(outcome.key, external_id="ext-1")
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    assert derived_provenance_for_events(events) is Origin.EXTERNAL_AGENT
    ctx.storage.close()


def test_sidecar_completed_action_derives_external_agent(tmp_path: Path) -> None:
    from continuum.serve import SidecarServer

    db = str(tmp_path / "sidecar.db")
    _run(db)
    srv = SidecarServer(database=db)
    claim = srv.dispatch("intercept_action", {"run_id": "run_1", "action_type": "x", "key": "k"})
    srv.dispatch("complete_action", {"run_id": "run_1", "action_key": claim["action_key"]})
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    assert derived_provenance_for_events(events) is Origin.EXTERNAL_AGENT
    srv.close()


def test_direct_ledger_default_is_unchanged(tmp_path: Path) -> None:
    db = str(tmp_path / "plain.db")
    _run(db)
    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("x", {}, key="k")
        events = store.read_events("run_1")
    assert derived_provenance_for_events(events) is Origin.DETERMINISTIC
