"""consumed_inputs over MCP (issue #295, sub-issue #558).

The ledger has accepted ``consumed_inputs`` on ``complete`` and ``reconcile``
for a while, but the MCP tools never forwarded it, so an agent reporting
through the server had no way to record what its effect was computed from.
These tests drive both tools through the real dispatch path and assert the
commitment lands in the fold and in the ``action_index`` projection.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from continuum.mcp.authz import AuthorizationPolicy
from continuum.mcp.server import build_server
from continuum.models import ConsumedInputs
from continuum.storage import SQLiteStorage
from tests.mcp_helpers import fake_context as _ctx

TEST_CLIENT = "pytest-client"


@pytest.fixture
def server_ctx() -> Iterator[tuple[Any, Any]]:
    """A server whose caller is authorized to mutate (mirrors test_mcp_server)."""
    storage = SQLiteStorage(":memory:")
    server, ctx = build_server(storage=storage, policy=AuthorizationPolicy([TEST_CLIENT]))
    yield server, ctx
    ctx.close()


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool the way a client would and parse its JSON result."""
    result = await server.call_tool(name, arguments, context=_ctx(TEST_CLIENT))
    assert result.content, f"{name} returned no content"
    return json.loads(result.content[0].text)


async def seed_run(server: Any, run_id: str = "run_1") -> None:
    await call(
        server,
        "continuum_record_progress",
        run_id=run_id,
        completed=20,
        total=100,
        goal="Analyze 100 documents",
    )


async def test_complete_tool_records_consumed_inputs(server_ctx: tuple[Any, Any]) -> None:
    """The commitment an agent reports must reach the fold and the index."""
    server, ctx = server_ctx
    await seed_run(server)
    done = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="db.write",
        arguments={"row": 7},
        key="ci:row-7",
    )
    await call(
        server,
        "continuum_complete_action",
        run_id="run_1",
        action_key=done["action_key"],
        consumed_inputs={
            "checkpoint_seq": 3,
            "event_positions": [4, 5],
            "component_ids": ["finding_1"],
            "action_ids": ["action_prev"],
        },
    )
    folded = ctx.ledger("run_1").folded()
    stored = folded[done["action_key"]].consumed_inputs
    assert stored == ConsumedInputs(
        checkpoint_seq=3,
        event_positions=[4, 5],
        component_ids=["finding_1"],
        action_ids=["action_prev"],
    )
    row = ctx.storage._connection.execute(
        "SELECT action_json FROM action_index WHERE key = ?", (done["action_key"],)
    ).fetchone()
    assert row is not None
    assert json.loads(row["action_json"])["consumed_inputs"]["checkpoint_seq"] == 3


async def test_complete_tool_defaults_to_empty(server_ctx: tuple[Any, Any]) -> None:
    """Omitting the param keeps old callers admissible (issue #558)."""
    server, ctx = server_ctx
    await seed_run(server)
    done = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="db.write",
        arguments={"row": 8},
        key="ci:row-8",
    )
    await call(server, "continuum_complete_action", run_id="run_1", action_key=done["action_key"])
    folded = ctx.ledger("run_1").folded()
    assert folded[done["action_key"]].consumed_inputs == ConsumedInputs()


async def test_complete_tool_rejects_malformed_inputs(server_ctx: tuple[Any, Any]) -> None:
    """A bad commitment is a validation refusal, not a stored row (fail closed)."""
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    await seed_run(server)
    done = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="db.write",
        arguments={"row": 9},
        key="ci:row-9",
    )
    with pytest.raises(ToolError, match="checkpoint_seq"):
        await server.call_tool(
            "continuum_complete_action",
            {
                "run_id": "run_1",
                "action_key": done["action_key"],
                "consumed_inputs": {"checkpoint_seq": -1},
            },
            context=_ctx(TEST_CLIENT),
        )
    folded = ctx.ledger("run_1").folded()
    assert folded[done["action_key"]].consumed_inputs == ConsumedInputs()


async def test_reconcile_tool_records_consumed_inputs(server_ctx: tuple[Any, Any]) -> None:
    """The evidence route carries commitments too (issue #558, #366)."""
    server, ctx = server_ctx
    await seed_run(server)
    done = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="card.charge",
        arguments={"amount": 100},
        key="ci:charge-100",
    )
    await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=done["action_key"],
        error="gateway timeout after the charge request was sent",
        certain=False,
    )
    await call(
        server,
        "continuum_reconcile_action",
        run_id="run_1",
        action_key=done["action_key"],
        occurred=True,
        note="charge visible in ledger dashboard",
        consumed_inputs={"checkpoint_seq": 1, "action_ids": ["action_probe"]},
    )
    folded = ctx.ledger("run_1").folded()
    stored = folded[done["action_key"]].consumed_inputs
    assert stored.checkpoint_seq == 1
    assert stored.action_ids == ["action_probe"]
    assert folded[done["action_key"]].status.value == "completed"
