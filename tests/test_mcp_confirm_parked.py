"""Tests for MCP continuum_confirm parked buffering for low-risk confirmations (issue #1410).

Verifies that low-risk confirmations buffer as parked items without requiring
the operator confirmation secret, while immediate/high-risk actions and general
progress confirmations continue to enforce the dedicated confirmation secret.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from continuum.mcp.authz import AuthorizationPolicy, ConfirmPolicy
from continuum.mcp.server import build_server
from continuum.models import Run
from continuum.recovery.review_queue import ReviewQueue
from continuum.storage import SQLiteStorage
from tests.mcp_helpers import fake_context

ALLOWED_CLIENT = "trusted-client"
STRANGER_CLIENT = "stranger-client"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStorage]:
    db = tmp_path / "mcp_confirm_test.db"
    storage = SQLiteStorage(f"sqlite:///{db}")
    storage.create_run(Run(run_id="run_1", goal="test parked confirmation"))
    yield storage
    storage.close()


@pytest.fixture
def server_no_token(store: SQLiteStorage) -> Any:
    policy = AuthorizationPolicy([ALLOWED_CLIENT])
    confirm_policy = ConfirmPolicy()  # No token configured
    srv, ctx = build_server(storage=store, policy=policy, confirm_auth=confirm_policy)
    return srv, ctx


@pytest.fixture
def server_with_token(store: SQLiteStorage) -> Any:
    policy = AuthorizationPolicy([ALLOWED_CLIENT])
    confirm_policy = ConfirmPolicy(expected="secret-token")
    srv, ctx = build_server(storage=store, policy=policy, confirm_auth=confirm_policy)
    return srv, ctx


@pytest.mark.asyncio
async def test_low_risk_action_buffers_as_parked_without_confirm_token(
    server_no_token: tuple[Any, Any],
) -> None:
    server, ctx = server_no_token
    # Low-risk action (default weight 0.0 < 0.8) buffers rather than failing
    result = await server.call_tool(
        "continuum_confirm",
        {
            "run_id": "run_1",
            "action_type": "read_query",
            "arguments": {"sql": "SELECT 1"},
        },
        context=fake_context(ALLOWED_CLIENT),
    )

    data = json.loads(result.content[0].text)
    assert data["status"] == "parked"
    assert data["actionable"] is False
    assert "batch_id" in data
    assert "review_id" in data

    # Verify review item exists in queue
    queue = ReviewQueue(ctx.storage)
    pending = queue.list_pending("run_1")
    assert len(pending) == 1
    assert pending[0].action_type == "read_query"
    assert pending[0].parked is True


@pytest.mark.asyncio
async def test_high_risk_action_fails_immediately_without_confirm_token(
    server_no_token: tuple[Any, Any],
) -> None:
    server, _ = server_no_token
    # Set up escalation policy where mem_delete is 0.9 (>= 0.8)
    policy_path = Path(".continuum/escalation.json")
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "blast_radius_threshold": 0.8,
                "risk_weights": {"mem_delete": 0.9, "default": 0.0},
            }
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(ToolError, match="CONTINUUM_MCP_CONFIRM_TOKEN"):
            await server.call_tool(
                "continuum_confirm",
                {
                    "run_id": "run_1",
                    "action_type": "mem_delete",
                    "arguments": {"key": "secret"},
                },
                context=fake_context(ALLOWED_CLIENT),
            )
    finally:
        if policy_path.exists():
            policy_path.unlink()


@pytest.mark.asyncio
async def test_general_confirm_without_token_still_refuses(
    server_no_token: tuple[Any, Any],
) -> None:
    server, _ = server_no_token
    with pytest.raises(ToolError, match="CONTINUUM_MCP_CONFIRM_TOKEN"):
        await server.call_tool(
            "continuum_confirm",
            {"run_id": "run_1"},
            context=fake_context(ALLOWED_CLIENT),
        )


@pytest.mark.asyncio
async def test_missing_run_raises_tool_error(
    server_no_token: tuple[Any, Any],
) -> None:
    server, _ = server_no_token
    with pytest.raises(ToolError, match="no such run: 'missing'"):
        await server.call_tool(
            "continuum_confirm",
            {"run_id": "missing", "action_type": "read_query"},
            context=fake_context(ALLOWED_CLIENT),
        )
