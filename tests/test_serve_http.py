"""HTTP transport over the sidecar dispatch (issue #238).

Non-Python agents get the durability plane by POSTing JSON to
`continuum serve --transport http`. Same handlers, same token auth, errors
mapped to status codes - proven here against a live server thread.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from continuum.serve.server import SidecarHTTP, SidecarServer
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
