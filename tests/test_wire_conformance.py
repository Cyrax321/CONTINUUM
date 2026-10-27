"""Tests for OpenAPI 3.1 specification and wire conformance suite.

Verifies:
- OpenAPI 3.1 schema definition and export
- Live HTTP serving of /openapi.json and /healthz
- Continuum-Version: 1.0 header on responses
- Automated run_wire_conformance suite passing against authenticated and unauthenticated servers
- CLI entry point for continuum conformance --url <endpoint>
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from continuum.serve import (
    SidecarAuth,
    SidecarServer,
    get_openapi_spec,
)
from continuum.serve.conformance import (
    WireConformanceReport,
    WireTestResult,
    _http_call,
    cmd_conformance,
    run_wire_conformance,
)
from continuum.serve.server import SidecarHTTP
from continuum.storage import SQLiteStorage

_PORT_COUNTER = {"port": 9600}


def _next_port() -> int:
    _PORT_COUNTER["port"] += 1
    return _PORT_COUNTER["port"]


def test_openapi_spec_structure() -> None:
    spec = get_openapi_spec()
    assert spec["openapi"] == "3.1.0"
    assert "info" in spec
    assert spec["info"]["version"] == "1.0.0"

    paths = spec["paths"]
    assert "/intercept_action" in paths
    assert "/complete_action" in paths
    assert "/fail_action" in paths
    assert "/record_progress" in paths
    assert "/checkpoint" in paths
    assert "/validate" in paths
    assert "/resume" in paths
    assert "/confirm" in paths
    assert "/list_actions" in paths
    assert "/healthz" in paths
    assert "/openapi.json" in paths

    components = spec["components"]
    assert "BearerAuth" in components["securitySchemes"]
    assert "ConfirmAuth" in components["securitySchemes"]
    assert "InterceptActionRequest" in components["schemas"]
    assert "InterceptActionResponse" in components["schemas"]
    assert "ErrorResponse" in components["schemas"]


def test_sidecar_serves_openapi_and_healthz(tmp_path: Path) -> None:
    db = str(tmp_path / "openapi_test.db")
    storage = SQLiteStorage(db)
    sidecar = SidecarServer(storage=storage)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        import json
        import urllib.request

        base = f"http://127.0.0.1:{http.port}"

        # GET /healthz
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get("Continuum-Version") == "1.0"
            body = json.loads(resp.read().decode("utf-8"))
            assert body["status"] == "ok"

        # GET /openapi.json
        with urllib.request.urlopen(f"{base}/openapi.json", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get("Continuum-Version") == "1.0"
            spec = json.loads(resp.read().decode("utf-8"))
            assert spec["openapi"] == "3.1.0"
            assert "/intercept_action" in spec["paths"]
    finally:
        http.shutdown()


def test_wire_conformance_unauthenticated_server(tmp_path: Path) -> None:
    db = str(tmp_path / "conf_unauth.db")
    storage = SQLiteStorage(db)
    sidecar = SidecarServer(storage=storage)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        report = run_wire_conformance(f"http://127.0.0.1:{http.port}")
        rendered = report.render()
        assert "All" in rendered
        assert "conformance tests passed" in rendered
        assert report.passed is True
        assert report.failed_count == 0
    finally:
        http.shutdown()


def test_wire_conformance_authenticated_server(tmp_path: Path) -> None:
    db = str(tmp_path / "conf_auth.db")
    storage = SQLiteStorage(db)
    auth = SidecarAuth(
        client_tokens=["test-client-token"],
        confirm_token="test-confirm-token",
    )
    sidecar = SidecarServer(storage=storage, auth=auth)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        report = run_wire_conformance(
            f"http://127.0.0.1:{http.port}",
            client_token="test-client-token",
            confirm_token="test-confirm-token",
        )
        assert "All" in report.render()
        assert report.passed is True
        assert report.failed_count == 0
        assert report.passed_count >= 10
    finally:
        http.shutdown()


def test_cli_conformance_command(tmp_path: Path) -> None:
    db = str(tmp_path / "conf_cli.db")
    storage = SQLiteStorage(db)
    auth = SidecarAuth(
        client_tokens=["client-tok"],
        confirm_token="confirm-tok",
    )
    sidecar = SidecarServer(storage=storage, auth=auth)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        from continuum.cli.main import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "conformance",
                "--url",
                f"http://127.0.0.1:{http.port}",
                "--token",
                "client-tok",
                "--confirm-token",
                "confirm-tok",
            ]
        )

        out = io.StringIO()
        err = io.StringIO()
        code = cmd_conformance(args, out=out, err=err)
        assert code == 0
        output = out.getvalue()
        assert "All" in output
        assert "conformance tests passed" in output
    finally:
        http.shutdown()


# --- report rendering and HTTP error handling, without a live server ---------- #


def test_report_render_names_every_failure() -> None:
    report = WireConformanceReport(target_url="http://127.0.0.1:9")
    report.results.append(WireTestResult("healthz", True))
    report.results.append(WireTestResult("openapi", False, "missing Continuum-Version header"))

    rendered = report.render()

    assert report.passed is False
    assert report.passed_count == 1
    assert report.failed_count == 1
    assert "FAILED: 1 of 2 conformance tests failed." in rendered
    assert "[FAIL] openapi: missing Continuum-Version header" in rendered
    assert "[PASS] healthz" in rendered


def test_report_render_announces_a_clean_pass() -> None:
    report = WireConformanceReport(target_url="http://127.0.0.1:9")
    report.results.append(WireTestResult("healthz", True))
    report.results.append(WireTestResult("openapi", True, "3.1.0"))

    rendered = report.render()

    assert report.passed is True
    assert report.failed_count == 0
    assert "All 2 conformance tests passed." in rendered
    assert "FAIL" not in rendered


def test_http_call_returns_the_raw_body_when_an_error_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy's HTML error page is not a parse error; it is evidence."""

    def boom(req: Any, timeout: float = 5.0) -> Any:
        raise urllib.error.HTTPError(
            req.full_url,
            500,
            "Internal Server Error",
            {"Content-Type": "text/html"},
            io.BytesIO(b"<html>gateway error</html>"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    status, parsed, headers = _http_call("http://127.0.0.1:9/broken")

    assert status == 500
    assert parsed == {"raw": "<html>gateway error</html>"}
    assert headers["Content-Type"] == "text/html"


def test_http_call_parses_a_json_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed JSON error body still comes back structured."""

    def boom(req: Any, timeout: float = 5.0) -> Any:
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error": "duplicate claim", "code": 409}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    status, parsed, _headers = _http_call("http://127.0.0.1:9/claim")

    assert status == 409
    assert parsed == {"error": "duplicate claim", "code": 409}


def test_cli_conformance_command_returns_one_when_the_suite_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing suite is a CLI failure, not a zero-exit report."""
    from continuum.serve import conformance

    failed = WireConformanceReport(target_url="http://127.0.0.1:9")
    failed.results.append(WireTestResult("healthz", False, "missing Continuum-Version header"))
    monkeypatch.setattr(conformance, "run_wire_conformance", lambda *a, **k: failed)

    args = SimpleNamespace(url="http://127.0.0.1:9", token=None, confirm_token=None)
    out = io.StringIO()

    code = cmd_conformance(args, out=out, err=io.StringIO())

    assert code == 1
    rendered = out.getvalue()
    assert "FAILED: 1 of 1 conformance tests failed." in rendered
    assert "missing Continuum-Version header" in rendered
