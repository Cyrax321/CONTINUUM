"""Trust and authentication tests for the CONTINUUM sidecar.

Tests cover:
- Separate confirm credential (an agent holding a normal token cannot call confirm)
- Per-client tokens
- Bearer tokens in Authorization header
- Constant-time comparison using hmac.compare_digest
- Configurable bind address
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.serve import (
    NotAuthorized,
    SidecarAuth,
    SidecarServer,
)
from continuum.serve.server import SidecarHTTP
from continuum.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Unit tests for SidecarAuth
# ---------------------------------------------------------------------------


def test_auth_single_token_from_expected() -> None:
    auth = SidecarAuth("client-key")
    assert not auth.disabled
    assert not auth.client_auth_disabled
    assert auth.expected == "client-key"
    assert "client-key" in auth.client_tokens

    # Verify matching token succeeds
    auth.verify("client-key")

    # Verify wrong or missing token fails
    with pytest.raises(NotAuthorized):
        auth.verify("wrong-key")
    with pytest.raises(NotAuthorized):
        auth.verify(None)


def test_auth_per_client_tokens() -> None:
    auth = SidecarAuth(client_tokens=["agent-alpha", "agent-beta", "agent-gamma"])
    assert len(auth.client_tokens) == 3
    assert not auth.client_auth_disabled

    auth.verify("agent-alpha")
    auth.verify("agent-beta")
    auth.verify("agent-gamma")

    with pytest.raises(NotAuthorized):
        auth.verify("agent-unknown")


def test_auth_per_client_tokens_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUUM_SERVE_CLIENT_TOKENS", "agent-1, agent-2 , agent-3")
    monkeypatch.setenv("CONTINUUM_SERVE_CONFIRM_TOKEN", "confirm-super")
    auth = SidecarAuth.from_environ()

    assert auth.client_tokens == {"agent-1", "agent-2", "agent-3"}
    assert auth.confirm_token == "confirm-super"

    auth.verify("agent-1")
    auth.verify("agent-2")
    auth.verify("agent-3")
    with pytest.raises(NotAuthorized):
        auth.verify("agent-4")

    # Confirm token check
    auth.verify_confirm("confirm-super")


def test_agent_holding_normal_token_cannot_call_confirm_when_confirm_token_configured() -> None:
    """An agent holding a normal client token cannot call confirm."""
    auth = SidecarAuth(
        client_tokens=["agent-key"],
        confirm_token="human-confirm-key",
    )

    # Client token works for normal verify
    auth.verify("agent-key")

    # Client token is refused for confirm
    with pytest.raises(NotAuthorized, match="cannot call confirm"):
        auth.verify_confirm("agent-key")

    # Dedicated confirm credential succeeds
    auth.verify_confirm("human-confirm-key")


def test_agent_holding_normal_token_cannot_call_confirm_when_confirm_token_unconfigured() -> None:
    """If client auth is enabled but no confirm token is set, confirm fails closed."""
    auth = SidecarAuth(client_tokens=["agent-key"], confirm_token=None)

    # Client token works for normal verify
    auth.verify("agent-key")

    # Confirm fails closed: normal client token still cannot call confirm
    with pytest.raises(NotAuthorized, match="dedicated confirm credential"):
        auth.verify_confirm("agent-key")
    with pytest.raises(NotAuthorized, match="dedicated confirm credential"):
        auth.verify_confirm(None)


def test_constant_time_compare_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token comparison must use hmac.compare_digest for constant-time evaluation."""
    import hmac

    called = []
    real_compare = hmac.compare_digest

    def tracking_compare(a: Any, b: Any) -> bool:
        called.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", tracking_compare)

    auth = SidecarAuth(expected="secret-123", confirm_token="confirm-456")
    auth.verify("secret-123")
    assert len(called) >= 1
    assert any(b"secret-123" in arg for pair in called for arg in pair)

    called.clear()
    auth.verify_confirm("confirm-456")
    assert len(called) >= 1
    assert any(b"confirm-456" in arg for pair in called for arg in pair)


# ---------------------------------------------------------------------------
# SidecarServer dispatch tests
# ---------------------------------------------------------------------------


