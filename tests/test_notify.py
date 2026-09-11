"""Signed webhook delivery primitive (issue #305, unit 1)."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import threading

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
        def do_POST(self) -> None:
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
