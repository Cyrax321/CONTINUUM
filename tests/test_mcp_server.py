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
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.mcp.authz import (
    CLIENT_TOKENS_ENV_VAR,
    CONFIRM_ENV_VAR,
    AuthorizationPolicy,
)
from continuum.mcp.server import (
    DEFAULT_DB,
    ContinuumMCP,
    _open_server_storage,
    build_server,
    main,
    resolve_database,
)
from continuum.models import ActionStatus, Origin, RecoveryMode, Run
from continuum.state.semantic import project
from continuum.storage import RunNotFound, SQLiteStorage
from tests.mcp_helpers import fake_context as _ctx

TEST_CLIENT = "pytest-client"


@pytest.fixture(autouse=True)
def _no_confirm_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the confirmation gate deterministic (issue #201).

    The confirm policy refuses by default; a stray CONTINUUM_MCP_CONFIRM_TOKEN
    in the operator's environment would silently flip that for every test here.
    """
    monkeypatch.delenv(CONFIRM_ENV_VAR, raising=False)


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


def seed_human_confirmation(server_ctx: tuple[Any, Any], run_id: str = "run_1") -> None:
    """Record REVIEW_CONFIRMED the way the human CLI path does (issue #201).

    ``continuum_confirm`` over MCP now refuses without CONTINUUM_MCP_CONFIRM_TOKEN
    (an agent must not confirm its own self-report), so a test that only wants
    the confirmation on record writes the same event directly, sourced from
    Origin.HUMAN exactly as ``continuum confirm`` writes it.
    """
    _, ctx = server_ctx
    ctx.storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
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
        "continuum_confirm",
        "continuum_record_summary",
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
    # Nothing was written: no TASK_UPDATED (issue #15), and since issue #203
    # not even the RUN_STARTED backfill happens for a rejected call.
    assert [e.type.value for e in ctx.storage.read_events("run_1")] == []


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
    # Nothing was written: no TASK_UPDATED (issue #38), and since issue #203
    # not even the RUN_STARTED backfill happens for a rejected call.
    assert [e.type.value for e in ctx.storage.read_events("run_1")] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejected_update",
    [
        {"completed": -5, "goal": "g"},
        {"completed": 15, "total": 10, "goal": "g"},
    ],
)
async def test_a_rejected_progress_call_writes_nothing_at_all(
    server_ctx: tuple[Any, Any],
    rejected_update: dict[str, Any],
) -> None:
    """A rejected call must not even create the run (issue #203).

    The counter checks used to run after `ensure_run`, so a typo'd or hostile
    call left a goal-bearing run row and a RUN_STARTED event behind — facts no
    tool can delete. Validation now precedes creation, matching the guard's
    own rule that a refusal writes nothing.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    with pytest.raises(ToolError, match="non-negative|exceeds total"):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", **rejected_update},
            context=_ctx(TEST_CLIENT),
        )
    with pytest.raises(RunNotFound):
        ctx.storage.get_run("run_1")


@pytest.mark.asyncio
async def test_progress_over_the_recorded_total_is_rejected_when_total_is_omitted(
    server_ctx: tuple[Any, Any],
) -> None:
    """The `total` guard must apply to the total already on record (issue #364).

    Omitting `total` used to skip the argument check entirely while projection
    still folded the `total` from an earlier event, so the invariant was
    evaluated against a limit the call never mentioned. The event was appended
    before it was projected, which made the rejected value durable.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    await call(
        server,
        "continuum_record_progress",
        run_id="run_1",
        completed=3,
        total=6,
        goal="g",
    )
    before = [e.type.value for e in ctx.storage.read_events("run_1")]

    with pytest.raises(ToolError, match="unprojectable"):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 99},
            context=_ctx(TEST_CLIENT),
        )

    assert [e.type.value for e in ctx.storage.read_events("run_1")] == before


@pytest.mark.asyncio
async def test_a_rejected_progress_call_leaves_the_run_projectable(
    server_ctx: tuple[Any, Any],
) -> None:
    """A refused update must not cost the run its recovery surface (issue #364).

    The fold validates each intermediate state, so a single unprojectable event
    could never be corrected by appending another. Every projecting tool stayed
    dead for that run while the action tools kept working, which let the run go
    on authorising side effects that recovery could no longer reason about.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server, _ = server_ctx
    await call(
        server,
        "continuum_record_progress",
        run_id="run_1",
        completed=3,
        total=6,
        goal="g",
    )
    with pytest.raises(ToolError):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 99},
            context=_ctx(TEST_CLIENT),
        )

    # Each of these folds the log, and each was permanently broken before.
    assert (await call(server, "continuum_record_progress", run_id="run_1", completed=4))[
        "completed"
    ] == 4
    assert await call(server, "continuum_checkpoint", run_id="run_1")
    assert await call(server, "continuum_validate", run_id="run_1")
    assert await call(server, "continuum_resume", run_id="run_1")


@pytest.mark.asyncio
async def test_a_racing_writer_cannot_compose_an_unprojectable_log(
    server_ctx: tuple[Any, Any],
) -> None:
    """Validation and append are two statements, so guard the gap (issue #364).

    Two individually-legal payloads can compose into a log neither would have
    been allowed to produce. Here `completed=75` is validated against `total=100`
    and a second writer lands `total=50` before the append. Without
    `expected_sequence` the stale candidate is committed and the run is
    unprojectable; with it the append is rejected, re-validated against the new
    head, and refused on its own merits.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server, ctx = server_ctx
    await seed_run(server, completed=10)  # total=100

    real_read = ctx.storage.read_events
    real_append = ctx.storage.append_event
    interposed = {"done": False}

    def read_then_let_a_writer_in(run_id: str, **kwargs: Any) -> Any:
        history = real_read(run_id, **kwargs)
        # Only the unbounded read is the one `_project_candidate` validates
        # against; `ensure_run` reads with `upto=1` earlier in the same call.
        if not interposed["done"] and not kwargs and run_id == "run_1":
            interposed["done"] = True
            # A concurrent writer shrinks the total after we have read it.
            real_append(
                "run_1",
                EventType.TASK_UPDATED,
                {"completed": 10, "failed": 0, "total": 50, "pending": 40},
                source=Origin.EXTERNAL_AGENT,
            )
        return history

    ctx.storage.read_events = read_then_let_a_writer_in  # type: ignore[method-assign]
    try:
        with pytest.raises(ToolError, match="unprojectable"):
            await server.call_tool(
                "continuum_record_progress",
                {"run_id": "run_1", "completed": 75},
                context=_ctx(TEST_CLIENT),
            )
    finally:
        ctx.storage.read_events = real_read  # type: ignore[method-assign]

    # The run survived the race: the log still folds, and the racing writer's
    # own event is the one that stands.
    state = project("run_1", ctx.storage.read_events("run_1"))
    assert state.progress.total == 50
    assert state.progress.completed == 10
    assert await call(server, "continuum_resume", run_id="run_1")


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
async def test_an_agent_cannot_confirm_its_own_self_report(
    server_ctx: tuple[Any, Any],
) -> None:
    """The self-certification exploit must not come back through one extra call.

    The 9738b9e fix made record_progress -> checkpoint -> resume answer
    request_human. Adding continuum_confirm (issue #35) gave that same
    allowlisted agent a tool that clears the REQUIRES_REVIEW, so the exploit
    returned as record_progress -> checkpoint -> confirm -> resume with
    safe=True (issue #201). Confirmation over MCP now needs its own secret,
    so an agent that stops at what it is allowed to do cannot unblock itself.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server, _ = server_ctx
    await seed_run(server)
    await call(server, "continuum_checkpoint", run_id="run_1")

    before = await call(server, "continuum_resume", run_id="run_1")
    assert before["safe"] is False
    assert before["mode"] == RecoveryMode.REQUEST_HUMAN.value

    # The caller may mutate (it is allowlisted), but confirming is a different
    # grant: without the operator's confirm secret the tool refuses.
    with pytest.raises(ToolError, match="CONTINUUM_MCP_CONFIRM_TOKEN"):
        await call(server, "continuum_confirm", run_id="run_1")

    after = await call(server, "continuum_resume", run_id="run_1")
    assert after["safe"] is False
    assert after["mode"] == RecoveryMode.REQUEST_HUMAN.value


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


# --- environment declared through the protocol ----------------------------- #
#
# The test above declares the dependency by appending an event straight to
# storage, which no MCP client can do. Checkpointing with ``env`` used to record
# a snapshot and nothing else, and the validator returns early for a state with
# no declared dependencies — so drift was rendered in ``environment_changes``
# while the verdict stayed ``safe``, which is precisely "reported as verified
# when it is not". These drive the whole path through the tools.


@pytest.mark.asyncio
async def test_checkpointing_with_env_declares_the_dependencies(
    server_ctx: tuple[Any, Any],
) -> None:
    """``env`` has to become declared state, not just a snapshot beside it."""
    server, ctx = server_ctx
    await seed_run(server)
    await call(
        server,
        "continuum_checkpoint",
        run_id="run_1",
        env={"dataset": "sha256:aaaa", "schema": "2.1"},
    )

    declared = [
        e for e in ctx.storage.read_events("run_1") if e.type is EventType.DEPENDENCY_DECLARED
    ]
    assert {e.payload["resource"] for e in declared} == {"dataset", "schema"}
    # Written through this server, so it is self-certified like everything else.
    assert {e.source for e in declared} == {Origin.EXTERNAL_AGENT}


@pytest.mark.asyncio
async def test_drift_in_an_env_declared_dependency_blocks_resume(
    server_ctx: tuple[Any, Any],
) -> None:
    """A moved dataset must stop the run even once the self-report is confirmed.

    Confirming clears the REQUIRES_REVIEW on goal and progress, so nothing else
    is left to mask the environment check — if the verdict were still ``safe``
    the agent would resume on top of data that changed underneath it.
    """
    server, _ = server_ctx
    await seed_run(server)
    await call(server, "continuum_checkpoint", run_id="run_1", env={"dataset": "sha256:aaaa"})
    seed_human_confirmation(server_ctx)

    clean = await call(server, "continuum_validate", run_id="run_1", env={"dataset": "sha256:aaaa"})
    assert clean["safe"] is True, "an unchanged environment must still resume"

    drifted = await call(
        server, "continuum_validate", run_id="run_1", env={"dataset": "sha256:bbbb"}
    )
    assert drifted["safe"] is False
    assert "conflicted" in drifted["reason"]
    assert ("external_dependency", "dataset", "conflicted") in {
        (e["component"], e["component_id"], e["status"]) for e in drifted["components"]
    }

    resumed = await call(server, "continuum_resume", run_id="run_1", env={"dataset": "sha256:bbbb"})
    assert resumed["safe"] is False
    assert resumed["next_allowed_action"] == "revalidate_dependency:dataset"
    assert any(r["kind"] == "revalidate_dependency" for r in resumed["repairs"])
    # Repair must not discard work already done.
    assert resumed["progress"]["completed"] == 20


@pytest.mark.asyncio
async def test_only_the_changed_dependency_is_invalidated(
    server_ctx: tuple[Any, Any],
) -> None:
    """Over-blocking is its own failure: untouched resources stay valid."""
    server, _ = server_ctx
    await seed_run(server)
    await call(
        server,
        "continuum_checkpoint",
        run_id="run_1",
        env={"dataset": "sha256:aaaa", "schema": "2.1"},
    )
    seed_human_confirmation(server_ctx)

    payload = await call(
        server,
        "continuum_validate",
        run_id="run_1",
        env={"dataset": "sha256:bbbb", "schema": "2.1"},
    )
    statuses = {
        (e["component_id"], e["status"])
        for e in payload["components"]
        if e["component"] == "external_dependency"
    }
    assert statuses == {("dataset", "conflicted"), ("schema", "valid")}


@pytest.mark.asyncio
async def test_repinning_only_records_what_actually_changed(
    server_ctx: tuple[Any, Any],
) -> None:
    """Checkpointing on a schedule must not append an event per resource per call."""
    server, ctx = server_ctx
    await seed_run(server)

    for _ in range(4):
        await call(
            server,
            "continuum_checkpoint",
            run_id="run_1",
            env={"dataset": "v3", "schema": "1.0"},
        )

    def declared() -> list[Any]:
        return [
            e for e in ctx.storage.read_events("run_1") if e.type is EventType.DEPENDENCY_DECLARED
        ]

    assert len(declared()) == 2, "an unchanged environment re-declared itself"

    await call(
        server, "continuum_checkpoint", run_id="run_1", env={"dataset": "v4", "schema": "1.0"}
    )
    assert len(declared()) == 3, "a genuine re-pin must be recorded"

    state = project("run_1", ctx.storage.read_events("run_1"))
    assert {(d.resource, d.version) for d in state.external_dependencies} == {
        ("dataset", "v4"),
        ("schema", "1.0"),
    }


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


async def test_resume_without_run_id_targets_the_active_run(server_ctx: tuple[Any, Any]) -> None:
    """A fresh session need not remember the old run id to resume it."""
    server, _ = server_ctx
    # No runs yet -> explicit "no active run" signal.
    none = await call(server, "continuum_resume")
    assert none["mode"] == "no_active_run"
    assert none["run_id"] is None
    assert "continuum_record_progress" in none["message"]

    # Seed a run; resume with no id should discover and assess it.
    await seed_run(server, "run_active", completed=30)
    resumed = await call(server, "continuum_resume")
    assert resumed["run_id"] == "run_active"
    assert resumed["progress"]["completed"] == 30
    # The goal comes back so a resumed session knows what to continue, with no
    # external task file.
    assert resumed["goal"] == "Analyze 100 documents"


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


@pytest.mark.asyncio
async def test_list_actions_marks_the_unresolved_row_itself(
    server_ctx: tuple[Any, Any],
) -> None:
    """The aggregate count is not enough; the row has to say which one.

    ``side_effect_uncertain`` is only set once an action has been *escalated* to
    UNKNOWN. An action left STARTED by a process that died mid-flight has not
    been escalated, so that flag reads false while ``continuum_resume`` reports
    the very same action as an unknown outcome. Reading the list row by row
    previously suggested the interrupted side effect was fine.
    """
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
    # Claimed and never completed: the crash-mid-action shape.
    await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="b.do",
        arguments={"n": 2},
    )

    payload = await call(server, "continuum_list_actions", run_id="run_1")
    rows = {a["action_type"]: a for a in payload["actions"]}
    assert rows["a.do"]["outcome_unresolved"] is False
    assert rows["b.do"]["outcome_unresolved"] is True

    # And it agrees with what the recovery decision says about the same action.
    resumed = await call(server, "continuum_resume", run_id="run_1")
    unresolved_ids = {a["action_id"] for a in resumed["uncertain_actions"]}
    assert rows["b.do"]["action_id"] in unresolved_ids
    assert {a["action_id"] for a in payload["actions"] if a["outcome_unresolved"]} == unresolved_ids


@pytest.mark.asyncio
async def test_complete_action_cannot_clear_an_unknown_outcome(
    server_ctx: tuple[Any, Any],
) -> None:
    """The evidence gate must not be bypassable by the ungated tool (issue #366).

    `continuum_complete_action` is the tool an agent is told to call routinely,
    it is on the same mutation allowlist as everything else, and it accepts the
    key the agent already holds from `continuum_intercept_action`. So it was both
    the easiest door and the one with no evidence requirement, while the gated
    route through `continuum_reconcile_action` was the harder one. An agent trying
    to get unstuck reaches for the easy door.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    server, _ = server_ctx
    await seed_run(server)
    claimed = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="card.charge",
        arguments={"amount": 4200, "invoice": "INV-9001"},
        key="charge:INV-9001",
    )
    await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claimed["action_key"],
        error="gateway timeout after the charge request was sent",
        certain=False,
    )

    with pytest.raises(ToolError, match="nothing has verified"):
        await server.call_tool(
            "continuum_complete_action",
            {"run_id": "run_1", "action_key": claimed["action_key"]},
            context=_ctx(TEST_CLIENT),
        )

    # The blocker survives, so recovery still refuses to call the run safe.
    listed = await call(server, "continuum_list_actions", run_id="run_1")
    assert listed["unresolved"] == 1
    assert listed["actions"][0]["status"] == "unknown"
    resumed = await call(server, "continuum_resume", run_id="run_1")
    assert resumed["safe"] is False
    assert resumed["next_allowed_action"].startswith("reconcile_action:")


@pytest.mark.asyncio
async def test_reconciling_an_unknown_outcome_records_that_it_was_a_correction(
    server_ctx: tuple[Any, Any],
) -> None:
    """The supported route keeps the decision visible in the log (issue #366).

    `complete` recorded ACTION_RECORDED, indistinguishable from a first-time
    success, so an auditor could not tell that an uncertain effect had been
    resolved by assertion. `reconcile` records ACTION_RECONCILED with the note.
    """
    server, ctx = server_ctx
    await seed_run(server)
    claimed = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="card.charge",
        arguments={"invoice": "INV-9001"},
        key="charge:INV-9001",
    )
    await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claimed["action_key"],
        error="gateway timeout",
        certain=False,
    )
    settled = await call(
        server,
        "continuum_reconcile_action",
        run_id="run_1",
        action_key=claimed["action_key"],
        occurred=True,
        external_id="txn-1",
        note="found the charge in the gateway ledger",
    )

    assert settled["status"] == "completed"
    assert settled["side_effect_uncertain"] is False
    assert (await call(server, "continuum_list_actions", run_id="run_1"))["unresolved"] == 0
    assert EventType.ACTION_RECONCILED in [e.type for e in ctx.storage.read_events("run_1")]


@pytest.mark.asyncio
async def test_the_identifier_resume_advertises_is_accepted_by_reconcile(
    server_ctx: tuple[Any, Any],
) -> None:
    """Recovery guidance must be executable exactly as written (issue #367).

    ``next_allowed_action``, the contract's ``required_actions``, ``human_steps``
    and the rendered report all name an ``action_id``, and ``human_steps`` spells
    out a ``continuum_reconcile_action(action_key=<action_id>)`` call. The tool
    keyed only on the idempotency key, so following the instruction verbatim
    failed and no MCP surface exposed the value that would have worked.
    """
    server, _ = server_ctx
    await seed_run(server)
    await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="registry.publish",
        arguments={"target": "registry://audit"},
    )
    resumed = await call(server, "continuum_resume", run_id="run_1")
    advertised = resumed["next_allowed_action"].removeprefix("reconcile_action:")
    assert advertised in resumed["human_steps"][0]

    settled = await call(
        server,
        "continuum_reconcile_action",
        run_id="run_1",
        action_key=advertised,
        occurred=False,
        note="checked the registry, nothing landed",
    )
    assert settled["status"] == "failed"
    assert (await call(server, "continuum_list_actions", run_id="run_1"))["unresolved"] == 0


@pytest.mark.asyncio
async def test_the_key_needed_to_reconcile_is_reported_not_truncated(
    server_ctx: tuple[Any, Any],
) -> None:
    """Every surface naming an uncertain action must carry a usable key (#367).

    The ``UnknownSideEffect`` response omitted ``action_key`` entirely, leaving a
    12-character truncated prefix inside the free-text ``reason`` as the only
    trace. ``list_actions`` and ``uncertain_actions`` reported ``action_id``
    alone, and ``arguments_hash`` from the CLI looks like a key but is a
    different hash.
    """
    server, _ = server_ctx
    await seed_run(server)
    claimed = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="card.charge",
        arguments={"invoice": "INV-9001"},
        key="charge:INV-9001",
    )
    escalated = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="card.charge",
        arguments={"invoice": "INV-9001"},
        key="charge:INV-9001",
    )
    assert escalated["proceed"] is False
    assert escalated["status"] == "unknown"
    assert escalated["action_key"] == claimed["action_key"]

    listed = await call(server, "continuum_list_actions", run_id="run_1")
    assert listed["actions"][0]["action_key"] == claimed["action_key"]
    resumed = await call(server, "continuum_resume", run_id="run_1")
    assert resumed["uncertain_actions"][0]["action_key"] == claimed["action_key"]

    # Usable as reported, from any of the three.
    settled = await call(
        server,
        "continuum_reconcile_action",
        run_id="run_1",
        action_key=escalated["action_key"],
        occurred=True,
        external_id="txn-1",
    )
    assert settled["status"] == "completed"


@pytest.mark.asyncio
async def test_an_unmatched_identifier_says_which_spaces_were_tried(
    server_ctx: tuple[Any, Any],
) -> None:
    """`no action recorded for key <prefix>...` told the caller nothing (#367)."""
    from mcp.server.mcpserver.exceptions import ToolError

    server, _ = server_ctx
    await seed_run(server)
    with pytest.raises(ToolError, match="idempotency key or an action_id"):
        await server.call_tool(
            "continuum_reconcile_action",
            {"run_id": "run_1", "action_key": "not-an-identifier", "occurred": False},
            context=_ctx(TEST_CLIENT),
        )


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
    sidecars provoke; the server's open path must then clear the sidecars that
    are in the way and retry, coming up with the database's prior contents still
    readable. Recovery is staged, so reaching a blocking ``-wal`` takes three
    opens: the initial failure, a retry after the reconstructable ``-shm`` is
    discarded, and a final retry once the log is parked aside. The
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
        # The wal was moved aside between the two opens: the mid-open callback saw
        # it present on the failing attempt and absent on the succeeding one.
        cleared_between: list[str] = []
        real_remove = os.remove
        real_replace = os.replace

        def _record_remove(target: str) -> None:
            cleared_between.append(str(target))
            real_remove(target)

        def _record_replace(src: str, dst: str) -> None:
            cleared_between.append(str(src))
            real_replace(src, dst)

        os.remove = _record_remove  # type: ignore[assignment]
        os.replace = _record_replace  # type: ignore[assignment]
        try:
            storage = _open_server_storage(path)
        finally:
            os.remove = real_remove  # type: ignore[assignment]
            os.replace = real_replace  # type: ignore[assignment]
        try:
            # Staged recovery: failed open, -shm discarded, retry, -wal parked
            # aside, final retry.
            assert calls["n"] == 3
            assert cleared_between  # at least one stale sidecar was cleared
            # And the pre-crash work survived the sidecar recovery.
            assert storage.get_run("survivor").goal == "pre-crash work"
            assert [e.type for e in storage.read_events("survivor")] == [EventType.RUN_STARTED]
        finally:
            storage.close()

        # The full server object routes through the same recovery.
        _orphan_wal_sidecars(path)
        calls["n"] = 0
        ctx = ContinuumMCP(path)
        try:
            assert calls["n"] == 3
            assert ctx.storage.get_run("survivor").goal == "pre-crash work"
        finally:
            ctx.close()
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]


def test_server_startup_prefers_discarding_only_the_shm_sidecar(tmp_path: Any) -> None:
    """A stale ``-shm`` must be recovered without touching the write-ahead log.

    The ``-shm`` file is a shared-memory index and is rebuildable, so discarding
    it is free. The ``-wal`` is not: it can hold committed transactions absent
    from the main database. When clearing the cheap sidecar is enough to reopen,
    the log must be left exactly where it is so SQLite replays it.
    """
    path = str(tmp_path / "agent.db")
    seed = SQLiteStorage(path)
    seed.create_run(Run(run_id="survivor", goal="pre-crash work"))
    seed.close()

    _orphan_wal_sidecars(path)
    wal_bytes = b"committed-but-not-checkpointed"
    with open(f"{path}-wal", "wb") as wal:
        wal.write(wal_bytes)

    real_init = SQLiteStorage.__init__
    calls = {"n": 0}

    def _fail_while_shm_present(self: Any, database: str, *args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if os.path.exists(f"{database}-shm"):
            raise sqlite3.OperationalError("disk I/O error")
        real_init(self, database, *args, **kwargs)

    SQLiteStorage.__init__ = _fail_while_shm_present  # type: ignore[method-assign]
    try:
        storage = _open_server_storage(path)
        try:
            assert calls["n"] == 2  # failed open, then a retry after -shm went
            # The log is untouched: same path, same bytes, nothing quarantined.
            assert os.path.exists(f"{path}-wal")
            with open(f"{path}-wal", "rb") as wal:
                assert wal.read() == wal_bytes
            assert not os.path.exists(f"{path}-wal.orphaned")
        finally:
            storage.close()
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]


def test_server_startup_never_deletes_the_write_ahead_log(tmp_path: Any) -> None:
    """A blocking ``-wal`` is quarantined, not destroyed.

    Deleting it would turn committed transactions into silent loss, and an
    emptied database still verifies as an intact chain — the failure would look
    like success. The bytes must survive somewhere recoverable.
    """
    path = str(tmp_path / "agent.db")
    SQLiteStorage(path).close()

    _orphan_wal_sidecars(path)
    wal_bytes = b"committed-but-not-checkpointed"
    with open(f"{path}-wal", "wb") as wal:
        wal.write(wal_bytes)

    real_init = SQLiteStorage.__init__

    def _fail_while_wal_present(self: Any, database: str, *args: Any, **kwargs: Any) -> None:
        if os.path.exists(f"{database}-wal"):
            raise sqlite3.OperationalError("disk I/O error")
        real_init(self, database, *args, **kwargs)

    SQLiteStorage.__init__ = _fail_while_wal_present  # type: ignore[method-assign]
    try:
        storage = _open_server_storage(path)
        try:
            quarantined = f"{path}-wal.orphaned"
            assert os.path.exists(quarantined), "the log was destroyed, not preserved"
            with open(quarantined, "rb") as wal:
                assert wal.read() == wal_bytes
        finally:
            storage.close()
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]


def test_quarantining_a_log_does_not_overwrite_an_earlier_one(tmp_path: Any) -> None:
    """A second crash must not erase the first crash's unrecovered log."""
    path = str(tmp_path / "agent.db")
    SQLiteStorage(path).close()

    with open(f"{path}-wal.orphaned", "wb") as previous:
        previous.write(b"from-the-first-crash")

    _orphan_wal_sidecars(path)
    with open(f"{path}-wal", "wb") as wal:
        wal.write(b"from-the-second-crash")

    real_init = SQLiteStorage.__init__

    def _fail_while_wal_present(self: Any, database: str, *args: Any, **kwargs: Any) -> None:
        if os.path.exists(f"{database}-wal"):
            raise sqlite3.OperationalError("disk I/O error")
        real_init(self, database, *args, **kwargs)

    SQLiteStorage.__init__ = _fail_while_wal_present  # type: ignore[method-assign]
    try:
        storage = _open_server_storage(path)
        try:
            with open(f"{path}-wal.orphaned", "rb") as first:
                assert first.read() == b"from-the-first-crash"
            with open(f"{path}-wal.orphaned.1", "rb") as second:
                assert second.read() == b"from-the-second-crash"
        finally:
            storage.close()
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]


def test_a_log_is_restored_when_quarantining_it_does_not_help(tmp_path: Any) -> None:
    """If the retry still fails, the filesystem is left as it was found.

    Parking the log bought nothing, so leaving it under a name nothing looks for
    would only make the operator's recovery harder.
    """
    path = str(tmp_path / "agent.db")
    SQLiteStorage(path).close()

    _orphan_wal_sidecars(path)
    wal_bytes = b"committed-but-not-checkpointed"
    with open(f"{path}-wal", "wb") as wal:
        wal.write(wal_bytes)

    real_init = SQLiteStorage.__init__

    def _always_disk_error(self: Any, database: str, *args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    SQLiteStorage.__init__ = _always_disk_error  # type: ignore[method-assign]
    try:
        with pytest.raises(sqlite3.OperationalError):
            _open_server_storage(path)
    finally:
        SQLiteStorage.__init__ = real_init  # type: ignore[method-assign]

    with open(f"{path}-wal", "rb") as wal:
        assert wal.read() == wal_bytes
    assert not os.path.exists(f"{path}-wal.orphaned")


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


# --- cold start -------------------------------------------------------------- #


def test_a_rejected_configuration_leaves_no_open_handle(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #87: a failed build must not strand the store.

    ``load_policy`` and ``load_auth`` reject malformed input with ValueError, so
    a build that opens storage before resolving them has no owner left to close
    the handle. That is the leak of #81 in the cold-start path, invisible on
    POSIX and fatal on Windows, where the stranded file cannot be removed.

    This asserts two things. No handle is left open, which any correct fix
    satisfies, including closing it on the way out. And no database file is
    created, which holds specifically because configuration is resolved before
    anything is acquired: a server that never started has no business leaving a
    database behind in the operator's working directory.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CLIENT_TOKENS_ENV_VAR, "missing-the-colon")

    live: list[Any] = []

    class _TrackedStorage(SQLiteStorage):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            live.append(self)

        def close(self) -> None:
            super().close()
            if self in live:
                live.remove(self)

    monkeypatch.setattr("continuum.mcp.server.SQLiteStorage", _TrackedStorage)

    with pytest.raises(ValueError):
        build_server("agent.db")

    assert live == [], "the rejected build left a storage handle open"
    assert not os.path.exists("agent.db"), "a server that never started created a database"


def test_main_reports_an_unopenable_database_instead_of_a_traceback(
    tmp_path: Any, capsys: Any
) -> None:
    """Regression for #87: a bad --db must be diagnosable by the operator.

    Over stdio an unhandled exception is written into the protocol pipe, so the
    client can only report that the server never became ready. The path that
    failed has to reach stderr instead, as it already does for the CLI.
    """
    missing = tmp_path / "no-such-directory" / "agent.db"

    assert main(["--db", str(missing)]) == 1

    err = capsys.readouterr().err
    assert "cannot open storage" in err
    assert str(missing) in err
    assert "Traceback" not in err


def test_the_reported_path_is_not_backslash_escaped(tmp_path: Any, capsys: Any) -> None:
    """Regression for #94, the same property as the CLI's identical test.

    ``!r`` doubled every backslash, so on Windows the assertion above (``str(
    missing) in err``) was red on a clean checkout of main -- the escaping broke
    the very guarantee #87 was fixed to provide. A backslash in the filename is
    legal on POSIX, so this reproduces on the ubuntu-only CI too.
    """
    missing = tmp_path / "no-such-dir" / "back\\slash.db"

    assert main(["--db", str(missing)]) == 1

    err = capsys.readouterr().err
    assert str(missing) in err, "the path reported is not the path that was passed"
    assert "\\\\" not in err, "repr()-style escaping is back"


def test_main_reports_a_malformed_client_token_list(
    tmp_path: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #87's cold start: configuration the loaders reject.

    The message must name the offending variable, since the operator's only
    other signal is that the server is not ready.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(CLIENT_TOKENS_ENV_VAR, "missing-the-colon")

    assert main(["--db", "agent.db"]) == 1

    err = capsys.readouterr().err
    assert CLIENT_TOKENS_ENV_VAR in err
    assert "Traceback" not in err


# --- missing optional extra (a real subprocess, as the client launches it) --- #

_WITHOUT_MCP_SDK = """
import sys


class _BlockMCP:
    def find_spec(self, name, path=None, target=None):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None


sys.meta_path.insert(0, _BlockMCP())

# Must survive import: the SDK is imported inside build_server precisely so
# that this line cannot be the thing that fails.
from continuum.mcp.server import main

raise SystemExit(main(["--db", "agent.db"]))
"""


def test_main_reports_a_missing_mcp_extra_instead_of_a_traceback(tmp_path: Any) -> None:
    """Regression for #87: `pip install continuum` ships the script, not the SDK.

    Run in a subprocess because blocking an already-imported package in-process
    would corrupt the import state of every later test.

    The load-bearing assertions are the stderr ones. A crash and a reported
    error both exit 1, so the exit code alone does not distinguish them -- what
    the operator needs is a named cause and a command to run, neither of which
    survives an unhandled exception. stdout is asserted empty separately: over
    stdio the client parses that stream as protocol frames, so diagnostics must
    never be printed there, however tempting it is.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _WITHOUT_MCP_SDK],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == "", "the protocol stream must stay clean"
    assert "Traceback" not in proc.stderr
    assert "continuum[mcp]" in proc.stderr, "the operator needs the fix, not just the fault"
    assert not (tmp_path / "agent.db").exists(), "a server that never started created a database"


def test_a_missing_unrelated_module_keeps_its_traceback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extra is blamed only when the extra is what is missing.

    A broken install of something else must not be reported as "install
    continuum[mcp]", which would send the operator after the wrong fix.
    """
    monkeypatch.chdir(tmp_path)

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")

    monkeypatch.setattr("continuum.mcp.server.build_server", _explode)

    with pytest.raises(ModuleNotFoundError, match="pydantic"):
        main(["--db", "agent.db"])


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


# --- the retry budget must not defeat idempotency (issue #309) ---------------- #


@pytest.mark.asyncio
async def test_many_successful_actions_of_one_type_are_not_blocked(
    server_ctx: tuple[Any, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct successes are not retries, so the default budget of 3 must not
    refuse the fourth invoice a run legitimately sends."""
    monkeypatch.chdir(tmp_path)  # no .continuum/budgets.json here: defaults apply
    server, _ = server_ctx
    await seed_run(server)

    for n in range(5):
        claim = await call(
            server,
            "continuum_intercept_action",
            run_id="run_1",
            action_type="send_invoice",
            key=f"invoice:{n}",
        )
        assert claim["proceed"] is True, f"claim {n} was refused: {claim}"
        await call(
            server,
            "continuum_complete_action",
            run_id="run_1",
            action_key=claim["action_key"],
            external_id=f"ext-{n}",
        )


@pytest.mark.asyncio
async def test_a_completed_action_still_deduplicates_at_budget(
    server_ctx: tuple[Any, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that let an exhausted budget cause a duplicate effect.

    Re-claiming an action that already completed is a lookup, not an attempt. If
    the budget gate runs first it raises instead of answering, and an agent that
    gets an error where it expected "already done" has every reason to perform
    the side effect again out of band.
    """
    monkeypatch.chdir(tmp_path)
    server, _ = server_ctx
    await seed_run(server)

    done = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="charge",
        key="charge:once",
    )
    await call(
        server,
        "continuum_complete_action",
        run_id="run_1",
        action_key=done["action_key"],
        external_id="receipt-1",
        result={"cents": 500},
    )

    # Exhaust the default budget of 3 with genuinely unsettled attempts.
    for n in range(3):
        await call(
            server,
            "continuum_intercept_action",
            run_id="run_1",
            action_type="charge",
            key=f"charge:stuck-{n}",
        )

    # A fresh identity is now correctly refused.
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="retry budget exhausted"):
        await server.call_tool(
            "continuum_intercept_action",
            {"run_id": "run_1", "action_type": "charge", "key": "charge:new"},
            context=_ctx(TEST_CLIENT),
        )

    # The completed one still answers, which is the whole point of the ledger.
    again = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="charge",
        key="charge:once",
    )
    assert again["proceed"] is False
    assert again["status"] == ActionStatus.COMPLETED.value
    assert again["external_id"] == "receipt-1"
    assert again["previous_result"] == {"cents": 500}


@pytest.mark.asyncio
async def test_an_uncertain_action_still_refuses_at_budget(
    server_ctx: tuple[Any, Any],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted budget must not mask the reconciliation path either."""
    monkeypatch.chdir(tmp_path)
    server, _ = server_ctx
    await seed_run(server)

    claim = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="charge",
        key="charge:maybe",
    )
    await call(
        server,
        "continuum_fail_action",
        run_id="run_1",
        action_key=claim["action_key"],
        error="timeout after send",
        certain=False,
    )
    # The unresolved charge:maybe is itself one unsettled attempt, so two more
    # reach the default budget of 3.
    for n in range(2):
        await call(
            server,
            "continuum_intercept_action",
            run_id="run_1",
            action_type="charge",
            key=f"charge:stuck-{n}",
        )

    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="retry budget exhausted"):
        await server.call_tool(
            "continuum_intercept_action",
            {"run_id": "run_1", "action_type": "charge", "key": "charge:new"},
            context=_ctx(TEST_CLIENT),
        )

    again = await call(
        server,
        "continuum_intercept_action",
        run_id="run_1",
        action_type="charge",
        key="charge:maybe",
    )
    assert again["proceed"] is False
    assert again["status"] == ActionStatus.UNKNOWN.value
    assert "reconcile" in again["guidance"].lower()
