"""The enforcing HTTP gateway (seam 4).

A local proxy that refuses unclaimed outbound requests to registered
upstreams and settles claims from real upstream responses. Tested against a
live upstream server on an ephemeral port, through the actual HTTP stack.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.gateway import (
    Decision,
    GatewayConfigError,
    GatewayServer,
    Route,
    load_gateway_config,
    match_route,
    render_key,
)
from continuum.models import ActionStatus, Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "gw.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


ROUTES = [
    {
        "host": "api.example.com",
        "methods": ["POST"],
        "prefix": "/v1/invoices",
        "action_type": "send_invoice",
        "key_template": "invoice:{id}",
    }
]


def config_file(tmp_path: Path) -> str:
    p = tmp_path / "gateway.json"
    p.write_text(json.dumps({"upstreams": ROUTES}))
    return str(p)


@pytest.fixture
def gateway(db: str, tmp_path: Path):
    """A live gateway bound to an ephemeral port."""
    cfg = load_gateway_config(Path(config_file(tmp_path)))
    server = GatewayServer(lambda: SQLiteStorage(db), "run_1", cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.port}"
    server.shutdown()


def post(addr: str, path: str, body: dict[str, object], host: str = "api.example.com"):
    conn = http.client.HTTPConnection(addr, timeout=10)
    conn.request(
        "POST",
        path,
        body=json.dumps(body),
        headers={"Host": host, "Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def recv_until_close(sock: socket.socket) -> bytes:
    chunks = []
    while chunk := sock.recv(4096):
        chunks.append(chunk)
    return b"".join(chunks)


def claim(db: str, key: str) -> str:
    with SQLiteStorage(db) as store:
        outcome = ActionLedger(store, "run_1").claim("send_invoice", {}, key=key)
    return outcome.key


def test_public_gateway_names_are_exported() -> None:
    namespace: dict[str, object] = {}
    exec("from continuum.gateway import *", namespace)

    assert namespace["Route"] is Route
    assert namespace["Decision"] is Decision
    assert namespace["render_key"] is render_key


def test_config_loading_and_validation(tmp_path: Path) -> None:
    missing = load_gateway_config(tmp_path / "nope.json")
    assert missing == []

    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(GatewayConfigError):
        load_gateway_config(bad)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"upstreams": [{"host": "x"}]}))
    with pytest.raises(GatewayConfigError, match="required field"):
        load_gateway_config(incomplete)


def test_unclaimed_request_is_denied_with_claim_instructions(db: str, gateway: str) -> None:
    status, body = post(gateway, "/v1/invoices", {"id": "I-1"})
    assert status == 403
    assert "continuum_intercept_action" in body["reason"]
    # Nothing was forwarded or recorded as evidence.
    with SQLiteStorage(db) as store:
        events = [e for e in store.read_events("run_1") if e.type is EventType.TOOL_COMPLETED]
    assert events == []


def test_claimed_request_is_forwarded_settled_and_recorded(db: str, gateway: str) -> None:
    """Forwarding requires a live claim; the upstream response settles it.

    api.example.com is not reachable from tests, so forwarding raises a
    network error and the claim becomes uncertain-failed - which is exactly
    the honest outcome for an unreachable effect. The enforcement and ledger
    behaviour is what this test pins; reachability belongs to production.
    """
    key = claim(db, "invoice:I-2")
    status, _ = post(gateway, "/v1/invoices", {"id": "I-2"})
    assert status == 502  # DNS failure for example.com inside CI sandboxes
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    action = folded[key]
    assert action.side_effect_uncertain is True
    assert action.status is ActionStatus.UNKNOWN


def test_completed_effect_blocks_the_duplicate(db: str, gateway: str) -> None:
    key = claim(db, "invoice:I-3")
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import ActionLedger as AL

        AL(store, "run_1").complete(key, external_id="sent")
    status, body = post(gateway, "/v1/invoices", {"id": "I-3"})
    assert status == 403
    assert "already completed" in body["reason"]


def test_a_padded_body_field_still_hits_the_duplicate_verdict(db: str, gateway: str) -> None:
    """The proxy has to derive the same key `gate` does (issue #361).

    The effect on ``invoice:I-4`` is already completed and the retry body says
    ``" I-4\\n"``, which names the same invoice. Before the fix the gateway
    rendered ``invoice: I-4\\n``, found no record of itself, and answered with
    claim instructions instead of the already-completed refusal -- so a client
    following those instructions would have sent the invoice a second time.
    """
    key = claim(db, "invoice:I-4")
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import ActionLedger as AL

        AL(store, "run_1").complete(key, external_id="sent")
    status, body = post(gateway, "/v1/invoices", {"id": " I-4\n"})
    assert status == 403
    assert "already completed" in body["reason"]


def test_unknown_host_is_refused_fail_closed(db: str, gateway: str) -> None:
    key = claim(db, "invoice:I-9")
    del key
    status, body = post(gateway, "/anything", {"id": "x"}, host="evil.example.net")
    assert status == 403
    assert "no upstream registered" in body["reason"]


def test_method_mismatch_is_refused(db: str, gateway: str) -> None:
    conn = http.client.HTTPConnection(gateway, timeout=10)
    conn.request("GET", "/v1/invoices", headers={"Host": "api.example.com"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 403
    assert "not among its allowed methods" in body["reason"]


def test_body_missing_template_field_denies_with_config_error(db: str, gateway: str) -> None:
    claim(db, "invoice:seed")
    status, body = post(gateway, "/v1/invoices", {})
    assert status == 403
    assert "key template" in body["reason"]


def _post_raw(addr: str, path: str, raw: str | bytes, host: str = "api.example.com"):
    """POST a body verbatim, bypassing the JSON encoding of :func:`post`.

    Accepts ``bytes`` as well as ``str`` so a test can send a body that is not
    valid UTF-8, which no encoding of a ``str`` can produce.
    """
    conn = http.client.HTTPConnection(addr, timeout=10)
    conn.request(
        "POST",
        path,
        body=raw.encode() if isinstance(raw, str) else raw,
        headers={"Host": host, "Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_malformed_json_is_refused_with_400(db: str, gateway: str) -> None:
    """Broken JSON must name itself, not masquerade as a missing field (#323).

    ``_body`` swallowed ``JSONDecodeError`` and returned an empty mapping, so a
    request whose body never parsed was carried on to the key derivation and
    refused with ``key template 'invoice:{id}' needs body field(s) ['id']``.
    The operator then goes looking for a field they did send, in a body the
    gateway never read.
    """
    claim(db, "invoice:seed")
    status, body = _post_raw(gateway, "/v1/invoices", '{"id": "I-5"')
    assert status == 400
    assert "invalid JSON" in body["error"]


def test_a_body_that_is_not_utf8_is_refused_with_400(db: str, gateway: str) -> None:
    """The decode half of "cannot be read" answers the same way (#323).

    ``json.loads`` decodes bytes before it parses them, so a body that is not
    valid UTF-8 raises ``UnicodeDecodeError`` rather than ``JSONDecodeError``.
    Uncaught, that escapes the handler and the connection closes with no
    response at all, so the caller cannot tell a rejected body from a crashed
    proxy.
    """
    claim(db, "invoice:seed")
    status, body = _post_raw(gateway, "/v1/invoices", b'{"id": "\xff\xfe I-5"}')
    assert status == 400
    assert "invalid JSON" in body["error"]


def test_an_empty_body_is_still_an_empty_mapping(db: str, gateway: str) -> None:
    """Only broken JSON becomes a 400; absent is not the same as malformed.

    A route whose template needs no fields is legitimately callable with no body,
    so the empty case has to keep reaching the decision table rather than being
    swept up by the new refusal.
    """
    claim(db, "invoice:seed")
    status, body = _post_raw(gateway, "/v1/invoices", "")
    assert status == 403
    assert "key template" in body["reason"]


# --- CLI ---------------------------------------------------------------------- #


def test_gateway_cli_refuses_to_start_without_routes(db: str, tmp_path: Path) -> None:
    import io

    from continuum.cli import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"upstreams": []}))
    code = cli_main(
        ["--db", db, "--json", "gateway", "--port", "0", "--config", str(empty)],
        out=out,
        err=err,
    )
    assert code != 0
    assert "open relay" in err.getvalue()


def test_oversized_body_is_refused_with_413(db: str, gateway: str) -> None:
    """A proxy reading unbounded bodies is a DoS surface against the agent."""
    conn = http.client.HTTPConnection(gateway, timeout=10)
    huge = json.dumps({"id": "x", "blob": "y" * (10 * 1024 * 1024 + 1)})
    conn.request(
        "POST",
        "/v1/invoices",
        body=huge,
        headers={"Host": "api.example.com", "Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 413
    assert "exceeds" in body["error"]


def test_malformed_content_length_returns_400(db: str, gateway: str) -> None:
    conn = http.client.HTTPConnection(gateway, timeout=10)
    conn.request(
        "POST",
        "/v1/invoices",
        body=b'{"id": "1"}',
        headers={
            "Host": "api.example.com",
            "Content-Type": "application/json",
            "Content-Length": "invalid",
        },
    )
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 400
    assert "malformed Content-Length" in body["error"]


def test_chunked_transfer_encoding_returns_400(db: str, gateway: str) -> None:
    conn = http.client.HTTPConnection(gateway, timeout=10)
    conn.request(
        "POST",
        "/v1/invoices",
        body=b'e\r\n{"id": "1"}\r\n0\r\n\r\n',
        headers={
            "Host": "api.example.com",
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
    )
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 400
    assert "transfer encoding is not supported" in body["error"]
    assert resp.getheader("Connection") == "close"


def test_duplicate_content_length_returns_400(db: str, gateway: str) -> None:
    host, port = gateway.split(":")
    request = (
        b"POST /v1/invoices HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 4\r\n"
        b"Content-Length: 9\r\n"
        b"\r\n"
        b'{"id": "1"}'
    )
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(request)
        response = recv_until_close(sock)

    assert b"400 Bad Request" in response
    assert b"Connection: close" in response
    assert b"multiple Content-Length headers are not supported" in response


def test_transfer_encoding_closes_gateway_connection(db: str, gateway: str) -> None:
    host, port = gateway.split(":")
    request = (
        b"POST /v1/invoices HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Transfer-Encoding: gzip\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"1\r\nx\r\n0\r\n\r\n"
        b"POST /v1/invoices HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(request)
        response = recv_until_close(sock)

    assert response.count(b"HTTP/1.1") == 1
    assert b"400 Bad Request" in response
    assert b"Connection: close" in response
    assert b"transfer encoding is not supported" in response
    assert b"501" not in response


def test_compacted_consumed_authority_still_denies(db: str, gateway: str) -> None:
    from continuum.actions.authority import record_authority_consumed
    from continuum.cli import main

    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "spent", via_action_id="original-action")
    assert main(["--db", db, "compact", "run_1", "--force"]) == 0
    status, body = post(gateway, "/v1/invoices", {"id": "new", "authority_id": "spent"})
    assert status == 403
    assert "spent" in body["reason"] and "consumed at seq" in body["reason"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"id": "INV-2", "payment": {"auth_token": "spent"}}, id="dict-nested"),
        pytest.param({"id": "INV-2", "payment": {"auth": ["spent"]}}, id="list-nested"),
    ],
)
def test_nested_consumed_authority_still_denies(db: str, gateway: str, payload: dict) -> None:
    """A spent authority nested in the body must not smuggle past the gateway (#1074)."""
    from continuum.actions.authority import record_authority_consumed

    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "spent", via_action_id="original-action")
    status, resp = post(gateway, "/v1/invoices", payload)
    assert status == 403
    assert "spent" in resp["reason"] and "consumed at seq" in resp["reason"]


@pytest.mark.parametrize("bad_length", ["abc", "-5"])
def test_malformed_length_smuggled_body_is_never_dispatched(
    db: str, gateway: str, bad_length: str
) -> None:
    """A refused body must not become the next request on a live socket (#611).

    The refused head carries no trustable body boundary, so the gateway must
    answer once and close. Whatever the client already wrote stays unread and
    must never be parsed as a second request.
    """
    host, port = gateway.split(":")
    smuggled = (
        b"POST /v1/invoices HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Content-Length: 11\r\n"
        b"\r\n"
        b'{"id":"X1"}'
    )
    request = (
        b"POST /v1/invoices HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"Content-Length: " + bad_length.encode() + b"\r\n"
        b"\r\n" + smuggled
    )
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(request)
        response = recv_until_close(sock)

    assert response.count(b"HTTP/1.1") == 1
    assert b"400 Bad Request" in response
    assert b"Connection: close" in response
    assert b"malformed Content-Length" in response


@pytest.mark.parametrize(
    ("requested", "prefix", "expected"),
    [
        # The boundary is a whole segment, not a string prefix.
        ("/v1/invoices", "/v1/invoices", True),
        ("/v1/invoices/49", "/v1/invoices", True),
        ("/v1/invoices/49/lines", "/v1/invoices", True),
        ("/v1/invoices", "/v1/invoices/", True),
        ("/v1/invoices-archived", "/v1/invoices", False),
        ("/v1/invoicesX", "/v1/invoices", False),
        ("/v1/refunds", "/v1/invoices", False),
        ("/v1/invoice", "/v1/invoices", False),
        ("/", "/v1/invoices", False),
        # A route without a prefix keeps the whole host, as it always has.
        ("/anything", "/", True),
        ("/anything/at/all", "", True),
        # The upstream rewrites these before dispatching, so the refusal has to
        # be about the path actually served.
        ("/v1/invoices/../refunds", "/v1/invoices", False),
        ("/v1/invoices/./49", "/v1/invoices", True),
        ("/v1/invoices/../../refunds", "/v1/invoices", False),
        # Percent-encoded traversal: the upstream decodes before it routes, so
        # %2e is a ".." the upstream honours even though the bytes differ.
        ("/v1/invoices/%2e%2e/refunds", "/v1/invoices", False),
        ("/v1/invoices/%2E%2E/refunds", "/v1/invoices", False),
        ("/v1/invoices/%2e/49", "/v1/invoices", True),
        # An encoded character in the prefix's own segment still matches.
        ("/files/a%2Bb/49", "/files/a+b", True),
        # A query is not part of the path scope.
        ("/v1/invoices/49?dry_run=1", "/v1/invoices", True),
        ("/v1/refunds?x=1", "/v1/invoices", False),
        # A request line that is not absolute is still compared absolutely.
        ("v1/invoices", "/v1/invoices", True),
        ("v1/refunds", "/v1/invoices", False),
    ],
)
def test_prefix_boundary_is_a_whole_segment(requested: str, prefix: str, expected: bool) -> None:
    """The prefix is the only per-path scope a route has (issue #1051).

    Without a segment boundary the check is decorative: ``/v1/invoices`` as a
    string prefix admits ``/v1/invoices-archived``, a different resource the
    claim says nothing about.
    """
    from continuum.gateway import _path_under_prefix

    assert _path_under_prefix(requested, prefix) is expected


def test_a_live_claim_cannot_reach_another_path(db: str, gateway: str) -> None:
    """A claim for /v1/invoices does not authorise /v1/refunds (#1051).

    Before the fix the prefix was parsed onto the Route record and never
    compared, so the whole host was the route's scope: this request would have
    been forwarded, settled as completed, and recorded as evidence that the
    invoice was sent while the upstream saw a refund.
    """
    key = claim(db, "invoice:I-5")
    status, body = post(gateway, "/v1/refunds", {"id": "I-5"})
    assert status == 403
    assert "not under any of its prefixes" in body["reason"]

    # Nothing was forwarded, so nothing was settled and nothing recorded.
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
        assert folded[key].status is ActionStatus.STARTED
        events = [e for e in store.read_events("run_1") if e.type is EventType.TOOL_COMPLETED]
    assert events == []


def test_the_prefix_is_enforced_through_match_route(tmp_path: Path) -> None:
    """The prefix narrows before the key is rendered (#1051)."""
    from continuum.actions.ledger import fold_action_events

    route = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(store, "run_1")
    ledger.claim("send_invoice", {"id": "I-6"}, key="invoice:I-6")
    actions = fold_action_events(store.read_events("run_1"))

    def decide(path: str) -> Decision:
        return match_route(
            [route],
            host="api.example.com",
            method="POST",
            path=path,
            body={"id": "I-6"},
            actions_by_key=actions,
            run_id="run_1",
        )

    # The claimed path is still allowed; a sibling path is refused even though
    # the same key has a live claim behind it.
    assert decide("/v1/invoices").allow is True
    off_prefix = decide("/v1/refunds")
    assert off_prefix.allow is False
    assert "/v1/refunds" in off_prefix.reason
    assert decide("/v1/invoices/49").allow is True
    assert decide("/v1/invoices/../refunds").allow is False


def test_a_doubly_encoded_separator_does_not_buy_the_prefix(tmp_path: Path) -> None:
    """The path is decoded once, as the upstream does, not twice (#1051).

    ``unquote`` is not idempotent: ``/v1%252finvoices/49`` decodes to
    ``/v1%2finvoices/49`` once and to ``/v1/invoices/49`` twice. The second
    pass made the gateway see the invoice path, spend the claim, and record
    evidence that the invoice was sent, while the upstream decoded once and
    served one literal segment that never reached the invoice endpoint. The
    recorded path is the raw request line, so the verdict also has to be about
    the raw line and not a pre-normalised stand-in.
    """
    from continuum.actions.ledger import fold_action_events

    route = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(store, "run_1")
    outcome = ledger.claim("send_invoice", {"id": "I-6"}, key="invoice:I-6")
    actions = fold_action_events(store.read_events("run_1"))

    decision = match_route(
        [route],
        host="api.example.com",
        method="POST",
        path="/v1%252finvoices/49",
        body={"id": "I-6"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is False
    assert "not under any of its prefixes" in decision.reason
    # The refusal names what the upstream actually serves, not the
    # twice-decoded path the old double normalisation invented.
    assert "/v1%2finvoices/49" in decision.reason

    # Nothing was forwarded, so the claim is still live and unspent.
    folded = fold_action_events(store.read_events("run_1"))
    assert folded[outcome.key].status is ActionStatus.STARTED

    # The same raw line, genuinely encoded once, is the invoice path and is
    # admitted: decoding exactly once does not tighten the boundary.
    once = match_route(
        [route],
        host="api.example.com",
        method="POST",
        path="/v1/invoices/%34%39",
        body={"id": "I-6"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert once.allow is True


def test_two_prefixes_on_one_host_route_to_the_right_claim(tmp_path: Path) -> None:
    """A host can carry more than one operation; the path picks the route."""
    from continuum.actions.ledger import fold_action_events

    routes = [
        Route(
            host="api.example.com",
            methods=("POST",),
            prefix="/v1/invoices",
            action_type="send_invoice",
            key_template="invoice:{id}",
        ),
        Route(
            host="api.example.com",
            methods=("POST",),
            prefix="/v1/refunds",
            action_type="refund_invoice",
            key_template="refund:{id}",
        ),
    ]
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(store, "run_1")
    ledger.claim("refund_invoice", {"id": "I-7"}, key="refund:I-7")
    actions = fold_action_events(store.read_events("run_1"))

    # A live refund claim does not authorise the invoice path, and the refusal
    # names the prefixes the host does serve rather than the unclaimed key.
    decision = match_route(
        routes,
        host="api.example.com",
        method="POST",
        path="/v1/invoices",
        body={"id": "I-7"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is False
    assert "no ledger claim" in decision.reason
    assert "send_invoice" in decision.reason

    claimed = match_route(
        routes,
        host="api.example.com",
        method="POST",
        path="/v1/refunds",
        body={"id": "I-7"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert claimed.allow is True
    assert claimed.route is not None
    assert claimed.route.action_type == "refund_invoice"


def test_a_route_without_a_prefix_keeps_the_whole_host(tmp_path: Path) -> None:
    """Omitting ``prefix`` has always meant the host, not a broken route."""
    from continuum.actions.ledger import fold_action_events

    route = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ActionLedger(store, "run_1").claim("send_invoice", {"id": "I-8"}, key="invoice:I-8")
    actions = fold_action_events(store.read_events("run_1"))
    decision = match_route(
        [route],
        host="api.example.com",
        method="POST",
        path="/v1/anything-at-all",
        body={"id": "I-8"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is True
