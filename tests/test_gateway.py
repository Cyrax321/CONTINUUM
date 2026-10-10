"""The enforcing HTTP gateway (seam 4).

A local proxy that refuses unclaimed outbound requests to registered
upstreams and settles claims from real upstream responses. A plain-HTTP
upstream is driven through the actual HTTP stack against a live server on an
ephemeral port; the failure modes a real upstream cannot be made to produce on
demand (a truncated reply, an oversized one, a DNS-unreachable host) use a
canned connection instead.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    load_gateway_tenant,
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


# The scheme tests replace ``http.client``'s connection classes to see which one
# the gateway dials out with, and ``post`` dials in through the same class --
# so a patched ``HTTPConnection`` would fake the request that drives the
# gateway and hand it a canned 200 that never reached the proxy. Captured at
# import time, before any patch, this is the real socket the test talks to.
_REAL_HTTP_CONNECTION = http.client.HTTPConnection


def _post_to_gateway(
    addr: str, path: str, body: dict[str, object], host: str = "api.example.com"
) -> tuple[int, dict[str, object]]:
    conn = _REAL_HTTP_CONNECTION(addr, timeout=10)
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


def _one_upstream(tmp_path: Path, **overrides: object) -> Path:
    entry: dict[str, object] = {
        "host": "api.example.com",
        "methods": ["POST"],
        "prefix": "/v1/invoices",
        "action_type": "send_invoice",
        "key_template": "invoice:{id}",
    }
    entry.update(overrides)
    p = tmp_path / "gateway.json"
    p.write_text(json.dumps({"upstreams": [entry]}))
    return p


@pytest.mark.parametrize("origin,expected", [("http://a.com", "http"), ("https://a.com", "https")])
def test_a_scheme_prefix_on_the_host_is_carried_to_the_route(
    tmp_path: Path, origin: str, expected: str
) -> None:
    """``http://a.com`` names a plain-HTTP upstream and leaves the host bare.

    The host is stored without its prefix: every consumer downstream splits or
    compares it as a bare authority, and a scheme left on it would make
    ``_normalize_host`` read ``http`` as the name.
    """
    routes = load_gateway_config(_one_upstream(tmp_path, host=origin))
    assert routes[0].scheme == expected
    assert routes[0].host == "a.com"


def test_a_scheme_spelled_two_agreeing_ways_is_accepted(tmp_path: Path) -> None:
    routes = load_gateway_config(_one_upstream(tmp_path, host="http://a.com", scheme="http"))
    assert routes[0].scheme == "http"
    assert routes[0].host == "a.com"


def test_a_scheme_that_disagrees_with_its_own_host_is_rejected(tmp_path: Path) -> None:
    """One entry naming two upstreams cannot tell the operator which would run."""
    with pytest.raises(GatewayConfigError, match="while its 'scheme' field says 'http'"):
        load_gateway_config(_one_upstream(tmp_path, host="https://a.com", scheme="http"))


@pytest.mark.parametrize("scheme", ["ftp", "ws", "HTTPD"])
def test_an_unknown_scheme_is_rejected(tmp_path: Path, scheme: str) -> None:
    with pytest.raises(GatewayConfigError, match="is not one of"):
        load_gateway_config(_one_upstream(tmp_path, scheme=scheme))


def test_an_unknown_scheme_prefix_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GatewayConfigError, match="uses scheme 'ftp'"):
        load_gateway_config(_one_upstream(tmp_path, host="ftp://a.com"))


def test_a_scheme_with_no_host_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GatewayConfigError, match="names a scheme but no host"):
        load_gateway_config(_one_upstream(tmp_path, host="http://"))


def test_an_https_and_an_http_route_for_one_host_collide(tmp_path: Path) -> None:
    """A ``Host`` header carries no scheme, so one host cannot name two upstreams.

    The request cannot tell the registry which it meant, so the collision is
    rejected at load time, where the operator still has the file open, rather
    than resolved by registry order at request time. The two routes below agree
    on host, port, prefix and method and differ only in scheme -- the one axis a
    request cannot carry.
    """
    p = tmp_path / "gateway.json"
    p.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "http://a.com",
                        "methods": ["POST"],
                        "prefix": "/v1/invoices",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{id}",
                    },
                    {
                        "host": "https://a.com",
                        "methods": ["POST"],
                        "prefix": "/v1/invoices",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{other}",
                    },
                ]
            }
        )
    )
    with pytest.raises(GatewayConfigError, match="repeats"):
        load_gateway_config(p)


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


def test_a_truncated_upstream_reply_settles_the_claim_uncertain(
    db: str, gateway: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed/truncated upstream response must not leave a live STARTED claim.

    ``conn.getresponse()``/``resp.read()`` can raise ``http.client.HTTPException``
    subclasses (``IncompleteRead``, ``BadStatusLine``, ...) that are *not*
    ``OSError``. A catch of only ``OSError`` let them escape ``_handle`` with the
    claim still STARTED; a retry then saw a live claim and forwarded the effect a
    second time -- the double-fire the gateway exists to prevent. The request may
    already have reached the upstream and fired the effect, so the honest
    settlement is UNKNOWN (uncertain), exactly as for a dropped connection.
    """

    class _TruncatedResponse:
        status = 200

        # The gateway reads in bounded chunks, so read() takes the chunk size.
        def read(self, amt: int | None = None) -> bytes:
            """Simulate reading a chunk that raises IncompleteRead."""
            raise http.client.IncompleteRead(b"partial", 512)

    class _TruncatedConn:
        def __init__(self, netloc: str, timeout: int | None = None) -> None:
            pass

        def request(self, *args: object, **kwargs: object) -> None:
            pass

        def getresponse(self) -> _TruncatedResponse:
            return _TruncatedResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", _TruncatedConn)

    key = claim(db, "invoice:I-read")
    status, _ = post(gateway, "/v1/invoices", {"id": "I-read"})
    assert status == 502
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        action = fold_action_events(store.read_events("run_1"))[key]
    assert action.status is ActionStatus.UNKNOWN
    assert action.side_effect_uncertain is True


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


