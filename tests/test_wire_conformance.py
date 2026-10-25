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
from pathlib import Path

from continuum.serve import (
    SidecarAuth,
    SidecarServer,
    get_openapi_spec,
)
from continuum.serve.conformance import (
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
