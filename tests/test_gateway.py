"""The enforcing HTTP gateway (seam 4).

A local proxy that refuses unclaimed outbound requests to registered
upstreams and settles claims from real upstream responses. Tested against a
live upstream server on an ephemeral port, through the actual HTTP stack.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.gateway import (
    Decision,
    GatewayConfigError,
    GatewayServer,
    Route,
    _path_under_prefix,
    _resolved_request_path,
    _wire_request_target,
    load_gateway_config,
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


# Two operations on one host and method, differing only in prefix and key
# template. ``/v1/refunds`` is listed first on purpose: a gateway that takes
# the first host-and-method route and checks only its prefix would refuse a
# perfectly good invoice request, and would render the wrong key for a refund.
MULTI_ROUTES = [
    {
        "host": "api.example.com",
        "methods": ["POST"],
        "prefix": "/v1/refunds",
        "action_type": "send_refund",
        "key_template": "refund:{id}",
    },
    {
        "host": "api.example.com",
        "methods": ["POST"],
        "prefix": "/v1/invoices",
        "action_type": "send_invoice",
        "key_template": "invoice:{id}",
    },
]


@pytest.fixture
def gateway_multi(db: str, tmp_path: Path):
    """A live gateway serving two prefixes on one host and method."""
    path = tmp_path / "gateway_multi.json"
    path.write_text(json.dumps({"upstreams": MULTI_ROUTES}))
    cfg = load_gateway_config(path)
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


@contextlib.contextmanager
def _captured_forwarding(*, fail_with: BaseException | None = None) -> Iterator[dict[str, str]]:
    """Patch the gateway's upstream connection, capturing the request line.

    The real upstream is unreachable from CI, so a request that gets as far as
    forwarding needs a stand-in that records what was sent instead of opening a
    socket. ``fail_with`` makes that stand-in fail at request time, the way an
    unparseable target or a dropped connection would.
    """
    forwarded: dict[str, str] = {}

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b"{}"

        def getheader(self, name: str, default: str | None = None) -> str | None:
            return default

    class _FakeConn:
        def __init__(self, netloc: str, **kw: object) -> None:
            forwarded["netloc"] = netloc

        def request(self, method: str, url: str, **kw: object) -> None:
            forwarded["method"] = method
            forwarded["url"] = url
            if fail_with is not None:
                raise fail_with

        def getresponse(self) -> _FakeResponse:
            return _FakeResponse()

        def close(self) -> None:
            pass

    original = http.client.HTTPSConnection
    http.client.HTTPSConnection = _FakeConn  # type: ignore[assignment]
    try:
        yield forwarded
    finally:
        http.client.HTTPSConnection = original  # type: ignore[assignment]


def test_the_forwarded_request_line_carries_the_resolved_path(db: str, gateway: str) -> None:
    """The upstream receives the resolved path, not the raw one (#1051).

    ``/v1/invoices/sub/../I-2`` is an in-prefix path, so the claim is spent
    legitimately, but the raw form is what an upstream or an intermediary proxy
    may then normalize. Forwarding the raw path would let the upstream resolve
    a path the gate never measured, so the resolved form is what is sent.
    """
    claim(db, "invoice:I-2")

    with _captured_forwarding() as forwarded:
        status, _body = post(gateway, "/v1/invoices/sub/../I-2", {"id": "I-2"})

    assert status == 200, _body
    # The traversal was resolved before forwarding and before recording.
    assert forwarded["url"] == "/v1/invoices/I-2", forwarded

    with SQLiteStorage(db) as store:
        events = [
            e
            for e in store.read_events("run_1")
            if e.type is EventType.TOOL_COMPLETED and e.payload.get("via") == "gateway"
        ]
    assert events, "a forwarded call must be recorded as evidence"
    recorded = events[0].payload["path"]
    assert recorded == "https://api.example.com/v1/invoices/I-2", recorded


def test_the_query_string_survives_to_the_request_line(db: str, gateway: str) -> None:
    """The scope check strips the query; the wire form restores it (#1051).

    A query is no part of a route's prefix, so it can neither satisfy nor
    violate the scope, but the caller sent it and the upstream still needs it.
    Resolving the path drops it, so it is re-attached from the raw request
    rather than silently altering the call.
    """
    claim(db, "invoice:I-2")

    with _captured_forwarding() as forwarded:
        status, _body = post(gateway, "/v1/invoices/sub/../I-2?since=2026-01-01", {"id": "I-2"})

    assert status == 200, _body
    assert forwarded["url"] == "/v1/invoices/I-2?since=2026-01-01", forwarded


def test_a_decoded_character_is_re_encoded_on_the_wire(db: str, gateway: str) -> None:
    """A decoded space is still one segment, but a broken request line.

    Resolving turns ``%20`` into a space, which would end the request target
    early on the wire, so the resolved path is percent-encoded again before it
    is sent and the upstream receives the same segment the gate measured.
    """
    claim(db, "invoice:I-7")

    with _captured_forwarding() as forwarded:
        status, _body = post(gateway, "/v1/invoices/I%202", {"id": "I-7"})

    assert status == 200, _body
    assert forwarded["url"] == "/v1/invoices/I%202", forwarded


def test_an_unusable_request_target_fails_the_claim_certainly(db: str, gateway: str) -> None:
    """A target the transport rejects is a 502 and a certain failure (#1051).

    ``http.client.InvalidURL`` is raised before the request line is written, so
    nothing was sent and the effect cannot have landed, unlike a connection
    that drops mid-flight. It is not an ``OSError`` subclass, so catching
    ``OSError`` alone would let it escape the handler as an uncaught 500.
    """
    key = claim(db, "invoice:I-8")

    with _captured_forwarding(fail_with=http.client.InvalidURL("unparseable target")):
        status, body = post(gateway, "/v1/invoices/I-8", {"id": "I-8"})

    assert status == 502, body
    assert "unparseable target" in body["detail"], body

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    assert folded[key].status is ActionStatus.FAILED, folded[key].status


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


def test_off_prefix_path_is_refused_fail_closed(db: str, gateway: str) -> None:
    """A claim scoped to /v1/invoices must not be spendable on /v1/refunds (#1051).

    The prefix is a route's only per-path scope: refusing here, before the key
    is rendered, is what keeps an invoice claim from settling as completed
    against evidence that records a refund URL.
    """
    key = claim(db, "invoice:I-3")
    status, body = post(gateway, "/v1/refunds", {"id": "I-3"})
    assert status == 403
    assert "outside it" in body["reason"]
    assert "/v1/invoices" in body["reason"]

    # The claim is untouched: nothing was consumed or settled by the attempt.
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    assert folded[key].status is ActionStatus.STARTED


def test_off_prefix_path_with_query_string_smuggling_is_refused(db: str, gateway: str) -> None:
    """The query string is not part of the prefix scope, so it cannot satisfy it."""
    claim(db, "invoice:I-4")
    status, body = post(gateway, "/v1/refunds?x=/v1/invoices", {"id": "I-4"})
    assert status == 403
    assert "outside it" in body["reason"]


def test_a_path_sharing_the_prefix_spelling_is_refused(db: str, gateway: str) -> None:
    """/v1/invoices-archive is a different segment, not a longer invoice path."""
    claim(db, "invoice:I-5")
    status, body = post(gateway, "/v1/invoices-archive", {"id": "I-5"})
    assert status == 403
    assert "outside it" in body["reason"]


def test_off_prefix_path_with_dot_segments_is_refused(db: str, gateway: str) -> None:
    """``/v1/invoices/../refunds`` reads as an invoices path but is not one.

    The request is forwarded as sent, so an upstream is free to resolve the
    dot segments and serve ``/v1/refunds``. Comparing the raw path would wave
    exactly that through, so the check resolves dot segments first (#1051).
    """
    key = claim(db, "invoice:I-30")
    status, body = post(gateway, "/v1/invoices/../refunds", {"id": "I-30"})
    assert status == 403
    assert "outside it" in body["reason"]

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    assert folded[key].status is ActionStatus.STARTED


def test_off_prefix_path_with_percent_encoded_traversal_is_refused(db: str, gateway: str) -> None:
    """``%2f`` decodes to a separator, so it cannot smuggle a traversal either.

    ``/v1/invoices/..%2frefunds`` is past the segment boundary, so only the
    decoding and the dot-segment resolution expose it as a refund path.
    """
    key = claim(db, "invoice:I-31")
    status, body = post(gateway, "/v1/invoices/..%2frefunds", {"id": "I-31"})
    assert status == 403
    assert "outside it" in body["reason"]

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    assert folded[key].status is ActionStatus.STARTED


def test_a_path_under_the_prefix_is_in_scope(db: str, gateway: str) -> None:
    """Prefix matching, not exact matching: /v1/invoices/I-1 is an invoice path.

    api.example.com is unreachable from CI, so the request dies at the network
    with 502, but that is past the prefix check, which is what this pins. An
    off-prefix path returns 403 long before forwarding.
    """
    claim(db, "invoice:I-6")
    status, _body = post(gateway, "/v1/invoices/I-6", {"id": "I-6"})
    assert status != 403


@pytest.mark.parametrize(
    ("path", "prefix", "expected"),
    [
        # Outside and inside the prefix, at the segment boundary.
        ("/v1/refunds", "/v1/invoices", False),
        ("/v1/invoices", "/v1/invoices", True),
        ("/v1/invoices/I-1", "/v1/invoices", True),
        ("/v1/invoices-archive", "/v1/invoices", False),
        # A trailing slash on either side is the same segment boundary.
        ("/v1/invoices/", "/v1/invoices/", True),
        ("/v1/invoices", "/v1/invoices/", True),
        # The query string is not part of the scope.
        ("/v1/refunds?x=/v1/invoices", "/v1/invoices", False),
        ("/v1/invoices?x=/v1/refunds", "/v1/invoices", True),
        # Traversal and encoding are resolved before the comparison.
        ("/v1/invoices/../refunds", "/v1/invoices", False),
        ("/v1/invoices/..%2frefunds", "/v1/invoices", False),
        ("/v1/invoices/sub/../I-1", "/v1/invoices", True),
        # Decoding runs to a fixed point: the gateway forwards the path as
        # sent, and an upstream or proxy that decodes twice would resolve
        # these to /v1/refunds after the claim was approved.
        ("/v1/invoices/..%252frefunds", "/v1/invoices", False),
        ("/v1/invoices/..%25252frefunds", "/v1/invoices", False),
        ("/v1/invoices/%252e%252e/refunds", "/v1/invoices", False),
        # An empty prefix or "/" is the registry's whole-host default.
        ("/anything", "/", True),
        ("/anything", "", True),
        ("/", "/", True),
        # A missing leading slash is repaired on both sides, not treated as
        # outside the scope.
        ("v1/invoices/I-1", "/v1/invoices", True),
        ("/v1/invoices", "v1/invoices", True),
    ],
)
def test_path_under_prefix_holds_at_the_boundaries(path: str, prefix: str, expected: bool) -> None:
    """The scope rule, checked directly so the edge cases do not need a socket."""
    assert _path_under_prefix(path, prefix) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The query string survives as sent but never reaches the scope check.
        ("/v1/invoices?x=1", "/v1/invoices"),
        # Dot segments, single and repeated decoding, and a bare host root.
        ("/v1/invoices/sub/../I-1", "/v1/invoices/I-1"),
        ("/v1/invoices/..%2frefunds", "/v1/refunds"),
        ("/v1/invoices/..%252frefunds", "/v1/refunds"),
        ("/v1/./invoices/", "/v1/invoices"),
        ("", "/"),
        ("/", "/"),
    ],
)
def test_resolved_request_path_is_the_canonical_form(path: str, expected: str) -> None:
    """The path the gate checks is the path it forwards and records (#1051).

    A normalizing upstream must resolve the same path the prefix check
    measured, so resolution happens once here and is reused for both.
    """
    assert _resolved_request_path(path) == expected


@pytest.mark.parametrize(
    ("resolved", "raw", "expected"),
    [
        # A path already in canonical form is carried as sent.
        ("/v1/invoices/I-1", "/v1/invoices/I-1", "/v1/invoices/I-1"),
        # The query the scope check stripped is restored from the raw request.
        (
            "/v1/invoices/I-1",
            "/v1/invoices/I-1?since=2026-01-01",
            "/v1/invoices/I-1?since=2026-01-01",
        ),
        # A query that spells an in-prefix path cannot leak into the target.
        ("/v1/refunds", "/v1/refunds?x=/v1/invoices", "/v1/refunds?x=/v1/invoices"),
        # A decoded character that would break the request line is re-encoded.
        ("/v1/invoices/I 2", "/v1/invoices/I%202", "/v1/invoices/I%202"),
    ],
)
def test_wire_request_target_carries_the_measured_path(
    resolved: str, raw: str, expected: str
) -> None:
    """The request line carries the path the gate measured, in transport form.

    ``resolved`` is what the prefix check saw and what the evidence records;
    the wire target is that same path, percent-encoded again so a decoded
    character cannot end the target early, with the query re-attached (#1051).
    """
    assert _wire_request_target(resolved, raw) == expected


def test_a_request_for_a_later_configured_prefix_is_not_measured_against_the_first(
    db: str, gateway_multi: str
) -> None:
    """/v1/invoices is a registered operation even though it is listed second.

    Without prefix-aware selection the gateway takes the refunds route, renders
    ``refund:I-40`` against an invoice claim, and answers with claim
    instructions for an operation the caller never named.
    """
    claim(db, "invoice:I-40")
    status, _body = post(gateway_multi, "/v1/invoices", {"id": "I-40"})
    # Past route selection and past the claim check. The upstream is
    # unreachable, so the request dies at the network, not at the gate.
    assert status != 403


def test_an_off_prefix_path_is_refused_across_all_configured_prefixes(
    db: str, gateway_multi: str
) -> None:
    """A path under no configured prefix can spend none of the claims."""
    key = claim(db, "invoice:I-41")
    status, body = post(gateway_multi, "/internal/admin/purge", {"id": "I-41"})
    assert status == 403
    assert "outside all of them" in body["reason"]
    assert "/v1/invoices" in body["reason"]
    assert "/v1/refunds" in body["reason"]

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    assert folded[key].status is ActionStatus.STARTED


def test_a_claim_cannot_be_spent_on_another_registered_operation(
    db: str, gateway_multi: str
) -> None:
    """/v1/refunds is registered, so it is not off-prefix, but it is not free.

    Selecting the refunds route renders ``refund:I-42``, which the invoice
    claim never covered, so the call is refused on the claim rather than on the
    prefix. Either way the invoice claim is not spent.
    """
    key = claim(db, "invoice:I-42")
    status, body = post(gateway_multi, "/v1/refunds", {"id": "I-42"})
    assert status == 403
    assert "no ledger claim" in body["reason"]
    assert "refund:I-42" in body["reason"]

    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    assert folded[key].status is ActionStatus.STARTED


# Overlapping prefixes on one host and method. ``/v1`` is listed first on
# purpose: ``/v1/invoices/I-43`` is under both, and a selection that keeps
# config order would render the broad route's key template and measure an
# invoice claim against it.
OVERLAP_ROUTES = [
    {
        "host": "api.example.com",
        "methods": ["POST"],
        "prefix": "/v1",
        "action_type": "broad",
        "key_template": "broad:{id}",
    },
    {
        "host": "api.example.com",
        "methods": ["POST"],
        "prefix": "/v1/invoices",
        "action_type": "send_invoice",
        "key_template": "invoice:{id}",
    },
]


@pytest.fixture
def gateway_overlap(db: str, tmp_path: Path):
    """A live gateway serving nested prefixes on one host and method."""
    path = tmp_path / "gateway_overlap.json"
    path.write_text(json.dumps({"upstreams": OVERLAP_ROUTES}))
    cfg = load_gateway_config(path)
    server = GatewayServer(lambda: SQLiteStorage(db), "run_1", cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.port}"
    server.shutdown()


def test_an_overlapping_prefix_selects_the_most_specific_route(
    db: str, gateway_overlap: str
) -> None:
    """A path under two prefixes is measured against the longer one.

    ``/v1/invoices/I-43`` is under both ``/v1`` and ``/v1/invoices``. The
    specific route renders ``invoice:I-43``, which the invoice claim covers,
    so the request is forwarded; the broad route's template would render
    ``broad:I-43`` and the call would be refused on the claim. Whichever
    route was configured first must not change which key is rendered.
    """
    claim(db, "invoice:I-43")
    status, body = post(gateway_overlap, "/v1/invoices/I-43", {"id": "I-43"})
    assert status != 403, body


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