def _mem_scenario(final: str) -> Decision:
    """Seed a foreign run holding a global memory key in ``final`` state, then
    return the gateway's verdict for a second run that has no local claim."""
    from continuum.actions.ledger import fold_action_events

    memkey = "mem:store1:tenantA:rec1"
    atype = "mem_write"
    store = SQLiteStorage(":memory:")
    for rid in ("runA", "runB"):
        store.create_run(Run(run_id=rid, goal="g"))
        store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
    other = ActionLedger(store, "runA")
    oc = other.claim(atype, {"k": "rec1"}, key=memkey, scoped_to_run=False)
    if final == "failed":
        other.fail(oc.key, "rejected upstream", certain=True)
    elif final == "unknown":
        other.fail(oc.key, "connection dropped", certain=False)
    elif final == "compensated":
        other.complete(oc.key)
        other.compensate(oc.key, note="rolled back")
    elif final == "completed":
        other.complete(oc.key, external_id="EXT1")
    # "started" leaves the foreign claim live.
    route = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/mem",
        action_type=atype,
        key_template=memkey,
    )
    actions_b = fold_action_events(store.read_events("runB"))
    return match_route(
        [route],
        host="api.example.com",
        method="POST",
        path="/v1/mem",
        body={"k": "rec1"},
        actions_by_key=actions_b,
        run_id="runB",
        storage=store,
    )


def test_foreign_memory_record_gets_a_status_specific_verdict() -> None:
    """A foreign claim on a global memory key is denied with guidance that
    matches its status, mirroring gate.decide rather than telling the caller
    to "reconcile" a record that cannot be reconciled.

    Regression for the blanket "reconcile it first" message: reconcile only
    fits an UNKNOWN outcome. A terminal foreign record (failed/compensated)
    left no live effect, so the way forward is a fresh claim (#765e4bc); a
    completed one must not be repeated. Every foreign status is still denied.
    """
    started = _mem_scenario("started")
    assert started.allow is False
    assert "claimed live in another run" in started.reason

    completed = _mem_scenario("completed")
    assert completed.allow is False
    assert "already completed in another run" in completed.reason
    assert "do not repeat" in completed.reason
    assert "EXT1" in completed.reason

    unknown = _mem_scenario("unknown")
    assert unknown.allow is False
    assert "unknown outcome in another run" in unknown.reason
    assert "reconcile it first (continuum_reconcile_action)" in unknown.reason

    failed = _mem_scenario("failed")
    assert failed.allow is False
    assert "claim it again through continuum_intercept_action" in failed.reason
    assert "closed (status failed)" in failed.reason

    compensated = _mem_scenario("compensated")
    assert compensated.allow is False
    assert "closed (status compensated)" in compensated.reason
    assert "reconcile it first" not in compensated.reason


def test_gateway_enforces_tenant_scoped_memory_boundary_and_header(tmp_path: Path) -> None:
    """Enforce tenant-scoped namespace boundaries on external memory claims (#1415)."""
    from continuum.actions.ledger import fold_action_events
    from continuum.gate import is_memory_key

    # Standardized key convention: memory:<store_id>:<tenant_id>:<namespace>:<record_key>
    template = "memory:{store_id}:{tenant_id}:{namespace}:{record_key}"
    assert is_memory_key(template)

    route = Route(
        host="vector.internal",
        methods=("POST",),
        prefix="/v1/memories",
        action_type="memory_write",
        key_template=template,
    )
    store = SQLiteStorage(":memory:")
    # Run with bound tenant metadata
    store.create_run(Run(run_id="run_t1", goal="g", metadata={"tenant_id": "tenant_alpha"}))
    store.append_event("run_t1", EventType.RUN_STARTED, {"goal": "g"})

    # Claim for authorized tenant
    ActionLedger(store, "run_t1").claim(
        "memory_write",
        {},
        key="memory:pgvector:tenant_alpha:kb:doc-1",
        scoped_to_run=False,
    )
    actions = fold_action_events(store.read_events("run_t1"))

    # Authorized request matches and is allowed
    authorized_body = {
        "store_id": "pgvector",
        "tenant_id": "tenant_alpha",
        "namespace": "kb",
        "record_key": "doc-1",
    }
    decision_ok = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories/upsert",
        body=authorized_body,
        actions_by_key=actions,
        run_id="run_t1",
        bound_tenant="tenant_alpha",
    )
    assert decision_ok.allow is True

    # Cross-tenant request is denied with tenant mismatch
    cross_tenant_body = {
        "store_id": "pgvector",
        "tenant_id": "tenant_beta",
        "namespace": "kb",
        "record_key": "doc-1",
    }
    decision_deny = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories/upsert",
        body=cross_tenant_body,
        actions_by_key=actions,
        run_id="run_t1",
        bound_tenant="tenant_alpha",
    )
    assert decision_deny.allow is False
    assert "tenant mismatch" in decision_deny.reason


