"""Tests for OpenAPI 3.1 specification and wire conformance suite.

Verifies:
- OpenAPI 3.1 schema definition and export
- Live HTTP serving of /openapi.json and /healthz
- Continuum-Version: 1.0 header on responses
- Automated run_wire_conformance suite passing against authenticated and unauthenticated servers
- CLI entry point for continuum conformance --url <endpoint>
- Schema-aware conformance (#1895): the suite fails a server whose 200
  responses omit fields the served spec marks required, a server whose
  handlers demand parameters the request schema does not list, and a server
  that answers with a status its served spec does not document
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

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


def test_wire_spec_validator_resolves_and_validates() -> None:
    """The schema validator behind the suite covers refs, types, enums, required."""
    from continuum.serve.conformance import _WireSpec

    wire = _WireSpec(get_openapi_spec())

    # $ref pointers resolve, for request bodies, response bodies and the
    # shared error responses, and documented operations are found.
    assert wire.request_schema("/complete_action", "post") is not None
    assert wire.response_schema("/complete_action", "post", 200) is not None
    assert wire.response_schema("/complete_action", "post", 400) is not None
    assert wire.operation("/healthz", "get") is not None
    assert wire.operation("/not_a_documented_path", "post") is None
    assert "/complete_action" in wire.post_paths()
    assert "/healthz" not in wire.post_paths()

    complete = {"$ref": "#/components/schemas/CompleteActionResponse"}
    assert wire.validate({"run_id": "r", "action_id": "a", "status": "completed"}, complete) == []
    assert any(
        "action_id" in v for v in wire.validate({"run_id": "r", "status": "completed"}, complete)
    )
    # The status enum is part of the contract, not just its shape.
    assert wire.validate({"run_id": "r", "action_id": "a", "status": "pending"}, complete), (
        "status outside the documented enum must be a violation"
    )
    assert wire.validate({"run_id": 1, "action_id": "a", "status": "completed"}, complete), (
        "an integer run_id must be a violation"
    )

    # 3.1 nullable types, free-form schemas and unresolvable refs.
    assert wire.validate(None, {"type": ["string", "null"]}) == []
    assert wire.validate({"anything": 1}, {}) == []
    assert wire.validate("x", {"$ref": "#/components/schemas/NoSuchSchema"})

    # JSON keeps booleans apart from integers, even though Python does not.
    assert wire.validate(True, {"type": "integer"})
    assert wire.validate(1, {"type": "integer"}) == []
    assert wire.validate([{"a": 1}], {"type": "array", "items": {"type": "object"}}) == []
    assert wire.validate([1], {"type": "array", "items": {"type": "object"}})


def test_wire_conformance_fails_a_server_that_omits_required_response_fields(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A 200 missing a field the served spec marks required fails the suite (#1895).

    This is the exact blindness the issue reported: a server whose responses
    drop a field its own published spec marks required, while keeping every
    behaviour the suite's other tests exercise, used to pass the suite. Only
    the schema test sees it, because the endpoint's behaviour is otherwise
    unchanged.
    """
    import continuum.serve.server as server_module

    real_complete = server_module._HANDLERS["complete_action"]

    def complete_without_action_id(server: Any, params: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(real_complete(server, params))
        result.pop("action_id", None)
        return result

    monkeypatch.setitem(server_module._HANDLERS, "complete_action", complete_without_action_id)

    db = str(tmp_path / "conf_drift_response.db")
    storage = SQLiteStorage(db)
    sidecar = SidecarServer(storage=storage)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        report = run_wire_conformance(f"http://127.0.0.1:{http.port}")
        assert report.passed is False
        schema_result = next(r for r in report.results if r.name == "response_schema_conformance")
        assert schema_result.passed is False
        assert "/complete_action" in schema_result.detail
        assert "'action_id'" in schema_result.detail
        # The endpoint still works, so every behavioural test still passes:
        # the violation is visible to the schema test alone.
        behavioural = [
            r
            for r in report.results
            if r.name not in ("response_schema_conformance", "request_schema_conformance")
        ]
        assert all(r.passed for r in behavioural)
    finally:
        http.shutdown()


def test_wire_conformance_fails_a_server_that_demands_undocumented_parameters(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A handler demanding a parameter the request schema does not list fails the suite (#1895).

    A client generated from the published spec sends exactly the spec's
    required fields; a server that needs more answers it 400, which is the
    drift the request-schema probe exists to catch.
    """
    import continuum.serve.server as server_module

    real_record_progress = server_module._HANDLERS["record_progress"]

    def record_progress_demanding_notes(server: Any, params: dict[str, Any]) -> dict[str, Any]:
        # The suite's own progress call carries ``total``; the spec-minimal
        # probe does not, so only the probe is refused.
        if "total" not in params and "notes" not in params:
            raise server_module.BadParams("missing parameter 'notes'")
        settled: dict[str, Any] = real_record_progress(server, params)
        return settled

    monkeypatch.setitem(server_module._HANDLERS, "record_progress", record_progress_demanding_notes)

    db = str(tmp_path / "conf_drift_request.db")
    storage = SQLiteStorage(db)
    sidecar = SidecarServer(storage=storage)
    port = _next_port()
    http = SidecarHTTP(sidecar, port=port, bind="127.0.0.1")
    http.start_background()

    try:
        report = run_wire_conformance(f"http://127.0.0.1:{http.port}")
        assert report.passed is False
        request_result = next(r for r in report.results if r.name == "request_schema_conformance")
        assert request_result.passed is False
        assert "/record_progress" in request_result.detail
        assert "400" in request_result.detail
        # The behavioural progress test still passes, since it sends ``total``.
        progress_result = next(r for r in report.results if r.name == "progress_tracking")
        assert progress_result.passed is True
    finally:
        http.shutdown()
