"""Signed webhook delivery primitive (issue #305, unit 1)."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import threading

import pytest

from continuum.recovery.notify import (
    SIGNATURE_HEADER,
    post_webhook,
    signature,
    verify_signature,
)


def test_signature_known_answer() -> None:
    expected = "sha256=" + hmac.new(b"s3cret", b'{"a":1}', hashlib.sha256).hexdigest()
    assert signature(b'{"a":1}', "s3cret") == expected


def test_verify_accepts_and_rejects() -> None:
    payload = b'{"run_id": "r1"}'
    header = signature(payload, "s3cret")
    assert verify_signature(payload, "s3cret", header) is True
    assert verify_signature(payload, "wrong", header) is False
    assert verify_signature(payload, "s3cret", None) is False
    assert verify_signature(payload, "s3cret", "bogus") is False


def _receiver(captured: dict, status: int = 200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            captured["body"] = self.rfile.read(length)
            captured["signature"] = self.headers.get(SIGNATURE_HEADER)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_signed_post_delivers_and_verifies() -> None:
    captured: dict = {}
    server = _receiver(captured)
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        assert post_webhook(url, {"run_id": "r1"}, secret="s3cret") is True
        assert json.loads(captured["body"]) == {"run_id": "r1"}
        assert verify_signature(captured["body"], "s3cret", captured["signature"]) is True
    finally:
        server.shutdown()


def test_unsigned_post_keeps_plain_format(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CONTINUUM_WEBHOOK_SECRET", raising=False)
    captured: dict = {}
    server = _receiver(captured)
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        assert post_webhook(url, {"run_id": "r1"}, secret=None) is True
        assert captured["signature"] is None
    finally:
        server.shutdown()


def test_delivery_failure_returns_false(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CONTINUUM_WEBHOOK_SECRET", raising=False)
    assert post_webhook("http://127.0.0.1:1/hook", {"run_id": "r1"}, timeout=0.2) is False


def test_secret_env_var_signs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}
    server = _receiver(captured)
    try:
        monkeypatch.setenv("CONTINUUM_WEBHOOK_SECRET", "env-secret")
        url = f"http://127.0.0.1:{server.server_port}/hook"
        assert post_webhook(url, {"run_id": "r1"}) is True
        assert verify_signature(captured["body"], "env-secret", captured["signature"]) is True
    finally:
        server.shutdown()
        monkeypatch.undo()


def test_non_serializable_payload_fails_open() -> None:
    """Serialization happens inside the fail-open boundary (#674 review)."""
    assert post_webhook("http://127.0.0.1:1/hook", {"bad": object()}, timeout=0.2) is False


def test_malformed_status_line_still_fails_open() -> None:
    """Response framing errors must not escape post_webhook (#674 review)."""
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def garble() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(5)
            try:
                conn.recv(65536)
            except OSError:
                return
            conn.sendall(b"NOTHTTP garbage\r\n\r\n")

    thread = threading.Thread(target=garble, daemon=True)
    thread.start()
    try:
        assert post_webhook(f"http://127.0.0.1:{port}/hook", {"run_id": "r1"}) is False
    finally:
        stop.set()
        listener.close()


def test_load_webhooks_absent_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from continuum.recovery.notify import load_webhooks

    assert load_webhooks(tmp_path / "nope.json") == []


def test_load_webhooks_validates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json as _json

    from continuum.recovery.notify import WebhookConfigError, load_webhooks

    good = tmp_path / "webhooks.json"
    good.write_text(
        _json.dumps(
            {
                "webhooks": [
                    {"url": "https://hooks.example.com/x", "secret": "s"},
                    {
                        "url": "http://int/hook",
                        "events": ["request_human", "liveness_breach"],
                        "timeout": 2,
                        "max_retries": 3,
                    },
                ]
            }
        )
    )
    endpoints = load_webhooks(good)
    assert len(endpoints) == 2
    assert endpoints[0].events == ("request_human",)
    assert endpoints[0].wants("request_human") and not endpoints[0].wants("liveness_breach")
    assert endpoints[1].timeout == 2 and endpoints[1].max_retries == 3

    bad_shapes = [
        {"webhooks": [{"url": "file:///etc/passwd"}]},
        {"webhooks": [{"url": "https://x", "events": ["nope"]}]},
        {"webhooks": [{"url": "https://x", "events": []}]},
        {"webhooks": [{"url": "https://x", "timeout": True}]},
        {"webhooks": [{"url": "https://x", "timeout": -1}]},
        {"webhooks": [{"url": "https://x", "max_retries": True}]},
        {"webhooks": [{"url": "https://x", "max_retries": 9}]},
        {"webhooks": [{"url": 42}]},
    ]
    # A missing key behaves like a missing file: no endpoints, no error.
    empty = tmp_path / "empty.json"
    empty.write_text(_json.dumps({"nope": []}))
    assert load_webhooks(empty) == []
    for i, shape in enumerate(bad_shapes):
        p = tmp_path / f"bad{i}.json"
        p.write_text(_json.dumps(shape))
        with pytest.raises(WebhookConfigError):
            load_webhooks(p)


def test_notify_endpoints_fans_out_by_subscription() -> None:
    """Only subscribed endpoints receive the event; results keyed by url."""
    from continuum.recovery.notify import WebhookEndpoint, notify_endpoints

    captured: dict = {}
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            captured.setdefault("n", 0)
            captured["n"] += 1
            self.rfile.read(length)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        endpoints = [
            WebhookEndpoint(url=url, events=("request_human",)),
            WebhookEndpoint(url="http://127.0.0.1:1/dead", events=("request_human",)),
            WebhookEndpoint(url=url, events=("liveness_breach",)),
        ]
        results = notify_endpoints(endpoints, "request_human", {"run_id": "r1"})
        assert results == {url: True, "http://127.0.0.1:1/dead": False}
        assert captured["n"] == 1
        assert notify_endpoints(endpoints, "requires_review", {"run_id": "r1"}) == {}
    finally:
        server.shutdown()
        server.server_close()


def test_delivery_failure_is_audit_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Dead letters record without touching projection or verify (#305)."""
    from continuum.events import EventType
    from continuum.models import Run
    from continuum.recovery.notify import record_delivery_failure
    from continuum.state.semantic import project
    from continuum.storage import SQLiteStorage

    db = str(tmp_path / "deadletter.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="run_1", goal="g"))
        before = project("run_1", store.read_events("run_1"))
        record_delivery_failure(store, "run_1", "http://dead/hook", "request_human", "refused")
        events = store.read_events("run_1")
        dead = [e for e in events if e.type is EventType.NOTIFY_FAILED]
        assert len(dead) == 1
        assert dead[0].payload["url"] == "http://dead/hook"
        after = project("run_1", events)
        assert after.progress == before.progress
        assert after.goal == before.goal
        assert store.verify_events("run_1").ok


def test_duplicate_urls_aggregate_conservatively() -> None:
    """One success must not mask another entry's failure on the same url."""
    import http.server
    import threading

    from continuum.recovery.notify import WebhookEndpoint, notify_endpoints

    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            captured["n"] = captured.get("n", 0) + 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        endpoints = [
            WebhookEndpoint(url=url, events=("request_human",)),
            WebhookEndpoint(url=url, events=("request_human",)),
        ]
        assert notify_endpoints(endpoints, "request_human", {}) == {url: True}
        assert captured["n"] == 2
    finally:
        server.shutdown()
        server.server_close()