def test_gateway_server_header_tenant_boundary_enforcement(tmp_path: Path) -> None:
    """GatewayServer extracts tenant from X-Continuum-Tenant and denies cross-tenant write (#1415)."""
    db_path = str(tmp_path / "gw_tenant.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_gw_t", goal="g", metadata={"tenant_id": "acme"}))
        store.append_event("run_gw_t", EventType.RUN_STARTED, {"goal": "g"})
        ActionLedger(store, "run_gw_t").claim(
            "memory_write",
            {},
            key="memory:vstore:acme:ns1:k1",
            scoped_to_run=False,
        )

    routes = [
        Route(
            host="vector.internal",
            methods=("POST",),
            prefix="/v1/memories",
            action_type="memory_write",
            key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
        )
    ]
    # Server initialized without static bound_tenant
    server = GatewayServer(lambda: SQLiteStorage(db_path), "run_gw_t", routes, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        # Cross-tenant write with X-Continuum-Tenant: acme
        conn = http.client.HTTPConnection(addr, timeout=10)
        cross_body = {
            "store_id": "vstore",
            "tenant_id": "globex",
            "namespace": "ns1",
            "record_key": "k1",
        }
        conn.request(
            "POST",
            "/v1/memories/upsert",
            body=json.dumps(cross_body),
            headers={
                "Host": "vector.internal",
                "Content-Type": "application/json",
                "X-Continuum-Tenant": "acme",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 403
        data = json.loads(resp.read() or b"{}")
        assert "tenant mismatch" in data.get("reason", "")
        conn.close()
    finally:
        server.shutdown()


def test_gateway_server_run_metadata_tenant_boundary_enforcement(tmp_path: Path) -> None:
    """GatewayServer extracts tenant from run context metadata when no header is present (#1415)."""
    db_path = str(tmp_path / "gw_run_tenant.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_meta_t", goal="g", metadata={"tenant_id": "tenant_x"}))
        store.append_event("run_meta_t", EventType.RUN_STARTED, {"goal": "g"})
        ActionLedger(store, "run_meta_t").claim(
            "memory_write",
            {},
            key="memory:vstore:tenant_x:kb:doc-1",
            scoped_to_run=False,
        )

    routes = [
        Route(
            host="vector.internal",
            methods=("POST",),
            prefix="/v1/memories",
            action_type="memory_write",
            key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
        )
    ]
    server = GatewayServer(lambda: SQLiteStorage(db_path), "run_meta_t", routes, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        # Cross-tenant write with body tenant_id = tenant_y, run tenant = tenant_x
        conn = http.client.HTTPConnection(addr, timeout=10)
        cross_body = {
            "store_id": "vstore",
            "tenant_id": "tenant_y",
            "namespace": "kb",
            "record_key": "doc-1",
        }
        conn.request(
            "POST",
            "/v1/memories/upsert",
            body=json.dumps(cross_body),
            headers={
                "Host": "vector.internal",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 403
        data = json.loads(resp.read() or b"{}")
        assert "tenant mismatch" in data.get("reason", "")
        conn.close()
    finally:
        server.shutdown()


def test_gateway_server_header_conflicts_with_run_tenant(tmp_path: Path) -> None:
    """Header tenant conflicting with run metadata tenant is denied (#1415)."""
    db_path = str(tmp_path / "gw_conflict.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_c", goal="g", metadata={"tenant_id": "tenant_real"}))
        store.append_event("run_c", EventType.RUN_STARTED, {"goal": "g"})

    routes = [
        Route(
            host="vector.internal",
            methods=("POST",),
            prefix="/v1/memories",
            action_type="memory_write",
            key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
        )
    ]
    server = GatewayServer(lambda: SQLiteStorage(db_path), "run_c", routes, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/v1/memories/upsert",
            body=json.dumps(
                {
                    "store_id": "vstore",
                    "tenant_id": "tenant_real",
                    "namespace": "kb",
                    "record_key": "doc-1",
                }
            ),
            headers={
                "Host": "vector.internal",
                "Content-Type": "application/json",
                "X-Continuum-Tenant": "tenant_spoofed",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 403
        data = json.loads(resp.read() or b"{}")
        assert "tenant mismatch" in data.get("reason", "")
        conn.close()
    finally:
        server.shutdown()


def test_load_gateway_tenant_reads_tenant_id(tmp_path: Path) -> None:
    """load_gateway_tenant supports 'tenant_id' alongside 'bound_tenant' and 'tenant' (#1415)."""
    cfg = tmp_path / "gateway_id.json"
    cfg.write_text(json.dumps({"tenant_id": "tenant_omega"}))
    assert load_gateway_tenant(cfg) == "tenant_omega"

    cfg_bound = tmp_path / "gateway_bound.json"
    cfg_bound.write_text(json.dumps({"bound_tenant": "tenant_alpha"}))
    assert load_gateway_tenant(cfg_bound) == "tenant_alpha"

    cfg_plain = tmp_path / "gateway_plain.json"
    cfg_plain.write_text(json.dumps({"tenant": "tenant_beta"}))
    assert load_gateway_tenant(cfg_plain) == "tenant_beta"


def test_gateway_cli_tenant_flag() -> None:
    """CLI parser accepts --tenant option for gateway command (#1415)."""
    from continuum.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["gateway", "--tenant", "tenant_prod"])
    assert args.tenant == "tenant_prod"


def test_load_gateway_config_validates_memory_templates(tmp_path: Path) -> None:
    """load_gateway_config requires namespace for memory: templates (#1415)."""
    bad_cfg = tmp_path / "bad_gw.json"
    bad_cfg.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "vector.internal",
                        "action_type": "memory_write",
                        "key_template": "memory:{store_id}:{tenant_id}:{record_key}",
                    }
                ]
            }
        )
    )
    with pytest.raises(GatewayConfigError, match="missing required placeholder"):
        load_gateway_config(bad_cfg)

    good_cfg = tmp_path / "good_gw.json"
    good_cfg.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "vector.internal",
                        "action_type": "memory_write",
                        "key_template": "memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
                    }
                ]
            }
        )
    )
    routes = load_gateway_config(good_cfg)
    assert len(routes) == 1
    assert routes[0].key_template == "memory:{store_id}:{tenant_id}:{namespace}:{record_key}"


def test_match_route_rejects_colon_in_body_fields() -> None:
    """Colon in body values is rejected as malformed memory key (#1415)."""
    route = Route(
        host="vector.internal",
        methods=("POST",),
        prefix="/v1/memories",
        action_type="memory_write",
        key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
    )
    body = {
        "store_id": "vstore:corrupted",
        "tenant_id": "acme",
        "namespace": "kb",
        "record_key": "k1",
    }
    decision = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories",
        body=body,
        actions_by_key={},
        run_id="run_1",
        bound_tenant="acme",
    )
    assert decision.allow is False
    assert "must not contain ':'" in decision.reason


def test_match_route_supports_flexible_placeholder_order() -> None:
    """Tenant check works regardless of placeholder position in memory template (#1415)."""
    route = Route(
        host="vector.internal",
        methods=("POST",),
        prefix="/v1/memories",
        action_type="memory_write",
        key_template="memory:{store_id}:{namespace}:{tenant_id}:{record_key}",
    )
    body_ok = {
        "store_id": "vstore",
        "namespace": "kb",
        "tenant_id": "acme",
        "record_key": "k1",
    }
    decision_ok = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories",
        body=body_ok,
        actions_by_key={},
        run_id="run_1",
        bound_tenant="acme",
    )
    # Claim not found in actions_by_key, but tenant check passed
    assert "tenant mismatch" not in decision_ok.reason

    body_bad = {
        "store_id": "vstore",
        "namespace": "kb",
        "tenant_id": "globex",
        "record_key": "k1",
    }
    decision_bad = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories",
        body=body_bad,
        actions_by_key={},
        run_id="run_1",
        bound_tenant="acme",
    )
    assert decision_bad.allow is False
    assert "tenant mismatch" in decision_bad.reason


