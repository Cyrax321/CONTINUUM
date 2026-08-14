"""MCP server tests, driven through the real tool-dispatch path.

Tools are invoked via ``server.call_tool`` rather than by calling the Python
functions directly, so argument coercion and result serialisation are exercised
the same way an MCP client would exercise them.

The behaviour that matters most is the last section: the server must never hand
back a "proceed" signal for an action whose outcome is unknown. Everything else
is plumbing by comparison.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.mcp.authz import AuthorizationPolicy
from continuum.mcp.server import (
    DEFAULT_DB,
    ContinuumMCP,
    _open_server_storage,
    build_server,
    resolve_database,
)
from continuum.models import ActionStatus, RecoveryMode, Run
from continuum.storage import SQLiteStorage
from tests.mcp_helpers import fake_context as _ctx

TEST_CLIENT = "pytest-client"


@pytest.fixture
def server_ctx() -> Iterator[tuple[Any, Any]]:
    """A server whose caller is authorized to mutate.

    These tests cover tool behaviour, not the authorization layer — that lives
    in test_mcp_authz.py. Without an explicit policy the server denies every
    mutation, and a policy failure here would look like a logic bug.
    """
    storage = SQLiteStorage(":memory:")
    server, ctx = build_server(storage=storage, policy=AuthorizationPolicy([TEST_CLIENT]))
    yield server, ctx
    ctx.close()


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool the way a client would and parse its JSON result."""
    result = await server.call_tool(name, arguments, context=_ctx(TEST_CLIENT))
    assert result.content, f"{name} returned no content"
    return json.loads(result.content[0].text)


async def seed_run(server: Any, run_id: str = "run_1", completed: int = 20) -> None:
    await call(
        server,
        "continuum_record_progress",
        run_id=run_id,
        completed=completed,
        total=100,
        goal="Analyze 100 documents",
    )


# --- registration ----------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_tool_is_registered(server_ctx: tuple[Any, Any]) -> None:
    server, _ = server_ctx
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "continuum_record_progress",
        "continuum_checkpoint",
        "continuum_validate",
        "continuum_resume",
        "continuum_intercept_action",
        "continuum_complete_action",
        "continuum_fail_action",
        "continuum_reconcile_action",
        "continuum_list_actions",
    }


@pytest.mark.asyncio
async def test_read_only_tools_are_annotated_as_such(server_ctx: tuple[Any, Any]) -> None:
    """Clients use this hint to decide what is safe to call unprompted."""
    server, _ = server_ctx
    hints = {t.name: t.annotations.read_only_hint for t in await server.list_tools()}
    assert hints["continuum_validate"] is True
    assert hints["continuum_resume"] is True
    assert hints["continuum_list_actions"] is True
    assert hints["continuum_checkpoint"] is False
    assert hints["continuum_intercept_action"] is False


@pytest.mark.asyncio
async def test_read_only_tools_do_not_write_events(server_ctx: tuple[Any, Any]) -> None:
    """The read-only guarantee must be behavioral, not just declared. A bare run
    (row exists, no history) must be listable without the tool appending a
    RUN_STARTED event of its own (issue #20)."""
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="bare", goal="g"))
    assert [e.type.value for e in ctx.storage.read_events("bare")] == []
    # list_actions returns empty without writing.
    await call(server, "continuum_list_actions", run_id="bare")
    assert [e.type.value for e in ctx.storage.read_events("bare")] == []
    # validate may error on a run with no history, but must not write either.
    with pytest.raises(ToolError):
        await server.call_tool("continuum_validate", {"run_id": "bare"}, context=_ctx(TEST_CLIENT))
    assert [e.type.value for e in ctx.storage.read_events("bare")] == []


@pytest.mark.asyncio
async def test_tools_describe_when_they_matter(server_ctx: tuple[Any, Any]) -> None:
    """An LLM picks tools from descriptions; vague ones get called wrongly."""
    server, _ = server_ctx
    described = {t.name: (t.description or "") for t in await server.list_tools()}
    assert "before resuming" in described["continuum_resume"].lower()
    assert "read-only" in described["continuum_validate"].lower()
    assert "do not repeat" in described["continuum_intercept_action"].lower()


