"""HTTP transport over the sidecar dispatch (issue #238).

Non-Python agents get the durability plane by POSTing JSON to
`continuum serve --transport http`. Same handlers, same token auth, errors
mapped to status codes - proven here against a live server thread.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from continuum.serve import server as serve_module
from continuum.serve.server import MAX_SIDECAR_BODY_BYTES, SidecarHTTP, SidecarServer
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "http.db")


_PORT_COUNTER = {"n": 9300}


@pytest.fixture
def server(db: str, monkeypatch: pytest.MonkeyPatch):
    # Auth enabled: the HTTP surface is reachable by any local process, so
    # tests pin the fail-closed behaviour explicitly. Unique ports prevent a
    # lingering listener from a previous test answering first.
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "test-secret")
    _PORT_COUNTER["n"] += 1
    sidecar = SidecarServer(database=db)
    http = SidecarHTTP(sidecar, port=_PORT_COUNTER["n"])
    import threading

    t = threading.Thread(target=http.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{http.port}"
    http.shutdown()


TOKEN = "test-secret"


def post(
    addr: str, method: str, params: dict[str, object] | None = None, token: str | None = None
) -> tuple[int, dict[str, object]]:
    params = dict(params or {})
    if token:
        params["auth_token"] = token
    conn_addr = addr
    req = urllib.request.Request(
        f"http://{conn_addr}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def raw_post(addr: str, request_line_and_headers: str, body: bytes = b"") -> tuple[int, bytes]:
    """Send exact bytes and return ``(status, whole response)``.

    urllib rewrites Content-Length and frames the body itself, so the malformed
    and chunked cases have to be spoken on the socket. Every request asks for
    ``Connection: close`` and half-closes after writing, so the response can be
    read to EOF and a handler that answers nothing is visible as an empty read
    rather than a hang.
    """
    host, _, port = addr.partition(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(request_line_and_headers.encode() + body)
        sock.shutdown(socket.SHUT_WR)
        received = b""
        while True:
            data = sock.recv(65536)
            if not data:
                break
            received += data
    if not received.startswith(b"HTTP/"):
        return 0, received
    return int(received.split(b" ", 2)[1]), received


_STATUS_LINE = re.compile(rb"HTTP/1\.[01] (\d{3})")


def response_statuses(raw: bytes) -> list[int]:
    """Status code of every response in one read, in the order they arrived.

    Scanned rather than split on lines: a response body is framed by
    Content-Length with no trailing CRLF, so a second status line starts
    immediately after the first body and would not begin a line of its own.
    Missing it is the one way this helper could report a smuggled request as
    absent when it was answered.
    """
    return [int(code) for code in _STATUS_LINE.findall(raw)]


def raw_pipeline(addr: str, wire: bytes) -> bytes:
    """Send exact bytes on one connection and read everything that comes back.

    No request here asks for ``Connection: close``: the point is what the
    handler leaves behind on a keep-alive socket, which is the default under
    ``protocol_version = "HTTP/1.1"``. Writing is half-closed so a handler that
    keeps the connection still ends the read instead of hanging.
    """
    host, _, port = addr.partition(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(wire)
        sock.shutdown(socket.SHUT_WR)
        received = b""
        while True:
            try:
                data = sock.recv(65536)
            except ConnectionResetError:
                # A server that closes while bytes it never read are still in
                # its receive queue resets instead of sending FIN. The response
                # is already buffered here when that happens, so this ends the
                # read rather than losing it; an empty read still fails the
                # assertions below rather than passing quietly.
                break
            if not data:
                break
            received += data
    return received


def pipelined_post(run_id: str) -> bytes:
    """A complete, valid, authenticated request to append after another one.

    Placed where a refused body's unread remainder would sit, this is the
    smuggled request: if the handler keeps the connection, these bytes are
    parsed as the next request and `run_id` reaches the database.
    """
    body = json.dumps(
        {
            "run_id": run_id,
            "completed": 1,
            "total": 1,
            "goal": "pipelined behind a refusal",
            "auth_token": TOKEN,
        }
    ).encode()
    return (
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


# --- auth ------------------------------------------------------------------------ #


def test_requests_require_the_shared_secret(server) -> None:
    status, body = post(
        server, "record_progress", {"run_id": "r", "completed": 0, "total": 1}, token=None
    )
    assert status == 403
    assert "secret" in body["error"].lower()


def test_wrong_secret_is_refused(server) -> None:
    status, _ = post(
        server, "record_progress", {"run_id": "r", "completed": 0, "total": 1}, token="wrong"
    )
    assert status == 403


# --- dispatch --------------------------------------------------------------------- #


def test_unknown_method_maps_to_404(server) -> None:
    status, body = post(server, "teleport", {}, token=TOKEN)
    assert status == 404


def test_bad_params_map_to_400(server) -> None:
    status, body = post(server, "record_progress", {"nonsense": True}, token=TOKEN)
    print("BADPARAMS:", status, body)
    assert status == 400
    assert "error" in body


# --- request shape (issue #582) ---------------------------------------------------- #


def post_body(addr: str, method: str, body: bytes) -> tuple[int, dict[str, object]]:
    """POST one exact body, so a test can send what ``post`` cannot encode."""
    req = urllib.request.Request(
        f"http://{addr}/{method}", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


@pytest.mark.parametrize("body", (b"[]", b"null", b"5", b'"resume"', b"true", b'["resume"]'))
def test_a_non_object_body_is_answered_not_dropped(server, body: bytes) -> None:
    """A body that parses but is not an object is a 400 (issue #582).

    ``do_POST`` already spelled out this refusal, but raised it inside a ``try``
    that caught only ``json.JSONDecodeError``: ``BadParams`` is not one, so it
    propagated out of the handler and the connection closed with no response at
    all. The caller saw ``RemoteDisconnected``, which is what a crashed sidecar
    looks like, three lines below code that knew the answer was 400.

    No token is sent and the answer is still 400 rather than 403: a body that is
    not a request cannot carry credentials to check, and the message is a fixed
    string about the protocol, so refusing on shape first discloses nothing.
    """
    status, payload = post_body(server, "resume", body)
    assert status == 400, payload
    assert payload["error"] == "body must be a JSON object"


def test_a_malformed_body_keeps_its_own_answer(server) -> None:
    """The framing answer #582 reuses, pinned so the shared check cannot move it.

    A body no parser can read keeps the decoder's message; only the shape case
    gets the new one. Same status, different text, which is what lets a client
    tell "you sent me bytes I cannot parse" from "you parsed, but that is not a
    request".
    """
    status, payload = post_body(server, "resume", b"not json at all")
    assert status == 400, payload
    assert "invalid JSON body" in str(payload["error"])


def test_the_server_keeps_serving_after_a_non_object_body(server) -> None:
    """The refusal must cost the caller nothing beyond that one request."""
    assert post_body(server, "resume", b"[]")[0] == 400

    status, body = post(
        server,
        "record_progress",
        {"run_id": "after-bad-shape", "completed": 1, "total": 2, "goal": "still up"},
        token=TOKEN,
    )
    assert status == 200, body
    assert body["completed"] == 1


def test_storage_errors_map_to_500_without_killing_the_server(server) -> None:
    status, body = post(server, "resume", {"run_id": "ghost"}, token=TOKEN)
    assert status == 500
    assert "ghost" in body["error"]
    # Server still alive afterwards.
    status2, body2 = post(server, "resume", {"run_id": "ghost"}, token=TOKEN)
    assert status2 == 500
    assert "ghost" in body2["error"]


def test_record_progress_round_trip_persists_events(server, db: str) -> None:
    status, body = post(
        server,
        "record_progress",
        {"run_id": "http-run", "completed": 1, "total": 3, "goal": "served over http"},
        token=TOKEN,
    )
    assert status == 200
    assert body["completed"] == 1

    with SQLiteStorage(db) as store:
        events = store.read_events("http-run")
    types = [e.type.value for e in events]
    assert "RUN_STARTED" in types and "TASK_UPDATED" in types


# --- body framing (issue #533) ----------------------------------------------------- #
#
# The two ways a caller could previously get past the body layer: a
# Content-Length that int() refused, which escaped the handler and closed the
# connection with no response at all, and a chunked body, which was read as
# length 0 and dispatched as if empty while the stream itself went unbounded.


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "0x10", " ", "12abc"])
def test_malformed_content_length_is_answered_not_dropped(server, value: str) -> None:
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {value}\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"{}",
    )
    assert status == 400, f"no HTTP response for Content-Length: {value!r}"
    assert b"invalid Content-Length" in raw


def test_the_server_survives_a_malformed_content_length(server) -> None:
    raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Length: abc\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"{}",
    )
    status, body = post(
        server,
        "record_progress",
        {"run_id": "after-malformed", "completed": 1, "total": 2, "goal": "still serving"},
        token=TOKEN,
    )
    assert status == 200
    assert body["completed"] == 1


@pytest.mark.parametrize("value", ["chunked", "Chunked", "gzip, chunked", "chunked "])
def test_chunked_body_is_refused_not_read_as_empty(server, value: str) -> None:
    # Without Content-Length the old handler read length 0, dispatched an empty
    # body, and refused it for the missing secret with 403 - while the chunked
    # stream itself was never bounded. A 403 here means the bypass is back.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        f"Transfer-Encoding: {value}\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"5\r\nhello\r\n0\r\n\r\n",
    )
    assert status == 400, f"chunked body not refused for Transfer-Encoding: {value!r}"
    assert b"chunked Transfer-Encoding is not supported" in raw


def test_malformed_chunk_framing_is_a_400(server) -> None:
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"zz\r\nhello\r\n0\r\n\r\n",
    )
    assert status == 400
    assert b"invalid chunked Transfer-Encoding" in raw


def test_oversize_content_length_is_refused_before_the_body_is_read(server) -> None:
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        f"Content-Length: {MAX_SIDECAR_BODY_BYTES + 1}\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"x",  # the cap is judged from the header, not from what arrives
    )
    assert status == 413
    assert str(MAX_SIDECAR_BODY_BYTES).encode() in raw


def test_a_body_at_the_cap_still_reaches_dispatch(server) -> None:
    # One byte under the cap is not refused: the goal string is padded so the
    # request is large but legal, and it round-trips.
    goal = "x" * 4096
    status, body = post(
        server,
        "record_progress",
        {"run_id": "big-but-legal", "completed": 2, "total": 4, "goal": goal},
        token=TOKEN,
    )
    assert status == 200
    assert body["completed"] == 2


def test_a_small_body_still_round_trips_on_the_socket(server) -> None:
    payload = json.dumps(
        {
            "run_id": "raw-run",
            "completed": 1,
            "total": 1,
            "goal": "spoken on the socket",
            "auth_token": TOKEN,
        }
    ).encode()
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n",
        payload,
    )
    assert status == 200
    assert b'"completed": 1' in raw


def test_no_content_length_at_all_still_dispatches_an_empty_body(server) -> None:
    # The happy path for a method called with no body is unchanged: it reaches
    # dispatch and is refused for the missing secret, not for its framing.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    assert status == 403
    assert b"secret" in raw.lower()


def test_a_zero_content_length_dispatches_an_empty_body(server) -> None:
    # A declared zero is the other spelling of "no body" and takes the read
    # path, not the absent-header shortcut, so it is pinned separately.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n"
        "\r\n",
    )
    assert status == 403
    assert b"secret" in raw.lower()


def test_a_chunked_body_with_trailers_is_drained(server) -> None:
    # Trailers follow the terminal chunk. Draining has to walk them to reach the
    # closing empty line, otherwise the refusal is answered from the middle of
    # the stream.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"5\r\nhello\r\n0\r\nX-Checksum: 1\r\n\r\n",
    )
    assert status == 400
    assert b"chunked Transfer-Encoding is not supported" in raw


# --- keep-alive framing (issue #533) ------------------------------------------------ #
#
# protocol_version is HTTP/1.1, so a refusal that answers and leaves the socket
# open hands whatever the client already wrote to the next parse. Every test
# above asks for Connection: close, which is why that stayed invisible. These
# send no Connection header and pipeline a real request behind the refusal: a
# body that is shaped like a request must not be dispatched as one.


def test_malformed_chunk_framing_closes_instead_of_smuggling(server, db: str) -> None:
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"zz\r\n"  # not a hex length: the drain gives up here, mid-body
        + pipelined_post("smuggled-past-chunk"),
    )
    assert response_statuses(raw) == [400], f"the unread remainder was answered too: {raw!r}"
    assert b"invalid chunked Transfer-Encoding" in raw
    assert b"Connection: close" in raw
    assert raw.endswith(b'{"error": "invalid chunked Transfer-Encoding"}'), raw
    with SQLiteStorage(db) as store:
        assert store.read_events("smuggled-past-chunk") == []


def test_invalid_content_length_closes_instead_of_smuggling(server, db: str) -> None:
    # Nothing is drained here: the length that would say how much to discard is
    # the thing that failed to parse, so every body byte already sent is still
    # queued when the 400 goes out.
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Length: abc\r\n"
        b"\r\n" + pipelined_post("smuggled-past-length"),
    )
    assert response_statuses(raw) == [400], f"the unread body was answered too: {raw!r}"
    assert b"invalid Content-Length" in raw
    assert b"Connection: close" in raw
    assert raw.endswith(b'{"error": "invalid Content-Length: \'abc\'"}'), raw
    with SQLiteStorage(db) as store:
        assert store.read_events("smuggled-past-length") == []


def test_a_chunk_without_its_terminating_crlf_is_malformed(server, db: str) -> None:
    # "hello" is not followed by CRLF, so the two bytes after it are the front of
    # the terminal chunk header. Consuming two bytes blindly swallowed them and
    # read as a clean finish, which is the one chunked outcome that answers
    # without closing.
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"5\r\nhello0\r\n\r\n" + pipelined_post("smuggled-past-terminator"),
    )
    assert response_statuses(raw) == [400], f"the remainder was parsed as requests: {raw!r}"
    assert b"invalid chunked Transfer-Encoding" in raw
    assert b"Connection: close" in raw
    # Nothing trails the refusal. The leftover used to be parsed as a request
    # line, and a line the parser reads as HTTP/0.9 answers with no status line
    # at all, so the count above cannot see it on its own.
    assert raw.endswith(b'{"error": "invalid chunked Transfer-Encoding"}'), raw
    with SQLiteStorage(db) as store:
        assert store.read_events("smuggled-past-terminator") == []


def test_an_oversize_refusal_keeps_a_well_framed_connection(server, db: str) -> None:
    # The other half of the fix: it closes what it cannot frame, not everything
    # it refuses. An oversize Content-Length is drained in full, so the boundary
    # is known and the next request on the socket is still served.
    payload = b"x" * (MAX_SIDECAR_BODY_BYTES + 1)
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"\r\n" + payload + pipelined_post("after-oversize"),
    )
    assert response_statuses(raw) == [413, 200], f"the pipelined request was lost: {raw!r}"
    with SQLiteStorage(db) as store:
        assert store.read_events("after-oversize")


def test_a_drained_chunked_body_keeps_the_connection(server, db: str) -> None:
    # Same for a chunked body that reaches its terminal chunk: refused, but the
    # stream ended where the framing said it would.
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"5\r\nhello\r\n0\r\n\r\n" + pipelined_post("after-chunked"),
    )
    assert response_statuses(raw) == [400, 200], f"the pipelined request was lost: {raw!r}"
    assert b"chunked Transfer-Encoding is not supported" in raw
    with SQLiteStorage(db) as store:
        assert store.read_events("after-chunked")


def test_a_chunked_drain_over_the_bound_is_413_and_closes(
    server, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is 256 MB in production, so it is patched down rather than fed:
    # the branch under test is the one that gives up mid-drain, and giving up
    # mid-drain is exactly the case that cannot be kept alive.
    monkeypatch.setattr(serve_module, "SIDECAR_DRAIN_LIMIT_BYTES", 32)
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"40\r\n" + b"x" * 64 + b"\r\n0\r\n\r\n" + pipelined_post("after-unbounded-chunk"),
    )
    assert response_statuses(raw) == [413], f"the remainder was answered too: {raw!r}"
    assert b"too large to drain" in raw
    assert b"Connection: close" in raw
    assert raw.endswith(b'{"error": "request body too large to drain"}'), raw
    with SQLiteStorage(db) as store:
        assert store.read_events("after-unbounded-chunk") == []


def test_an_oversize_drain_over_the_bound_is_413_and_closes(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve_module, "SIDECAR_DRAIN_LIMIT_BYTES", 32)
    raw = raw_pipeline(
        server,
        b"POST /record_progress HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Length: " + str(MAX_SIDECAR_BODY_BYTES + 1).encode() + b"\r\n"
        b"\r\n" + b"x" * 64,
    )
    assert response_statuses(raw) == [413]
    assert b"too large to drain" in raw
    assert b"Connection: close" in raw
    assert raw.endswith(b'{"error": "request body too large to drain"}'), raw


# --- drain edge cases (issue #533) -------------------------------------------------- #
#
# The give-up paths: framing that ends early, framing that never ends, and
# framing that is not framing. Each decides between a 400 and a 413, and with the
# keep-alive handling above, whether the socket survives the answer, so each is
# pinned rather than left to whatever shape a real client happens to send.


def test_a_chunked_request_with_no_body_at_all_is_refused(server) -> None:
    # EOF where the first chunk header should be. Nothing to drain, and the
    # refusal still has to be answered instead of waited on.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
    )
    assert status == 400
    assert b"chunked Transfer-Encoding is not supported" in raw


def test_a_blank_line_before_a_chunk_header_is_skipped(server) -> None:
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"\r\n5\r\nhello\r\n0\r\n\r\n",
    )
    assert status == 400
    assert b"chunked Transfer-Encoding is not supported" in raw


def test_a_negative_chunk_length_is_malformed(server) -> None:
    # int(b"-5", 16) parses, so a negative length arrives at the size check
    # rather than at the ValueError above it.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"-5\r\nhello\r\n0\r\n\r\n",
    )
    assert status == 400
    assert b"invalid chunked Transfer-Encoding" in raw


def test_a_chunk_that_ends_early_is_refused_not_awaited(server) -> None:
    # Declares 0x40 bytes, sends four, then stops writing. The drain stops at
    # the short read instead of blocking for the rest.
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"40\r\nxxxx",
    )
    assert status == 400
    assert b"chunked Transfer-Encoding is not supported" in raw


def test_trailers_past_the_bound_are_413(server, monkeypatch: pytest.MonkeyPatch) -> None:
    # Trailers after the terminal chunk are their own unbounded stream: a client
    # that never sends the closing empty line would otherwise be read forever.
    monkeypatch.setattr(serve_module, "SIDECAR_DRAIN_LIMIT_BYTES", 8)
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"0\r\nX-One: 1\r\nX-Two: 2\r\n\r\n",
    )
    assert status == 413
    assert b"too large to drain" in raw


def test_chunk_data_past_the_bound_is_413(server, monkeypatch: pytest.MonkeyPatch) -> None:
    # Read granularity is patched down alongside the bound so the read loop takes
    # more than one pass, which is the only way to reach the check inside it
    # without sending the real 256 MB.
    monkeypatch.setattr(serve_module, "SIDECAR_DRAIN_LIMIT_BYTES", 16)
    monkeypatch.setattr(serve_module, "_DRAIN_READ_BYTES", 8)
    status, raw = raw_post(
        server,
        "POST /record_progress HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n",
        b"40\r\n" + b"x" * 64 + b"\r\n0\r\n\r\n",
    )
    assert status == 413
    assert b"too large to drain" in raw