def test_gateway_server_header_conflicts_with_bound_tenant(tmp_path: Path) -> None:
    """Header tenant conflicting with server bound tenant is denied (#1415)."""
    db_path = str(tmp_path / "gw_bound_conflict.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_bc", goal="g"))
        store.append_event("run_bc", EventType.RUN_STARTED, {"goal": "g"})

    routes = [
        Route(
            host="vector.internal",
            methods=("POST",),
            prefix="/v1/memories",
            action_type="memory_write",
            key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
        )
    ]
    server = GatewayServer(
        lambda: SQLiteStorage(db_path),
        "run_bc",
        routes,
        port=0,
        bound_tenant="tenant_primary",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/v1/memories/upsert",
            body=json.dumps(
                {
                    "store_id": "vstore",
                    "tenant_id": "tenant_primary",
                    "namespace": "kb",
                    "record_key": "doc-1",
                }
            ),
            headers={
                "Host": "vector.internal",
                "Content-Type": "application/json",
                "X-Continuum-Tenant": "tenant_other",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 403
        data = json.loads(resp.read() or b"{}")
        assert "tenant mismatch" in data.get("reason", "")
        conn.close()
    finally:
        server.shutdown()


def test_gateway_server_ignores_upstream_tenant_headers_on_non_memory_routes(
    tmp_path: Path,
) -> None:
    """Non-Continuum tenant headers like X-Tenant-Id are ignored by tenant boundary check (#1415)."""
    db_path = str(tmp_path / "gw_upstream_header.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_uh", goal="g"))
        store.append_event("run_uh", EventType.RUN_STARTED, {"goal": "g"})

    routes = [
        Route(
            host="api.upstream.com",
            methods=("POST",),
            prefix="/v1/invoices",
            action_type="send_invoice",
            key_template="invoice:{id}",
        )
    ]
    server = GatewayServer(
        lambda: SQLiteStorage(db_path),
        "run_uh",
        routes,
        port=0,
        bound_tenant="tenant_contin",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/v1/invoices",
            body=json.dumps({"id": "123"}),
            headers={
                "Host": "api.upstream.com",
                "Content-Type": "application/json",
                "X-Tenant-Id": "upstream_tenant_abc",
            },
        )
        resp = conn.getresponse()
        # Request will be denied because invoice:123 is unclaimed, but NOT due to tenant mismatch
        assert resp.status == 403
        data = json.loads(resp.read() or b"{}")
        assert "tenant mismatch" not in data.get("reason", "")
        conn.close()
    finally:
        server.shutdown()


def test_gateway_server_handles_unknown_run_id_cleanly(tmp_path: Path) -> None:
    """Unknown run_id does not raise unhandled RunNotFound exception (#1415)."""
    db_path = str(tmp_path / "gw_unknown_run.db")
    routes = [
        Route(
            host="vector.internal",
            methods=("POST",),
            prefix="/v1/memories",
            action_type="memory_write",
            key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
        )
    ]
    server = GatewayServer(
        lambda: SQLiteStorage(db_path),
        "nonexistent_run_id",
        routes,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/v1/memories",
            body=json.dumps(
                {"store_id": "v", "tenant_id": "t", "namespace": "n", "record_key": "k"}
            ),
            headers={"Host": "vector.internal", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 403
        conn.close()
    finally:
        server.shutdown()


def test_cmd_gateway_runs_with_tenant_flag(tmp_path: Path) -> None:
    """cmd_gateway sets bound_tenant from args.tenant (#1415)."""
    import argparse
    import io
    from unittest.mock import patch

    from continuum.cli.main import cmd_gateway

    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "api.example.com",
                        "action_type": "send",
                        "key_template": "key:{id}",
                    }
                ]
            }
        )
    )
    db_path = str(tmp_path / "cli_gw.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_cli_gw", goal="g"))
        store.append_event("run_cli_gw", EventType.RUN_STARTED, {"goal": "g"})

        args = argparse.Namespace(
            config=str(cfg),
            tenant="tenant_cli_override",
            run_id="run_cli_gw",
            db=db_path,
            port=0,
        )
        out = io.StringIO()
        err = io.StringIO()
        with patch("continuum.gateway.GatewayServer") as mock_server:
            mock_server.return_value.serve_forever.return_value = None
            mock_server.return_value.port = 12345
            code = cmd_gateway(args, store, out, err)
            assert code == 0
            assert mock_server.call_args.kwargs["bound_tenant"] == "tenant_cli_override"


def test_match_route_falls_back_to_run_metadata_tenant(tmp_path: Path) -> None:
    """match_route extracts bound tenant from storage run metadata when bound_tenant is None (#1415)."""
    route = Route(
        host="vector.internal",
        methods=("POST",),
        prefix="/v1/memories",
        action_type="memory_write",
        key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
    )
    store = SQLiteStorage(str(tmp_path / "meta_tenant.db"))
    store.create_run(Run(run_id="run_m", goal="g", metadata={"tenant": "tenant_from_meta"}))
    store.append_event("run_m", EventType.RUN_STARTED, {"goal": "g"})

    body_mismatch = {
        "store_id": "vstore",
        "tenant_id": "other_tenant",
        "namespace": "kb",
        "record_key": "k1",
    }
    decision = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories",
        body=body_mismatch,
        actions_by_key={},
        run_id="run_m",
        bound_tenant=None,
        storage=store,
    )
    assert decision.allow is False
    assert "tenant mismatch" in decision.reason
    assert "tenant_from_meta" in decision.reason