# --- progress and checkpointing --------------------------------------------- #


@pytest.mark.asyncio
async def test_record_progress_creates_the_run_on_first_call(
    server_ctx: tuple[Any, Any],
) -> None:
    server, ctx = server_ctx
    payload = await call(
        server,
        "continuum_record_progress",
        run_id="run_1",
        completed=10,
        total=100,
        goal="Analyze 100 documents",
    )
    assert payload["completed"] == 10
    assert payload["pending"] == 90
    assert ctx.storage.get_run("run_1").goal == "Analyze 100 documents"


@pytest.mark.asyncio
async def test_progress_accumulates_across_calls(server_ctx: tuple[Any, Any]) -> None:
    server, _ = server_ctx
    await seed_run(server, completed=20)
    payload = await call(
        server, "continuum_record_progress", run_id="run_1", completed=55, total=100
    )
    assert payload["completed"] == 55
    assert payload["pending"] == 45


@pytest.mark.asyncio
async def test_over_total_progress_is_rejected_before_being_written(
    server_ctx: tuple[Any, Any],
) -> None:
    """An over-total update must fail before any TASK_UPDATED is appended, or the
    run's log stays intact yet unprojectable and every later checkpoint/resume
    fails with a raw pydantic ValidationError (issue #15)."""
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="run_1", goal="g"))
    with pytest.raises(ToolError, match="exceeds total"):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 15, "total": 10, "goal": "g"},
            context=_ctx(TEST_CLIENT),
        )
    # Nothing beyond the start event was written: the log is not poisoned.
    assert [e.type.value for e in ctx.storage.read_events("run_1")] == ["RUN_STARTED"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "negative_update",
    [
        {"completed": -5},
        {"completed": 0, "failed": -1},
    ],
)
async def test_negative_progress_is_rejected_before_being_written_without_total(
    server_ctx: tuple[Any, Any],
    negative_update: dict[str, int],
) -> None:
    """Negative counters must be rejected even when `total` is omitted, or the
    bad event is written and every later projection/checkpoint/resume fails
    permanently (issue #38)."""
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="run_1", goal="g"))
    arguments = {"run_id": "run_1", "goal": "g", **negative_update}
    with pytest.raises(ToolError, match="must be non-negative"):
        await server.call_tool(
            "continuum_record_progress",
            arguments,
            context=_ctx(TEST_CLIENT),
        )
    # Nothing beyond the start event was written: the log is not poisoned.
    assert [e.type.value for e in ctx.storage.read_events("run_1")] == ["RUN_STARTED"]


@pytest.mark.asyncio
async def test_recording_progress_for_an_unknown_run_without_a_goal_fails(
    server_ctx: tuple[Any, Any],
) -> None:
    """Silently inventing a run for a typo'd id would scatter work across
    phantom runs, so the error surfaces to the caller instead."""
    from mcp.server.mcpserver.exceptions import ToolError

    server, _ = server_ctx
    with pytest.raises(ToolError, match="no such run"):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "ghost", "completed": 1},
            context=_ctx(TEST_CLIENT),
        )


@pytest.mark.asyncio
async def test_checkpoint_returns_a_sealed_record(server_ctx: tuple[Any, Any]) -> None:
    server, ctx = server_ctx
    await seed_run(server)
    payload = await call(
        server,
        "continuum_checkpoint",
        run_id="run_1",
        reason="milestone",
        env={"dataset": "v3"},
    )
    assert payload["version"] == 0
    assert payload["integrity_hash"]
    assert payload["completed"] == 20

    stored = ctx.storage.get_checkpoint(payload["checkpoint_id"])
    assert stored.verify()


@pytest.mark.asyncio
async def test_successive_checkpoints_increment_the_version(
    server_ctx: tuple[Any, Any],
) -> None:
    server, _ = server_ctx
    await seed_run(server, completed=20)
    first = await call(server, "continuum_checkpoint", run_id="run_1")
    await call(server, "continuum_record_progress", run_id="run_1", completed=40, total=100)
    second = await call(server, "continuum_checkpoint", run_id="run_1")
    assert (first["version"], second["version"]) == (0, 1)


