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
    AUTH_ENV_VAR,
    CLIENT_TOKENS_ENV_VAR,
    POLICY_ENV_VAR,
    POLICY_ENV_VAR_ALIAS,
    POLICY_FILENAME,
    AuthorizationPolicy,
    AuthPolicy,
    NotAuthenticated,
    NotAuthorized,
    UnknownCaller,
    caller_name,
    load_auth,
    load_policy,
    token_from,
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


# --- authentication (issue #1) ---------------------------------------------- #


def test_a_disabled_auth_policy_is_a_no_op() -> None:
    auth = AuthPolicy()
    assert auth.disabled
    # Must not raise, and must not require anything.
    auth.verify(STRANGER, None)
    auth.verify(None, "anything")


def test_a_configured_secret_is_required() -> None:
    auth = AuthPolicy("secret")
    assert not auth.disabled
    # Correct secret passes.
    auth.verify(ALLOWED, "secret")
    # Missing, empty, or wrong secret refuses.
    with pytest.raises(NotAuthenticated, match="shared secret"):
        auth.verify(ALLOWED, None)
    with pytest.raises(NotAuthenticated, match="shared secret"):
        auth.verify(ALLOWED, "")
    with pytest.raises(NotAuthenticated, match="shared secret"):
        auth.verify(ALLOWED, "wrong")


def test_auth_fails_closed_when_required_but_unset() -> None:
    """The past mistake (PR #3) was a guard that authorized on ValueError.

    An empty expected secret must refuse, never open the door.
    """
    auth = AuthPolicy("")
    # "" is not a usable secret, so verification refuses rather than passing.
    with pytest.raises(NotAuthenticated):
        auth.verify(ALLOWED, "")


def test_per_client_tokens_map_a_secret_to_a_name() -> None:
    auth = AuthPolicy(tokens={ALLOWED: "a-secret"})
    assert not auth.disabled
    auth.verify(ALLOWED, "a-secret")
    # An unregistered caller is refused even with a plausible token.
    with pytest.raises(NotAuthenticated, match="not registered"):
        auth.verify(STRANGER, "a-secret")
    # A registered caller with the wrong token is refused.
    with pytest.raises(NotAuthenticated, match="shared secret"):
        auth.verify(ALLOWED, "nope")


def test_load_auth_reads_the_env_var() -> None:
    auth = load_auth(env={AUTH_ENV_VAR: "s3cr3t"})
    assert not auth.disabled
    assert auth.source == AUTH_ENV_VAR
    auth.verify(ALLOWED, "s3cr3t")
    with pytest.raises(NotAuthenticated):
        auth.verify(ALLOWED, "nope")


def test_load_auth_is_disabled_without_a_secret() -> None:
    auth = load_auth(env={})
    assert auth.disabled
    # Argument wins over env and over the disabled default.
    assert not load_auth("x", env={}).disabled


def test_load_auth_reads_per_client_tokens_env() -> None:
    auth = load_auth(env={CLIENT_TOKENS_ENV_VAR: "trusted-agent:tok-a,kilo:tok-k"})
    assert not auth.disabled
    assert auth.source == CLIENT_TOKENS_ENV_VAR
    # Each caller's secret is bound to its own name.
    auth.verify("trusted-agent", "tok-a")
    auth.verify("kilo", "tok-k")
    with pytest.raises(NotAuthenticated):
        auth.verify("trusted-agent", "tok-k")  # another client's token
    with pytest.raises(NotAuthenticated):
        auth.verify("stranger", "tok-a")  # unregistered caller


def test_load_auth_rejects_malformed_client_tokens() -> None:
    with pytest.raises(ValueError, match="name:secret"):
        load_auth(env={CLIENT_TOKENS_ENV_VAR: "no-colon-here"})


def test_load_auth_per_client_tokens_win_over_shared_secret_env() -> None:
    auth = load_auth(
        env={
            CLIENT_TOKENS_ENV_VAR: "trusted-agent:tok-a",
            AUTH_ENV_VAR: "shared",
        }
    )
    assert auth.source == CLIENT_TOKENS_ENV_VAR
    auth.verify("trusted-agent", "tok-a")
    with pytest.raises(NotAuthenticated):
        auth.verify("trusted-agent", "shared")


def test_token_from_reads_the_handshake_meta() -> None:
    assert token_from(fake_context(ALLOWED, auth_token="tok")) == "tok"
    assert token_from(fake_context(ALLOWED)) is None
    assert token_from(None) is None


# --- authentication through the server -------------------------------------- #


