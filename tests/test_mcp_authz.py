"""Who may change a run through MCP.

The property under test: **an unlisted caller cannot mutate state, and a
rejected call writes nothing.** Read-only tools stay open to everyone, because
asking "is this safe to resume?" should never require permission.

This is authorization by declared identity, not authentication. ``clientInfo``
is asserted by the client at handshake and never verified, so these tests prove
that honestly-named agents are kept apart — not that a hostile one is stopped.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from continuum.mcp.authz import (
    POLICY_ENV_VAR,
    POLICY_ENV_VAR_ALIAS,
    POLICY_FILENAME,
    AuthorizationPolicy,
    NotAuthorized,
    UnknownCaller,
    caller_name,
    load_policy,
)
from continuum.mcp.server import build_server
from continuum.storage import SQLiteStorage
from tests.mcp_helpers import fake_context

ALLOWED = "trusted-agent"
STRANGER = "some-other-agent"

#: Each mutating tool with arguments valid for its own schema. Sending
#: malformed arguments would trip pydantic validation *before* the guard runs,
#: so the test would pass for the wrong reason.
MUTATING_CALLS: dict[str, dict[str, Any]] = {
    "continuum_record_progress": {"run_id": "run_1", "completed": 1},
    "continuum_checkpoint": {"run_id": "run_1"},
    "continuum_intercept_action": {"run_id": "run_1", "action_type": "x.do"},
    "continuum_complete_action": {"run_id": "run_1", "action_key": "k"},
    "continuum_fail_action": {"run_id": "run_1", "action_key": "k", "error": "boom"},
    "continuum_reconcile_action": {
        "run_id": "run_1",
        "action_key": "k",
        "occurred": True,
    },
    "continuum_confirm": {"run_id": "run_1"},
}
MUTATING = list(MUTATING_CALLS)
READ_ONLY = ["continuum_validate", "continuum_resume", "continuum_list_actions"]


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    yield storage
    storage.close()


@pytest.fixture
def server(store: SQLiteStorage) -> Any:
    srv, _ = build_server(storage=store, policy=AuthorizationPolicy([ALLOWED]))
    return srv


async def seed(server: Any) -> None:
    await server.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 1, "total": 10, "goal": "g"},
        context=fake_context(ALLOWED),
    )


# --- the policy object ------------------------------------------------------ #


def test_an_empty_policy_permits_nobody() -> None:
    policy = AuthorizationPolicy()
    assert policy.denies_everything
    assert not policy.permits(ALLOWED)
    assert not policy.permits(None)


def test_a_listed_caller_is_permitted() -> None:
    policy = AuthorizationPolicy([ALLOWED])
    assert policy.permits(ALLOWED)
    assert not policy.permits(STRANGER)


def test_require_raises_for_a_stranger() -> None:
    with pytest.raises(NotAuthorized, match="not permitted"):
        AuthorizationPolicy([ALLOWED]).require(STRANGER, "continuum_checkpoint")


def test_require_distinguishes_an_unidentified_connection() -> None:
    """ "Who are you?" and "you may not" are different problems."""
    with pytest.raises(UnknownCaller, match="did not identify itself"):
        AuthorizationPolicy([ALLOWED]).require(None, "continuum_checkpoint")


def test_the_refusal_explains_how_to_fix_it() -> None:
    with pytest.raises(NotAuthorized) as excinfo:
        AuthorizationPolicy([ALLOWED]).require(STRANGER, "continuum_checkpoint")
    message = str(excinfo.value)
    # Points at the preferred variable name, not merely a working one.
    assert POLICY_ENV_VAR_ALIAS in message
    assert POLICY_FILENAME in message
    assert "Read-only tools remain available" in message
    assert ALLOWED in message  # who *is* permitted


def test_blank_names_are_ignored_not_treated_as_a_grant() -> None:
    policy = AuthorizationPolicy(["", "  ", ALLOWED])
    assert policy.allowed == frozenset({ALLOWED})
    assert not policy.permits("")
    assert not policy.permits("   ")


# --- resolving the policy --------------------------------------------------- #


def test_an_unconfigured_server_denies_every_mutation(tmp_path: Path) -> None:
    policy = load_policy(root=tmp_path, env={})
    assert policy.denies_everything
    assert "deny" in policy.source


def test_the_env_var_grants_access(tmp_path: Path) -> None:
    policy = load_policy(root=tmp_path, env={POLICY_ENV_VAR: "a, b c"})
    assert policy.allowed == frozenset({"a", "b", "c"})
    assert policy.source == POLICY_ENV_VAR


def test_the_alias_env_var_resolves_identically(tmp_path: Path) -> None:
    """CONTINUUM_MCP_MUTATING_CLIENTS is an alias, not a second config source.

    The name is preserved from the closed PR #3, where it read better than
    CONTINUUM_MCP_ALLOW: it says what is being allowed.
    """
    via_alias = load_policy(root=tmp_path, env={POLICY_ENV_VAR_ALIAS: "a, b c"})
    via_primary = load_policy(root=tmp_path, env={POLICY_ENV_VAR: "a, b c"})

    assert via_alias.allowed == via_primary.allowed == frozenset({"a", "b", "c"})
    assert via_alias.source == POLICY_ENV_VAR_ALIAS
    assert via_primary.source == POLICY_ENV_VAR


def test_the_alias_wins_when_both_env_vars_are_set(tmp_path: Path) -> None:
    """Ambiguity has to resolve one way; the more precise name wins.

    Someone who followed PR #3's history reaches for the longer name first,
    and silently preferring the vaguer one would surprise them. `source`
    reports which was used so the choice is never invisible.
    """
    policy = load_policy(
        root=tmp_path,
        env={POLICY_ENV_VAR_ALIAS: ALLOWED, POLICY_ENV_VAR: STRANGER},
    )
    assert policy.permits(ALLOWED)
    assert not policy.permits(STRANGER)
    assert policy.source == POLICY_ENV_VAR_ALIAS


def test_an_empty_alias_falls_through_to_the_primary(tmp_path: Path) -> None:
    """An exported-but-empty variable is not a grant of nothing."""
    policy = load_policy(root=tmp_path, env={POLICY_ENV_VAR_ALIAS: "", POLICY_ENV_VAR: ALLOWED})
    assert policy.permits(ALLOWED)
    assert policy.source == POLICY_ENV_VAR


def test_a_policy_file_grants_access(tmp_path: Path) -> None:
    path = tmp_path / POLICY_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"allow": [ALLOWED]}))
    policy = load_policy(root=tmp_path, env={})
    assert policy.permits(ALLOWED)
    assert str(path) in policy.source


def test_a_policy_file_may_be_a_bare_list(tmp_path: Path) -> None:
    path = tmp_path / POLICY_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([ALLOWED]))
    assert load_policy(root=tmp_path, env={}).permits(ALLOWED)


def test_an_explicit_argument_wins_over_everything(tmp_path: Path) -> None:
    policy = load_policy([STRANGER], root=tmp_path, env={POLICY_ENV_VAR: ALLOWED})
    assert policy.permits(STRANGER)
    assert not policy.permits(ALLOWED)


def test_a_malformed_policy_file_raises_rather_than_guessing(tmp_path: Path) -> None:
    """A file that exists is a statement of intent; ignoring a typo in it
    would either baffle the owner or silently widen access."""
    path = tmp_path / POLICY_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="cannot read MCP policy"):
        load_policy(root=tmp_path, env={})

    path.write_text(json.dumps({"allow": "not-a-list"}))
    with pytest.raises(ValueError, match="must be a list"):
        load_policy(root=tmp_path, env={})


# --- reading the caller's identity ------------------------------------------ #


def test_caller_name_reads_the_handshake() -> None:
    assert caller_name(fake_context(ALLOWED)) == ALLOWED


def test_caller_name_is_none_when_absent() -> None:
    assert caller_name(None) is None
    assert caller_name(fake_context(None)) is None
    assert caller_name(object()) is None


# --- enforcement through the server ----------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", MUTATING)
async def test_a_stranger_cannot_use_any_mutating_tool(server: Any, tool: str) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="not permitted"):
        await server.call_tool(tool, MUTATING_CALLS[tool], context=fake_context(STRANGER))


@pytest.mark.asyncio
async def test_an_unidentified_connection_cannot_mutate(server: Any) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="did not identify itself"):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1},
            context=fake_context(None),
        )


@pytest.mark.asyncio
async def test_an_authorized_caller_succeeds(server: Any, store: SQLiteStorage) -> None:
    result = await server.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 3, "total": 10, "goal": "g"},
        context=fake_context(ALLOWED),
    )
    assert json.loads(result.content[0].text)["completed"] == 3
    assert store.get_run("run_1").goal == "g"


@pytest.mark.asyncio
async def test_a_refused_call_writes_nothing(server: Any, store: SQLiteStorage) -> None:
    """The denial must precede the side effect, not follow it."""
    from mcp.server.mcpserver.exceptions import ToolError

    await seed(server)
    before = store.last_sequence("run_1")

    with pytest.raises(ToolError):
        await server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 999},
            context=fake_context(STRANGER),
        )

    assert store.last_sequence("run_1") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", READ_ONLY)
async def test_read_only_tools_stay_open_to_anyone(
    server: Any, store: SQLiteStorage, tool: str
) -> None:
    """Asking "is this safe to resume?" must never require permission.

    A stranger denied read access could not even discover why its writes are
    failing, and the information disclosed — a run's goal and progress — is
    already readable by anyone holding the database file.
    """
    await seed(server)
    result = await server.call_tool(tool, {"run_id": "run_1"}, context=fake_context(STRANGER))
    assert not result.is_error


@pytest.mark.asyncio
async def test_an_unconfigured_server_is_effectively_read_only(
    store: SQLiteStorage,
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    srv, _ = build_server(storage=store, policy=AuthorizationPolicy())

    with pytest.raises(ToolError, match="not permitted|did not identify"):
        await srv.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(ALLOWED),
        )


@pytest.mark.asyncio
async def test_identity_cannot_be_forged_through_tool_arguments(
    server: Any,
) -> None:
    """The name comes from the handshake, not from anything the caller sends.

    Verified against the live transport too: a client calling itself
    "attacker" while passing clientInfo={"name": "claude-code"} in tool
    arguments is still seen as "attacker".
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="not permitted"):
        await server.call_tool(
            "continuum_record_progress",
            {
                "run_id": "run_1",
                "completed": 1,
                "goal": "g",
                "clientInfo": {"name": ALLOWED},
            },
            context=fake_context(STRANGER),
        )


# --- every tool must be classified ------------------------------------------ #


@pytest.mark.asyncio
async def test_every_tool_is_covered_by_this_suite(server: Any) -> None:
    """A tool added without a policy decision should fail here, not in prod."""
    registered = {t.name for t in await server.list_tools()}
    assert registered == set(MUTATING) | set(READ_ONLY)


@pytest.mark.asyncio
async def test_the_gate_matches_the_declared_annotations(server: Any) -> None:
    """Enforcement is driven by read_only_hint, so the two cannot drift."""
    for tool in await server.list_tools():
        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
        assert read_only == (tool.name in READ_ONLY), tool.name