# --- validation and recovery ------------------------------------------------ #


@pytest.mark.asyncio
async def test_agent_self_reported_state_is_never_reported_as_verified(
    server_ctx: tuple[Any, Any],
) -> None:
    """An agent cannot certify its own work as safe.

    Everything written through MCP is an unverified self-report, so even with
    a stable environment the run needs review rather than a clean bill. This
    previously returned safe=True, which let an agent fabricate progress and
    then have CONTINUUM confirm it was fine to continue.
    """
    server, _ = server_ctx
    await seed_run(server)
    await call(server, "continuum_checkpoint", run_id="run_1", env={"dataset": "v3"})

    payload = await call(server, "continuum_validate", run_id="run_1", env={"dataset": "v3"})
    assert payload["safe"] is False
    assert payload["mode"] != RecoveryMode.RESUME.value
    assert payload["environment_changes"] == []  # the environment really is clean
    statuses = {(e["component"], e["status"]) for e in payload["components"]}
    assert ("progress", "requires_review") in statuses
    assert ("goal", "requires_review") in statuses


@pytest.mark.asyncio
async def test_validate_flags_a_changed_dependency(server_ctx: tuple[Any, Any]) -> None:
    server, ctx = server_ctx
    await seed_run(server)
    ctx.storage.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
    )
    await call(server, "continuum_checkpoint", run_id="run_1", env={"dataset": "v3"})

    payload = await call(server, "continuum_validate", run_id="run_1", env={"dataset": "v4"})
    assert payload["safe"] is False
    assert any("v3 -> v4" in c for c in payload["environment_changes"])
    assert any(
        e["component"] == "external_dependency" and e["status"] == "conflicted"
        for e in payload["components"]
    )


@pytest.mark.asyncio
async def test_validate_does_not_mutate_the_run(server_ctx: tuple[Any, Any]) -> None:
    """Read-only must mean read-only; clients may call it speculatively."""
    server, ctx = server_ctx
    await seed_run(server)
    await call(server, "continuum_checkpoint", run_id="run_1", env={"dataset": "v3"})
    before = (ctx.storage.last_sequence("run_1"), list(ctx.storage.list_versions("run_1")))

    await call(server, "continuum_validate", run_id="run_1", env={"dataset": "v4"})
    await call(server, "continuum_resume", run_id="run_1", env={"dataset": "v4"})

    after = (ctx.storage.last_sequence("run_1"), list(ctx.storage.list_versions("run_1")))
    assert before == after


@pytest.mark.asyncio
async def test_resume_returns_a_contract_and_next_action(
    server_ctx: tuple[Any, Any],
) -> None:
    server, ctx = server_ctx
    await seed_run(server)
    ctx.storage.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
    )
    await call(server, "continuum_checkpoint", run_id="run_1", env={"dataset": "v3"})

    payload = await call(server, "continuum_resume", run_id="run_1", env={"dataset": "v4"})
    # Self-reported state outranks the dependency change: REQUEST_HUMAN (4)
    # beats REPAIR_AND_RESUME (1) on the severity ordering.
    assert payload["mode"] == RecoveryMode.REQUEST_HUMAN.value
    assert payload["safe"] is False
    assert any(r["kind"] == "revalidate_dependency" for r in payload["repairs"])
    assert payload["contract"]["integrity_hash"]
    assert payload["repairs"]
    assert payload["progress"]["completed"] == 20


@pytest.mark.asyncio
async def test_deterministic_state_still_resumes_cleanly(
    server_ctx: tuple[Any, Any],
) -> None:
    """The provenance check must not block genuinely verified state.

    Written through the storage API directly (as the CLI or an in-process
    adapter would), the same run resumes cleanly — proving the gate keys on
    *who asserted it*, not on some blanket refusal.
    """
    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="run_det", goal="Analyze 100 documents"))
    ctx.storage.append_event(
        "run_det", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
    )
    for i in range(20):
        ctx.storage.append_event("run_det", EventType.WORK_COMPLETED, {"doc": i})
    CheckpointManager(ctx.storage).checkpoint(
        "run_det", environment=capture("run_det", StaticProvider(dataset="v3"))
    )

    payload = await call(server, "continuum_resume", run_id="run_det", env={"dataset": "v3"})
    assert payload["mode"] == RecoveryMode.RESUME.value
    assert payload["safe"] is True
    assert payload["next_allowed_action"] is None