def test_server_dispatch_confirm_requires_confirm_credential(tmp_path: Any) -> None:
    db = str(tmp_path / "dispatch_test.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run-1", goal="deploy app"))
    storage.append_event(
        "run-1",
        EventType.RUN_STARTED,
        {"goal": "deploy app"},
        source=Origin.EXTERNAL_AGENT,
    )

    auth = SidecarAuth(
        client_tokens=["agent-token-1"],
        confirm_token="supervisor-confirm-token",
    )
    srv = SidecarServer(storage=storage, auth=auth)

    # Normal client token works for record_progress
    srv.dispatch(
        "record_progress",
        {"run_id": "run-1", "completed": 1, "total": 5},
        token="agent-token-1",
    )

    # Normal client token is refused on confirm
    with pytest.raises(NotAuthorized):
        srv.dispatch("confirm", {"run_id": "run-1"}, token="agent-token-1")

    with pytest.raises(NotAuthorized):
        srv.dispatch(
            "confirm",
            {"run_id": "run-1", "auth_token": "agent-token-1"},
        )

    # Separate confirm token succeeds
    res = srv.dispatch(
        "confirm",
        {"run_id": "run-1"},
        token="supervisor-confirm-token",
    )
    assert res["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# HTTP transport tests
# ---------------------------------------------------------------------------


_PORT_COUNTER = {"port": 9500}


def _next_port() -> int:
    _PORT_COUNTER["port"] += 1
    return _PORT_COUNTER["port"]


def _http_request(
    addr: str,
    method: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    url = f"http://{addr}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw.decode("utf-8")) if raw else {}


def test_http_authorization_bearer_header(tmp_path: Any) -> None:
    """Client presents Authorization: Bearer <token> in HTTP header without token in body."""
    db = str(tmp_path / "http_bearer.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run-http", goal="task"))
    storage.append_event(
        "run-http",
        EventType.RUN_STARTED,
        {"goal": "task"},
        source=Origin.EXTERNAL_AGENT,
    )

    auth = SidecarAuth(expected="agent-token-xyz")
    sidecar = SidecarServer(storage=storage, auth=auth)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        addr = f"127.0.0.1:{http.port}"

        # Missing token: 403
        status, resp = _http_request(
            addr,
            "record_progress",
            {"run_id": "run-http", "completed": 1, "total": 10},
        )
        assert status == 403

        # Authorization: Bearer header present: 200 OK
        status, resp = _http_request(
            addr,
            "record_progress",
            {"run_id": "run-http", "completed": 2, "total": 10},
            headers={"Authorization": "Bearer agent-token-xyz"},
        )
        assert status == 200
        assert resp["completed"] == 2
    finally:
        http.shutdown()


def test_http_agent_holding_normal_token_cannot_call_confirm(tmp_path: Any) -> None:
    """An agent holding a normal token cannot call confirm over HTTP."""
    db = str(tmp_path / "http_confirm.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run-c", goal="task"))
    storage.append_event(
        "run-c",
        EventType.RUN_STARTED,
        {"goal": "task"},
        source=Origin.EXTERNAL_AGENT,
    )

    auth = SidecarAuth(
        client_tokens=["agent-worker-token"],
        confirm_token="supervisor-token-999",
    )
    sidecar = SidecarServer(storage=storage, auth=auth)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        addr = f"127.0.0.1:{http.port}"

        # Agent holding normal token tries to call confirm: 403 Forbidden
        status, resp = _http_request(
            addr,
            "confirm",
            {"run_id": "run-c"},
            headers={"Authorization": "Bearer agent-worker-token"},
        )
        assert status == 403
        assert "cannot call confirm" in resp.get("error", "")

        # Even with normal token in body: 403 Forbidden
        status, resp = _http_request(
            addr,
            "confirm",
            {"run_id": "run-c", "auth_token": "agent-worker-token"},
        )
        assert status == 403

        # Caller presenting confirm token in Authorization header: 200 OK
        status, resp = _http_request(
            addr,
            "confirm",
            {"run_id": "run-c"},
            headers={"Authorization": "Bearer supervisor-token-999"},
        )
        assert status == 200
        assert resp["run_id"] == "run-c"
    finally:
        http.shutdown()


def test_http_per_client_tokens_over_wire(tmp_path: Any) -> None:
    """Multiple per-client tokens all authenticate over HTTP."""
    db = str(tmp_path / "http_multi.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run-m", goal="task"))
    storage.append_event(
        "run-m",
        EventType.RUN_STARTED,
        {"goal": "task"},
        source=Origin.EXTERNAL_AGENT,
    )

    auth = SidecarAuth(client_tokens=["client-A", "client-B"])
    sidecar = SidecarServer(storage=storage, auth=auth)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        addr = f"127.0.0.1:{http.port}"

        # Client A works
        status, resp = _http_request(
            addr,
            "record_progress",
            {"run_id": "run-m", "completed": 1, "total": 10},
            headers={"Authorization": "Bearer client-A"},
        )
        assert status == 200

        # Client B works
        status, resp = _http_request(
            addr,
            "record_progress",
            {"run_id": "run-m", "completed": 2, "total": 10},
            headers={"Authorization": "Bearer client-B"},
        )
        assert status == 200

        # Client C (unauthorized) fails with 403
        status, resp = _http_request(
            addr,
            "record_progress",
            {"run_id": "run-m", "completed": 3, "total": 10},
            headers={"Authorization": "Bearer client-C"},
        )
        assert status == 403
    finally:
        http.shutdown()


def test_http_bind_attribute() -> None:
    sidecar = SidecarServer(database=":memory:")
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    assert http.bind == "127.0.0.1"
    assert http.port == port
    http.shutdown()


def test_cli_serve_bind_option() -> None:
    """CLI argument parser accepts --bind flag for serve command."""
    from continuum.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["serve", "--transport", "http", "--bind", "0.0.0.0", "--port", "9000"]
    )
    assert args.command == "serve"
    assert args.transport == "http"
    assert args.bind == "0.0.0.0"
    assert args.port == 9000