def test_match_route_with_run_metadata_missing_tenant(tmp_path: Path) -> None:
    """match_route skips tenant check when run metadata has no tenant (#1415)."""
    route = Route(
        host="vector.internal",
        methods=("POST",),
        prefix="/v1/memories",
        action_type="memory_write",
        key_template="memory:{store_id}:{tenant_id}:{namespace}:{record_key}",
    )
    store = SQLiteStorage(str(tmp_path / "meta_no_tenant.db"))
    store.create_run(Run(run_id="run_no_t", goal="g", metadata={"env": "prod"}))
    store.append_event("run_no_t", EventType.RUN_STARTED, {"goal": "g"})

    body = {
        "store_id": "vstore",
        "tenant_id": "any_tenant",
        "namespace": "kb",
        "record_key": "k1",
    }
    decision = match_route(
        [route],
        host="vector.internal",
        method="POST",
        path="/v1/memories",
        body=body,
        actions_by_key={},
        run_id="run_no_t",
        bound_tenant=None,
        storage=store,
    )
    assert "tenant mismatch" not in decision.reason


def test_a_port_bound_route_is_reachable_through_the_live_gateway(db: str, tmp_path: Path) -> None:
    """The live server hands the raw Host header, port included, to match_route.

    The first pass at #1342 had the server strip the port before calling
    ``match_route``, so the matcher could never see one: every route had to be
    registered without a port or it was dead, and the fix lived only in the
    unit tests. This exercises the seam that actually broke -- the header the
    socket sees, not the argument a test passes.
    """
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "api.example.com:8443",
                        "methods": ["POST"],
                        "prefix": "/v1/invoices",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{id}",
                    }
                ]
            }
        )
    )
    server = GatewayServer(lambda: SQLiteStorage(db), "run_1", load_gateway_config(cfg), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.port}"
    try:
        # Unclaimed, so the refusal is a claim instruction -- which is only
        # reachable at all when the route matched, rather than "no upstream".
        status, body = post(addr, "/v1/invoices", {"id": "I-9"}, host="api.example.com:8443")
        assert status == 403
        assert "no upstream registered" not in body["reason"]

        # The default port is a different destination and must not reach the
        # port-bound route.
        status, body = post(addr, "/v1/invoices", {"id": "I-9"}, host="api.example.com")
        assert status == 403
        assert "no upstream registered" in body["reason"]
    finally:
        server.shutdown()


def test_the_most_specific_prefix_wins_regardless_of_registry_order(tmp_path: Path) -> None:
    """A broad route must not shadow a narrower one that also admits the path.

    ``match_route`` narrowed a host's routes by prefix and then took the first
    survivor in registry order (issue #1341). A route with a broad prefix
    therefore shadowed a route with a narrower one that also admitted the path,
    so two configs identical but for the order of their ``upstreams`` array
    rendered different keys and consulted, or spent, a different claim. The
    winner is now the longest matching prefix, whichever order the list is in.
    """
    from continuum.actions.ledger import fold_action_events

    broad = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:invoice",
    )
    specific = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices/archive",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:archived",
    )
    body = {"store_id": "pg", "tenant": "acme"}

    def decide(routes: list[Route]) -> Decision:
        store = SQLiteStorage(":memory:")
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        ledger.claim("mem_write", {}, key="mem:pg:acme:archived", scoped_to_run=False)
        actions = fold_action_events(store.read_events("run_1"))
        return match_route(
            routes,
            host="api.example.com",
            method="POST",
            path="/v1/invoices/archive/2024",
            body=body,
            actions_by_key=actions,
            run_id="run_1",
            bound_tenant="acme",
        )

    # The caller claimed the key the archive endpoint renders; the request is
    # allowed and lands on the specific route no matter how the list is ordered.
    for routes in ([broad, specific], [specific, broad]):
        decision = decide(routes)
        assert decision.allow is True, decision.reason
        assert decision.route is not None
        assert decision.route.prefix == "/v1/invoices/archive"


def test_a_claim_for_the_broad_route_no_longer_spends_on_the_specific_path(
    tmp_path: Path,
) -> None:
    """The mirror of #1341: the broad key must not settle the specific request.

    Before the fix a claim for the broad prefix was spendable on the narrower
    endpoint whenever the broad route happened to be listed first, so the run's
    evidence said the broad operation ran while the upstream served the
    specific one. The specific route now wins, and a request that carries only
    the broad claim is refused, naming the key the specific route renders.
    """
    from continuum.actions.ledger import fold_action_events

    broad = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:invoice",
    )
    specific = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices/archive",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:archived",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ledger = ActionLedger(store, "run_1")
    ledger.claim("mem_write", {}, key="mem:pg:acme:invoice", scoped_to_run=False)
    actions = fold_action_events(store.read_events("run_1"))

    decision = match_route(
        [broad, specific],
        host="api.example.com",
        method="POST",
        path="/v1/invoices/archive/2024",
        body={"store_id": "pg", "tenant": "acme"},
        actions_by_key=actions,
        run_id="run_1",
        bound_tenant="acme",
    )
    assert decision.allow is False
    assert "mem:pg:acme:archived" in decision.reason