# --- the core guarantee: never say "proceed" on an uncertain effect --------- #


@pytest.mark.asyncio
async def test_a_first_claim_is_permitted(server_ctx: tuple[Any, Any]) -> None:
    server, _ = server_ctx
    await seed_run(server)
    payload = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments={"title": "Bug"},
    )
    assert payload["proceed"] is True
    assert payload["action_key"]


@pytest.mark.asyncio
async def test_a_completed_action_is_never_repeated(server_ctx: tuple[Any, Any]) -> None:
    server, _ = server_ctx
    await seed_run(server)
    args = {"title": "Bug"}

    first = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    await call(
        server,
        "continuum_complete_action",
        run_id="run_1",
        action_key=first["action_key"],
        external_id="481",
        result={"url": "/issues/481"},
    )

    second = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    assert second["proceed"] is False
    assert second["external_id"] == "481"
    assert second["previous_result"] == {"url": "/issues/481"}
    assert "do not repeat" in second["guidance"].lower()


@pytest.mark.asyncio
async def test_a_stable_key_deduplicates_across_argument_shape_changes(
    server_ctx: tuple[Any, Any],
) -> None:
    """The e2e-autonomy-test regression (issue #6).

    Session 1 recorded an invoice send with a relative path argument; session 2
    passed an absolute path. Hashing raw arguments made the two look like
    different actions and intercept_action answered proceed=true for an invoice
    that was already sent. A stable `key` derived from the resource identity must
    make the second claim a no-op regardless of argument formatting.
    """
    server, _ = server_ctx
    await seed_run(server)

    first = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="send_invoice",
        arguments={"external_id": "INV-001.sent", "path": "INV-001.sent"},
        key="invoice:INV-001",
    )
    assert first["proceed"] is True
    await call(
        server,
        "continuum_complete_action",
        run_id="run_1",
        action_key=first["action_key"],
        external_id="INV-001.sent",
    )

    second = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="send_invoice",
        arguments={
            "external_id": "/tmp/e2e-outbox/INV-001.sent",
            "path": "/tmp/e2e-outbox/INV-001.sent",
        },
        key="invoice:INV-001",
    )
    assert second["proceed"] is False
    assert second["external_id"] == "INV-001.sent"


@pytest.mark.asyncio
async def test_an_unknown_outcome_refuses_to_grant_proceed(
    server_ctx: tuple[Any, Any],
) -> None:
    """The guarantee this server exists to preserve.

    A claim left in flight (crash, or a client that never reported back) means
    the side effect may or may not have happened. The server must refuse to
    authorise a retry rather than risk duplicating it.
    """
    server, ctx = server_ctx
    await seed_run(server)
    args = {"title": "Bug"}

    first = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    assert first["proceed"] is True  # claimed, then the client vanishes

    second = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    assert second["proceed"] is False
    assert second["status"] == ActionStatus.UNKNOWN.value
    assert "do not retry" in second["guidance"].lower()

    pending = ActionLedger(ctx.storage, "run_1").pending()
    assert len(pending) == 1
    assert pending[0].side_effect_uncertain


@pytest.mark.asyncio
async def test_a_started_action_blocks_resume(server_ctx: tuple[Any, Any]) -> None:
    """An unreported claim must stop the agent, not just the retry."""
    server, _ = server_ctx
    await seed_run(server)
    await call(server, "continuum_checkpoint", run_id="run_1", env={"dataset": "v3"})
    await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments={"title": "Bug"},
    )

    payload = await call(server, "continuum_resume", run_id="run_1", env={"dataset": "v3"})
    assert payload["safe"] is False
    assert payload["mode"] == RecoveryMode.REQUEST_HUMAN.value
    assert payload["uncertain_actions"]
    assert payload["next_allowed_action"].startswith("reconcile_action:")