@pytest.fixture
def authed_server(store: SQLiteStorage) -> Any:
    """A server that requires the shared secret and allows one caller."""
    srv, _ = build_server(
        storage=store,
        policy=AuthorizationPolicy([ALLOWED]),
        auth=AuthPolicy("secret"),
    )
    return srv


@pytest.mark.asyncio
async def test_a_mutating_call_needs_the_secret(authed_server: Any) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    # No token at all: refused, and nothing is written.
    with pytest.raises(ToolError, match="shared secret"):
        await authed_server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(ALLOWED),
        )


@pytest.mark.asyncio
async def test_the_wrong_secret_is_refused(authed_server: Any) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="shared secret"):
        await authed_server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(ALLOWED, auth_token="wrong"),
        )


@pytest.mark.asyncio
async def test_the_right_secret_and_name_succeeds(authed_server: Any, store: SQLiteStorage) -> None:
    result = await authed_server.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 3, "total": 10, "goal": "g"},
        context=fake_context(ALLOWED, auth_token="secret"),
    )
    assert json.loads(result.content[0].text)["completed"] == 3
    assert store.get_run("run_1").goal == "g"


@pytest.mark.asyncio
async def test_a_stranger_with_the_secret_is_still_unauthorized(
    authed_server: Any,
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    # Possessing the secret is necessary but not sufficient: the name must
    # also be on the allowlist. Auth passes, authorization still fails.
    with pytest.raises(ToolError, match="not permitted"):
        await authed_server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(STRANGER, auth_token="secret"),
        )


@pytest.mark.asyncio
async def test_read_only_tools_do_not_require_the_secret(
    authed_server: Any, store: SQLiteStorage
) -> None:
    """Authentication gates mutating tools only, matching authorization."""
    await authed_server.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 1, "total": 10, "goal": "g"},
        context=fake_context(ALLOWED, auth_token="secret"),
    )
    result = await authed_server.call_tool(
        "continuum_resume", {"run_id": "run_1"}, context=fake_context(STRANGER)
    )
    assert not result.is_error


@pytest.mark.asyncio
async def test_load_auth_wires_per_client_tokens_into_the_server(
    store: SQLiteStorage,
) -> None:
    """The env-driven per-client policy must reach the running server (issue #7)."""
    from mcp.server.mcpserver.exceptions import ToolError

    srv, _ = build_server(
        storage=store,
        policy=AuthorizationPolicy([ALLOWED, "kilo"]),
        auth=load_auth(env={CLIENT_TOKENS_ENV_VAR: f"{ALLOWED}:tok-a,kilo:tok-k"}),
    )
    # The right caller with its own token succeeds.
    result = await srv.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 3, "total": 10, "goal": "g"},
        context=fake_context(ALLOWED, auth_token="tok-a"),
    )
    assert json.loads(result.content[0].text)["completed"] == 3
    # Replaying another client's token against this name is refused.
    with pytest.raises(ToolError, match="shared secret|not registered"):
        await srv.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(ALLOWED, auth_token="tok-k"),
        )


@pytest.fixture
def per_client_server(store: SQLiteStorage) -> Any:
    """A server that issues a distinct secret to each allowed caller (issue #7)."""
    srv, _ = build_server(
        storage=store,
        policy=AuthorizationPolicy([ALLOWED, "kilo"]),
        auth=AuthPolicy(tokens={ALLOWED: "tok-a", "kilo": "tok-k"}),
    )
    return srv


@pytest.mark.asyncio
async def test_per_client_secret_is_bound_to_its_name(per_client_server: Any) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    # Correct caller and its own token: allowed.
    result = await per_client_server.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 3, "total": 10, "goal": "g"},
        context=fake_context(ALLOWED, auth_token="tok-a"),
    )
    assert json.loads(result.content[0].text)["completed"] == 3
    # A different client's token replayed under this name is refused.
    with pytest.raises(ToolError, match="shared secret|not registered"):
        await per_client_server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(ALLOWED, auth_token="tok-k"),
        )
    # The other client succeeds with its own token.
    result = await per_client_server.call_tool(
        "continuum_record_progress",
        {"run_id": "run_1", "completed": 5, "total": 10, "goal": "g"},
        context=fake_context("kilo", auth_token="tok-k"),
    )
    assert json.loads(result.content[0].text)["completed"] == 5


@pytest.mark.asyncio
async def test_an_unregistered_caller_is_refused_even_with_a_token(
    per_client_server: Any,
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="shared secret|not registered"):
        await per_client_server.call_tool(
            "continuum_record_progress",
            {"run_id": "run_1", "completed": 1, "goal": "g"},
            context=fake_context(STRANGER, auth_token="tok-a"),
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
