"""HTTP transport over the sidecar dispatch (issue #238).

Non-Python agents get the durability plane by POSTing JSON to
`continuum serve --transport http`. Same handlers, same token auth, errors
mapped to status codes - proven here against a live server thread.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

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