@pytest.mark.asyncio
async def test_a_timeout_is_recorded_as_uncertain_by_default(
    server_ctx: tuple[Any, Any],
) -> None:
    """certain defaults to False: a timeout is not proof nothing happened."""
    server, _ = server_ctx
    await seed_run(server)
    claim = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="stripe.charge",
        arguments={"amount": 5000},
    )
    payload = await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claim["action_key"],
        error="timeout after 30s",
    )
    assert payload["status"] == ActionStatus.UNKNOWN.value
    assert payload["side_effect_uncertain"] is True


@pytest.mark.asyncio
async def test_a_definite_failure_frees_the_action_for_retry(
    server_ctx: tuple[Any, Any],
) -> None:
    server, _ = server_ctx
    await seed_run(server)
    args = {"amount": 5000}
    claim = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="stripe.charge",
        arguments=args,
    )
    payload = await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claim["action_key"],
        error="400 invalid card",
        certain=True,
    )
    assert payload["status"] == ActionStatus.FAILED.value

    retry = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="stripe.charge",
        arguments=args,
    )
    assert retry["proceed"] is True


@pytest.mark.asyncio
async def test_reconciling_as_occurred_prevents_any_repeat(
    server_ctx: tuple[Any, Any],
) -> None:
    server, _ = server_ctx
    await seed_run(server)
    args = {"title": "Bug"}
    claim = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claim["action_key"],
        error="connection lost",
    )
    await call(
        server,
        "continuum_reconcile_action",
        run_id="run_1",
        action_key=claim["action_key"],
        occurred=True,
        external_id="481",
    )

    repeat = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    assert repeat["proceed"] is False
    assert repeat["external_id"] == "481"


@pytest.mark.asyncio
async def test_reconciling_as_absent_permits_a_retry(server_ctx: tuple[Any, Any]) -> None:
    server, _ = server_ctx
    await seed_run(server)
    args = {"title": "Bug"}
    claim = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claim["action_key"],
        error="connection lost",
    )
    await call(
        server,
        "continuum_reconcile_action",
        run_id="run_1",
        action_key=claim["action_key"],
        occurred=False,
        note="no matching issue",
    )

    retry = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments=args,
    )
    assert retry["proceed"] is True


@pytest.mark.asyncio
async def test_different_arguments_are_different_actions(
    server_ctx: tuple[Any, Any],
) -> None:
    server, _ = server_ctx
    await seed_run(server)
    first = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments={"title": "Bug A"},
    )
    await call(
        server,
        "continuum_complete_action",
        run_id="run_1",
        action_key=first["action_key"],
        external_id="1",
    )
    second = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments={"title": "Bug B"},
    )
    assert second["proceed"] is True


@pytest.mark.asyncio
async def test_list_actions_surfaces_unresolved_outcomes(
    server_ctx: tuple[Any, Any],
) -> None:
    server, _ = server_ctx
    await seed_run(server)
    done = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="a.do",
        arguments={"n": 1},
    )
    await call(server, "continuum_complete_action", run_id="run_1", action_key=done["action_key"])
    await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="b.do",
        arguments={"n": 2},
    )

    payload = await call(server, "continuum_list_actions", run_id="run_1")
    assert len(payload["actions"]) == 2
    assert payload["unresolved"] == 1


# --- storage configuration --------------------------------------------------- #


def test_the_database_defaults_to_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTINUUM_DB", raising=False)
    assert resolve_database() == DEFAULT_DB


def test_an_env_var_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUUM_DB", "/tmp/from-env.db")
    assert resolve_database() == "/tmp/from-env.db"


def test_an_explicit_argument_wins_over_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTINUUM_DB", "/tmp/from-env.db")
    assert resolve_database("/tmp/explicit.db") == "/tmp/explicit.db"