@pytest.mark.parametrize(
    ("route_host", "request_host"),
    [
        ("api.example.com", "API.EXAMPLE.COM"),  # client upper-cased the header
        ("API.EXAMPLE.COM", "api.example.com"),  # config upper-cased the host
        ("Api.Example.Com", "api.example.COM"),  # mixed on both sides
    ],
)
def test_host_matching_is_case_insensitive(
    route_host: str, request_host: str, tmp_path: Path
) -> None:
    """HTTP host names are case-insensitive (RFC 7230 §5.4), issue #1342.

    A client sending ``Host: API.EXAMPLE.COM`` against a route registered as
    ``api.example.com`` was refused with "no upstream registered for host",
    for a spelling the protocol says is not a difference.
    """
    from continuum.actions.ledger import fold_action_events

    route = Route(
        host=route_host,
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ActionLedger(store, "run_1").claim("send_invoice", {"id": "I-9"}, key="invoice:I-9")
    actions = fold_action_events(store.read_events("run_1"))

    decision = match_route(
        [route],
        host=request_host,
        method="POST",
        path="/v1/invoices/49",
        body={"id": "I-9"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is True, decision.reason


def test_a_port_bound_route_is_reachable_when_the_request_carries_the_port(
    tmp_path: Path,
) -> None:
    """A route registered with a port must match a request that names it (#1342).

    The server used to hand ``host.split(":")[0]`` to ``match_route``, so a
    route registered as ``api.example.com:8443`` never matched anything and was
    silently dead. It now passes the raw header; the port is part of the
    destination, so it has to reach the matcher. The route keeps its ``host``
    verbatim for the upstream connection.
    """
    from continuum.actions.ledger import fold_action_events

    route = Route(
        host="api.example.com:8443",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ActionLedger(store, "run_1").claim("send_invoice", {"id": "I-9"}, key="invoice:I-9")
    actions = fold_action_events(store.read_events("run_1"))

    decision = match_route(
        [route],
        host="api.example.com:8443",
        method="POST",
        path="/v1/invoices/49",
        body={"id": "I-9"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is True, decision.reason
    # The route keeps its port for the upstream connection.
    assert decision.route is not None
    assert decision.route.host == "api.example.com:8443"


def test_a_request_without_a_port_does_not_reach_a_port_bound_route(tmp_path: Path) -> None:
    """The port is part of the destination, so it cannot be dropped (#1342).

    The first pass at this fix dropped the port on both sides, which let a
    request on the listener's default port reach a route registered for
    ``:8443`` -- a different upstream. Case still folds: only the port has to
    agree exactly.
    """
    route = Route(
        host="api.example.com:8443",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )

    decision = match_route(
        [route],
        host="API.EXAMPLE.COM",  # no port, so not the :8443 destination
        method="POST",
        path="/v1/invoices/49",
        body={"id": "I-9"},
        actions_by_key={},
        run_id="run_1",
    )
    assert decision.allow is False
    assert "no upstream registered" in decision.reason


def test_same_host_different_ports_do_not_merge(tmp_path: Path) -> None:
    """Port-bound routes must not collapse into one candidate list.

    Dropping the route-side port merged ``a.com:8443`` into ``a.com``: both
    survived host selection, the prefix sort could not separate them when the
    prefixes agreed, and registry order picked the winner -- the selection bug
    #1341 closed, reintroduced with a different name. Each request must now
    reach the route its own port names.
    """
    default_port = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    other_port = Route(
        host="api.example.com:8443",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    routes = [default_port, other_port]

    for request_host, expected in (
        ("api.example.com", "api.example.com"),
        ("api.example.com:8443", "api.example.com:8443"),
    ):
        decision = match_route(
            routes,
            host=request_host,
            method="POST",
            path="/v1/invoices/49",
            body={"id": "I-9"},
            actions_by_key={},
            run_id="run_1",
        )
        # No claim is live, so the verdict is a claim refusal, not a routing
        # one: the route it names is the routing decision.
        assert decision.allow is False
        assert decision.route is not None
        assert decision.route.host == expected


def test_a_broad_route_cannot_settle_a_path_a_specific_route_owns_for_another_method(
    tmp_path: Path,
) -> None:
    """Method selection stays inside the most specific prefix.

    The prefix sort puts the narrower route first, but the method filter used to
    keep scanning: a narrower route that admitted the path but not the method
    was skipped, and a broader route's claim authorised the request. The
    narrower route governs the path once it wins the prefix race, so the
    request is refused with its methods rather than falling through to a claim
    less specific than the scope that now owns the path.
    """
    from continuum.actions.ledger import fold_action_events

    broad = Route(
        host="api.example.com",
        methods=("GET",),
        prefix="",  # whole host
        action_type="read_invoice",
        key_template="invoice:{id}",
    )
    specific = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ActionLedger(store, "run_1").claim("read_invoice", {"id": "I-9"}, key="invoice:I-9")
    actions = fold_action_events(store.read_events("run_1"))

    decision = match_route(
        [broad, specific],
        host="api.example.com",
        method="GET",
        path="/v1/invoices/49",
        body={"id": "I-9"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is False
    # Named for the route that owns the path, not the broad one it skipped to.
    assert "not among its allowed methods" in decision.reason


def test_two_routes_sharing_a_prefix_split_by_method_still_resolve(tmp_path: Path) -> None:
    """One route per method on the same prefix is one scope, not a collision.

    That is the natural way to express a resource family, and restricting
    method selection to the most specific prefix has to keep it working.
    """
    from continuum.actions.ledger import fold_action_events

    reader = Route(
        host="api.example.com",
        methods=("GET",),
        prefix="/v1/invoices",
        action_type="read_invoice",
        key_template="invoice:{id}",
    )
    writer = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="send_invoice",
        key_template="invoice:{id}",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ActionLedger(store, "run_1").claim("read_invoice", {"id": "I-9"}, key="invoice:I-9")
    actions = fold_action_events(store.read_events("run_1"))

    decision = match_route(
        [reader, writer],
        host="api.example.com",
        method="GET",
        path="/v1/invoices/49",
        body={"id": "I-9"},
        actions_by_key=actions,
        run_id="run_1",
    )
    assert decision.allow is True, decision.reason
    assert decision.route is not None
    assert decision.route.methods == ("GET",)


def test_colliding_upstreams_are_refused_at_config_load(tmp_path: Path) -> None:
    """Two routes the matcher cannot tell apart fail at load, not at request time."""
    cfg = tmp_path / "gateway.json"
    cfg.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "api.example.com",
                        "methods": ["POST"],
                        "prefix": "/v1/invoices",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{id}",
                    },
                    {
                        "host": "API.example.com:443",  # folds to the same name/port
                        "methods": ["POST"],
                        "prefix": "/v1/invoices/",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{id}",
                    },
                ]
            }
        )
    )
    with pytest.raises(GatewayConfigError) as excinfo:
        load_gateway_config(cfg)
    assert "repeats POST on prefix" in str(excinfo.value)


def test_method_refusal_names_every_method_in_the_winning_scope() -> None:
    """The refusal lists the winning prefix scope's methods, not one route's.

    Method selection covers every route sharing the winning prefix, so the
    refusal must name their union: with a GET route and a POST route on the
    same prefix, a PUT refusal naming only one of them depends on registry
    order, the dependence #1341 set out to remove.
    """
    get_route = Route(
        host="api.example.com",
        methods=("GET",),
        prefix="/v1/invoices",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:invoice",
    )
    post_route = Route(
        host="api.example.com",
        methods=("POST",),
        prefix="/v1/invoices",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:invoice",
    )
    for routes in ([get_route, post_route], [post_route, get_route]):
        decision = match_route(
            routes,
            host="api.example.com",
            method="PUT",
            path="/v1/invoices/9",
            body={},
            actions_by_key={},
            run_id="run_1",
        )
        assert decision.allow is False
        assert "'get'" in decision.reason and "'post'" in decision.reason


# --- oversized upstream replies (issue #1055) ----------------------------------- #
#
# The request side refuses a body over the cap before reading it, but the
# reply half used to buffer the whole upstream response with no limit at all:
# a hostile or merely broken upstream could then hold the agent's own proxy
# and memory hostage by sending a large body, which is the DoS
# docs/threat_model.md describes as fended off.


class _FakeResponse:
    """Stands in for ``http.client.HTTPResponse``.

    The gateway reads a reply through ``.length``, ``.getheader`` and
    ``.read``; faking those drives the whole response path without a
    reachable upstream, the same way the suite avoids real network egress.
    """

    def __init__(self, status: int, chunks: bytes, declared: int | None) -> None:
        self.status = status
        self.length = declared
        self._remaining = chunks

    def getheader(self, name: str, default: str | None = None) -> str | None:
        if name.lower() == "content-length":
            return None if self.length is None else str(self.length)
        return default

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out, self._remaining = self._remaining, b""
            return out
        out, self._remaining = self._remaining[:n], self._remaining[n:]
        return out


class _FakeConnection:
    """Answers one request with a canned response, then closes."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def request(self, *args: object, **kwargs: object) -> None:
        pass

    def getresponse(self) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        pass


@pytest.fixture
def fake_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[_FakeResponse], _FakeConnection]:
    """Point the gateway's HTTPS client at a canned reply instead of the network.

    The cap is shrunk to 1 KiB so the refusal is exercised without allocating
    megabytes per attempt: the bound is what the test pins, not its size.
    """
    monkeypatch.setattr("continuum.gateway.MAX_RESPONSE_BYTES", 1024)

    def install(response: _FakeResponse) -> _FakeConnection:
        conn = _FakeConnection(response)
        monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **k: conn)
        return conn

    return install


def test_an_oversized_declared_reply_is_refused_with_502(
    db: str, gateway: str, fake_upstream: Callable[[_FakeResponse], _FakeConnection]
) -> None:
    """A declared Content-Length over the cap is refused before it is read."""
    key = claim(db, "invoice:I-50")
    fake_upstream(_FakeResponse(200, b"{}", declared=10 * 1024 * 1024))
    status, body = post(gateway, "/v1/invoices", {"id": "I-50"})
    assert status == 502
    assert "upstream response too large" in body["error"]

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        action = fold_action_events(store.read_events("run_1"))[key]
    # The upstream may well have applied the side effect, so the claim lands
    # uncertain rather than completed -- the honest answer for a reply we
    # could not read.
    assert action.side_effect_uncertain is True


def test_an_oversized_undeclared_reply_is_refused_mid_stream(
    db: str, gateway: str, fake_upstream: Callable[[_FakeResponse], _FakeConnection]
) -> None:
    """A reply that declares no length is bounded by what is actually read.

    ``.length`` is None for a chunked reply, so the declared check cannot
    fire; the running total has to catch it instead, or this path buffers
    the whole upstream with no bound at all.
    """
    claim(db, "invoice:I-51")
    fake_upstream(_FakeResponse(200, b"x" * 8192, declared=None))
    status, body = post(gateway, "/v1/invoices", {"id": "I-51"})
    assert status == 502
    assert "upstream response too large" in body["error"]


def test_a_reply_within_the_cap_is_forwarded_verbatim(
    db: str, gateway: str, fake_upstream: Callable[[_FakeResponse], _FakeConnection]
) -> None:
    key = claim(db, "invoice:I-52")
    payload = json.dumps({"ok": True, "id": "I-52"}).encode()
    fake_upstream(_FakeResponse(200, payload, declared=len(payload)))
    conn = http.client.HTTPConnection(gateway, timeout=10)
    conn.request(
        "POST",
        "/v1/invoices",
        body=json.dumps({"id": "I-52"}),
        headers={"Host": "api.example.com", "Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    echoed = resp.read()
    conn.close()
    assert resp.status == 200
    assert echoed == payload
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        action = fold_action_events(store.read_events("run_1"))[key]
    assert action.status is ActionStatus.COMPLETED


# --- A plain-HTTP upstream, reached through the real stack -----------------
#
# The canned connections above replace ``http.client``'s connection classes,
# which is the one seam that knows which transport the gateway opened. A route
# whose upstream is plain HTTP is only really exercised against a server a
# socket can talk to, so these spin one up and drive the whole path: the
# request leaves the proxy, the upstream answers, and the claim settles from
# the reply.


@pytest.fixture
def http_upstream() -> Generator[tuple[str, list[bytes]], None, None]:
    """A live plain-HTTP upstream on an ephemeral port.

    Yields ``(origin, received)``: the ``http://host:port`` the registry names
    and the list of bodies it was handed, so a test can prove the request
    really arrived rather than only that the gateway believes it forwarded one.
    """
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            """The access log is noise here; the event log is the record."""
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(body)
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", received
    server.shutdown()


@contextmanager
def _gateway_for(
    db_path: str, run_id: str, routes: list[Route], tmp_path: Path
) -> Generator[str, None, None]:
    """A live gateway on an ephemeral port serving ``routes`` for ``run_id``."""
    server = GatewayServer(lambda: SQLiteStorage(db_path), run_id, routes, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.port}"
    server.shutdown()


def test_a_plain_http_upstream_is_reached_and_settles_the_claim(
    db: str, tmp_path: Path, http_upstream: tuple[str, list[bytes]]
) -> None:
    """An http:// upstream is forwarded to over plain HTTP and settled (#7-fix).

    The transport was hardcoded to https, so a plain-HTTP upstream -- a local
    service, an internal address behind a TLS terminator -- was unreachable at
    all: the handshake to its cleartext port failed as WRONG_VERSION_NUMBER
    before a byte was forwarded, the claim settled UNKNOWN, and no evidence was
    recorded. The canned connections hide that, because they replace the very
    class that would have failed.
    """
    origin, received = http_upstream
    routes = load_gateway_config(_registry(tmp_path, origin))
    assert routes[0].scheme == "http"
    assert routes[0].host == origin[len("http://") :]

    key = claim(db, "invoice:I-60")
    with _gateway_for(db, "run_1", routes, tmp_path) as addr:
        status, body = post(addr, "/v1/invoices", {"id": "I-60"}, host=routes[0].host)
    assert status == 200
    assert body == {"ok": True}
    assert received == [b'{"id": "I-60"}']

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        action = fold_action_events(store.read_events("run_1"))[key]
        evidence = [e for e in store.read_events("run_1") if e.type is EventType.TOOL_COMPLETED]
    assert action.status is ActionStatus.COMPLETED
    assert action.side_effect_uncertain is False
    # The recorded path names the scheme the request actually travelled, so
    # the evidence does not claim TLS a plain-HTTP upstream never used.
    assert [e.payload["path"] for e in evidence] == [f"{origin}/v1/invoices"]


def test_a_route_without_a_scheme_still_means_https(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry written before schemes existed keeps its https meaning.

    Schemes are additive: the default has to stay https or every existing
    ``gateway.json`` silently changes which transport it dials.
    """
    routes = load_gateway_config(Path(config_file(tmp_path)))
    assert routes[0].scheme == "https"

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(http.client, "HTTPSConnection", _RecordingConn("https", opened))
    monkeypatch.setattr(http.client, "HTTPConnection", _RecordingConn("http", opened))

    claim(db, "invoice:I-61")
    with _gateway_for(db, "run_1", routes, tmp_path) as addr:
        _post_to_gateway(addr, "/v1/invoices", {"id": "I-61"})
    # The request the test sends also travels a patched HTTPConnection to the
    # gateway itself, so the netloc is what distinguishes the two hops.
    assert ("https", "api.example.com") in opened
    assert ("http", "api.example.com") not in opened


def test_an_http_route_opens_a_plain_connection(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's scheme selects the connection class, not the hardcoded one."""
    routes = load_gateway_config(_registry(tmp_path, "http://api.example.com"))
    assert routes[0].host == "api.example.com"

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(http.client, "HTTPSConnection", _RecordingConn("https", opened))
    monkeypatch.setattr(http.client, "HTTPConnection", _RecordingConn("http", opened))

    claim(db, "invoice:I-62")
    with _gateway_for(db, "run_1", routes, tmp_path) as addr:
        status, _ = _post_to_gateway(addr, "/v1/invoices", {"id": "I-62"})
    assert status == 200
    assert ("http", "api.example.com") in opened
    assert ("https", "api.example.com") not in opened


def test_an_explicit_scheme_key_selects_plain_http(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``scheme`` field reaches the same upstream without a URL prefix."""
    p = tmp_path / "gateway.json"
    p.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": "api.example.com",
                        "scheme": "http",
                        "methods": ["POST"],
                        "prefix": "/v1/invoices",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{id}",
                    }
                ]
            }
        )
    )
    routes = load_gateway_config(p)
    assert routes[0].scheme == "http"

    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(http.client, "HTTPSConnection", _RecordingConn("https", opened))
    monkeypatch.setattr(http.client, "HTTPConnection", _RecordingConn("http", opened))

    claim(db, "invoice:I-63")
    with _gateway_for(db, "run_1", routes, tmp_path) as addr:
        _post_to_gateway(addr, "/v1/invoices", {"id": "I-63"})
    assert ("http", "api.example.com") in opened


def _registry(tmp_path: Path, origin: str) -> Path:
    """Write a one-upstream registry whose host is ``origin``."""
    p = tmp_path / "gateway.json"
    p.write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "host": origin,
                        "methods": ["POST"],
                        "prefix": "/v1/invoices",
                        "action_type": "send_invoice",
                        "key_template": "invoice:{id}",
                    }
                ]
            }
        )
    )
    return p


class _RecordingConn:
    """Records which connection class the gateway opened and to whom.

    The canned connection the rest of the suite uses replaces one class and
    cannot say which one was chosen, which is exactly the question a scheme
    raises. Recording the netloc as well matters because the request the test
    itself sends reaches the gateway over a patched ``HTTPConnection`` too --
    so the client's own connection to ``127.0.0.1:<port>`` is recorded
    alongside the gateway's to the upstream, and only the netloc tells them
    apart.
    """

    def __init__(self, name: str, opened: list[tuple[str, str]]) -> None:
        self._name = name
        self._opened = opened

    def __call__(self, netloc: str, *args: object, **kwargs: object) -> _RecordingConn._Conn:
        self._opened.append((self._name, netloc))
        return self._Conn()

    class _Conn:
        def request(self, *args: object, **kwargs: object) -> None:
            pass

        def getresponse(self) -> _FakeResponse:
            return _FakeResponse(200, b"{}", declared=2)

        def close(self) -> None:
            pass


def test_an_http_route_matches_a_client_spelling_its_default_port(
    tmp_path: Path,
) -> None:
    """``Host: a.com:80`` reaches an ``http://a.com`` route (#1342's other half).

    The route side folds its own scheme's default port; the request side cannot
    know which scheme its route will turn out to have, so it folds either --
    or a client that writes the default port explicitly is refused as
    unregistered for spelling a port the scheme does not consider one.
    """
    from continuum.gateway import _normalize_request_host

    assert _normalize_request_host("a.com:80") == ("a.com", None)
    assert _normalize_request_host("a.com:443") == ("a.com", None)
    assert _normalize_request_host("a.com:8080") == ("a.com", "8080")

    route = Route(
        host="a.com",
        methods=("POST",),
        prefix="/",
        action_type="send_invoice",
        key_template="invoice:{id}",
        scheme="http",
    )
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    ActionLedger(store, "run_1").claim("send_invoice", {"id": "I-64"}, key="invoice:I-64")
    from continuum.actions.ledger import fold_action_events

    actions = fold_action_events(store.read_events("run_1"))
    for host in ("a.com", "a.com:80"):
        decision = match_route(
            [route],
            host=host,
            method="POST",
            path="/",
            body={"id": "I-64"},
            actions_by_key=actions,
            run_id="run_1",
        )
        assert decision.allow, host