def _orphan_wal_sidecars(path: str) -> None:
    """Recreate the sidecar files a hard-killed server leaves behind.

    SIGKILL denies SQLite its WAL checkpoint, so ``<db>-wal`` and ``<db>-shm``
    survive next to the database. On the affected platform reopening in WAL mode
    then failed with a disk I/O error; here the files stand in for that state so
    the recovery has something concrete to remove. The OS-level error itself is
    injected by the tests, because a crafted sidecar does not raise it on every
    filesystem (SQLite often just rebuilds it).
    """
    with open(f"{path}-wal", "wb"):
        pass
    with open(f"{path}-shm", "wb") as shm:
        shm.write(b"\x00" * 32768)


def test_server_startup_recovers_from_orphaned_wal_sidecars(tmp_path: Any) -> None:
    """A hard-killed predecessor must not wedge the next server launch.

    The first WAL-mode open raises the disk I/O error a crashed predecessor's
    sidecars provoke; the server's open path must then clear those sidecars and
    retry, coming up with the database's prior contents still readable. The
    ``ContinuumMCP`` constructor is checked too, since that is what the entry
    point builds.

    The error is injected rather than provoked from the crafted sidecars: they
    reliably reproduce it only on the filesystem the bug was seen on, so forcing
    the first open to fail while a sidecar is present keeps the test
    deterministic while still driving the exact recovery branch.
    """
    path = str(tmp_path / "agent.db")
    seed = SQLiteStorage(path)
    seed.create_run(Run(run_id="survivor", goal="pre-crash work"))
    seed.append_event("survivor", EventType.RUN_STARTED, {"goal": "pre-crash work"})
    seed.close()

    _orphan_wal_sidecars(path)
    assert os.path.exists(f"{path}-wal") and os.path.exists(f"{path}-shm")

    real_init = SQLiteStorage.__init__
    calls = {"n": 0}

    def _fail_while_sidecar_present(self: Any, database: str, *args: Any, **kwargs: Any) -> None:
        # Fail as an orphaned-WAL open does, but only while the stale sidecar is
        # still there. Once recovery removes it, the retry runs the real
        # constructor and succeeds.
        calls["n"] += 1
        if os.path.exists(f"{database}-wal"):
            raise sqlite3.OperationalError("disk I/O error")
        real_init(self, database, *args, **kwargs)

    SQLiteStorage.__init__ = _fail_while_sidecar_present  # type: ignore[method-assign]
    try:
        # The wal was removed between the two opens: the mid-open callback saw
        # it present on the failing attempt and absent on the succeeding one.
        removed_between: list[bool] = []
        real_remove = os.remove

        def _record_remove(target: str) -> None:
            removed_between.append(True)
            real_remove(target)

        os.remove = _record_remove  # type: ignore[assignment]
        try:
            storage = _open_server_storage(path)
        finally:
            os.remove = real_remove  # type: ignore[assignment]
        try:
            # The recovery ran: a failed open, a sidecar removal, then a retry.
            assert calls["n"] == 2
            assert removed_between  # at least one stale sidecar was cleared
            # And the pre-crash work survived the sidecar removal.
            assert storage.get_run("survivor").goal == "pre-crash work"
            assert [e.type for e in storage.read_events("survivor")] == [EventType.RUN_STARTED]
        finally:
            storage.close()

        # The full server object routes through the same recovery.
        _orphan_wal_sidecars(path)
        calls["n"] = 0
        ctx = ContinuumMCP(path)
        try:
            assert calls["n"] == 2
            assert ctx.storage.get_run("survivor").goal == "pre-crash work"
        finally:
            ctx.close()
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]


def test_server_open_reraises_a_disk_error_with_no_sidecars(tmp_path: Any) -> None:
    """A disk I/O error that is not a stale sidecar must still surface.

    With nothing to remove, the recovery has no business swallowing the error
    or retrying an open that would fail identically.
    """
    path = str(tmp_path / "agent.db")
    SQLiteStorage(path).close()

    calls = {"n": 0}
    real_init = SQLiteStorage.__init__

    def _always_disk_error(self: Any, database: str, *args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    SQLiteStorage.__init__ = _always_disk_error  # type: ignore[method-assign]
    try:
        with pytest.raises(sqlite3.OperationalError):
            _open_server_storage(path)
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]

    # One attempt, then re-raise: no retry when there was nothing to clear.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_state_persists_across_server_restarts(tmp_path: Any) -> None:
    """A restarted MCP server must see the previous session's work."""
    path = str(tmp_path / "agent.db")

    server, ctx = build_server(path, policy=AuthorizationPolicy([TEST_CLIENT]))
    await call(
        server,
        "continuum_record_progress",
        run_id="run_1",
        completed=42,
        total=100,
        goal="Analyze",
    )
    claim = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments={"title": "Bug"},
    )
    await call(
        server,
        "continuum_complete_action",
        run_id="run_1",
        action_key=claim["action_key"],
        external_id="481",
    )
    ctx.close()

    server2, ctx2 = build_server(path, policy=AuthorizationPolicy([TEST_CLIENT]))
    repeat = await call(
        server2,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="github.create_issue",
        arguments={"title": "Bug"},
    )
    assert repeat["proceed"] is False
    assert repeat["external_id"] == "481"

    actions = await call(server2, "continuum_list_actions", run_id="run_1")
    assert actions["unresolved"] == 0
    ctx2.close()


@pytest.mark.asyncio
async def test_an_existing_run_is_reused_not_recreated(
    server_ctx: tuple[Any, Any],
) -> None:
    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="run_1", goal="Existing goal"))
    await call(server, "continuum_record_progress", run_id="run_1", completed=5, goal="Ignored")
    assert ctx.storage.get_run("run_1").goal == "Existing goal"


@pytest.mark.asyncio
async def test_a_run_missing_run_started_is_backfilled(
    server_ctx: tuple[Any, Any],
) -> None:
    """A row created through the storage API has no events; the log gets one."""
    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="bare", goal="Created directly"))
    assert ctx.storage.read_events("bare") == []

    payload = await call(server, "continuum_record_progress", run_id="bare", completed=3)

    events = ctx.storage.read_events("bare")
    assert events[0].type is EventType.RUN_STARTED
    assert events[0].payload["goal"] == "Created directly"
    assert payload["completed"] == 3


@pytest.mark.asyncio
async def test_a_log_not_beginning_with_run_started_is_refused(
    server_ctx: tuple[Any, Any],
) -> None:
    """Backfilling behind existing history would misorder the run.

    If some other writer appends before RUN_STARTED, inserting the start event
    afterwards would place the run's beginning *after* events that supposedly
    preceded it. The resulting projection would be wrong in a way nothing
    downstream can detect, so this raises instead — naming the problem beats
    silently producing bad state.
    """
    from continuum.mcp.server import MalformedRunLog

    server, ctx = server_ctx
    ctx.storage.create_run(Run(run_id="odd", goal="Out of order"))
    ctx.storage.append_event("odd", EventType.TOOL_CALLED, {"tool": "search"})

    with pytest.raises(MalformedRunLog, match="does not begin with RUN_STARTED"):
        ctx.ensure_run("odd")

    # surfaced to the MCP caller rather than swallowed
    with pytest.raises(Exception, match="RUN_STARTED"):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "odd", "completed": 1},
            context=_ctx(TEST_CLIENT),
        )

    # nothing was written by the refused call
    assert [e.type for e in ctx.storage.read_events("odd")] == [EventType.TOOL_CALLED]


@pytest.mark.asyncio
async def test_backfill_is_not_repeated_on_later_calls(
    server_ctx: tuple[Any, Any],
) -> None:
    """Exactly one RUN_STARTED, however many tools are called."""
    server, ctx = server_ctx
    await seed_run(server, completed=5)
    await call(server, "continuum_record_progress", run_id="run_1", completed=10, total=100)
    await call(server, "continuum_checkpoint", run_id="run_1")
    await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="a.do",
        arguments={"n": 1},
    )

    starts = [e for e in ctx.storage.read_events("run_1") if e.type is EventType.RUN_STARTED]
    assert len(starts) == 1
