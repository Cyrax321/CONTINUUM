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
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from continuum.serve import (
    SidecarAuth,
    SidecarServer,
    get_openapi_spec,
)
from continuum.serve import conformance as conformance_module
from continuum.serve.conformance import (
    WireConformanceReport,
    WireTestResult,
    _example_value,
    _http_call,
    _type_is,
    _WireSpec,
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
            cast(Any, {"Content-Type": "text/html"}),
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
            cast(Any, {"Content-Type": "application/json"}),
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


# --- unit tests for wire-conformance schema helpers and canned transport ------ #


def _canned_wire_call(
    spec: dict[str, Any] | None = None,
    overrides: dict[str | tuple[str, str], Any] | None = None,
    raise_on: tuple[str, str] | str | None = None,
    exc: Exception | None = None,
) -> Any:
    open_api_spec = spec or get_openapi_spec()
    call_counts: dict[str, int] = {}
    overrides = overrides or {}

    def _call(
        url: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        call_key = f"{method}:{path}"
        call_counts[call_key] = call_counts.get(call_key, 0) + 1
        count = call_counts[call_key]

        if raise_on is not None:
            if isinstance(raise_on, tuple):
                if raise_on[0] == method and raise_on[1] == path:
                    raise exc or RuntimeError(f"error on {method} {path}")
            elif raise_on == path:
                raise exc or RuntimeError(f"error on {path}")

        if (method, path) in overrides:
            val = overrides[(method, path)]
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(path=path, method=method, payload=payload, headers=headers, count=count)
            return val
        if path in overrides:
            val = overrides[path]
            if isinstance(val, Exception):
                raise val
            if callable(val):
                return val(path=path, method=method, payload=payload, headers=headers, count=count)
            return val

        std_headers = {"Continuum-Version": "1.0", "Content-Type": "application/json"}
        run_id = (payload or {}).get("run_id", "test-run")

        if path == "/healthz":
            return 200, {"status": "ok", "version": "1.0.0"}, std_headers
        if path == "/openapi.json":
            return 200, open_api_spec, std_headers
        if path == "/record_progress":
            return (
                200,
                {"run_id": run_id, "completed": (payload or {}).get("completed", 1), "total": 10},
                std_headers,
            )
        if path == "/intercept_action":
            act_type = (payload or {}).get("action_type", "")
            if count == 2 and not act_type.startswith("conformance.probe."):
                return (
                    200,
                    {
                        "run_id": run_id,
                        "action_type": "stripe.charge",
                        "proceed": False,
                        "status": "completed",
                        "previous_result": {"tx_id": "tx_999"},
                    },
                    std_headers,
                )
            key = (payload or {}).get("key") or "probe-key"
            return (
                200,
                {
                    "run_id": run_id,
                    "action_type": act_type or "act",
                    "proceed": True,
                    "action_key": key,
                },
                std_headers,
            )
        if path == "/complete_action":
            if not (payload and "action_key" in payload):
                return 400, {"error": "bad params", "code": "bad_params"}, std_headers
            return 200, {"run_id": run_id, "action_id": "act-1", "status": "completed"}, std_headers
        if path == "/checkpoint":
            return 200, {"run_id": run_id, "checkpoint_id": "cp-1"}, std_headers
        if path == "/resume":
            return 200, {"run_id": run_id, "mode": "resume", "safe": True}, std_headers
        if path == "/list_actions":
            return 200, {"run_id": run_id, "actions": [], "unresolved": 0}, std_headers
        if path == "/unknown_method_xyz":
            return 404, {"error": "unknown method", "code": "method_not_found"}, std_headers
        if path == "/confirm":
            auth = (headers or {}).get("Authorization", "")
            if "client" in auth and "confirm" not in auth:
                return 403, {"error": "not authorized", "code": "not_authorized"}, std_headers
            return 200, {"run_id": run_id, "mode": "resume", "safe": True}, std_headers
        if path == "/fail_action":
            return 200, {"run_id": run_id, "action_id": "act-fail", "status": "failed"}, std_headers
        if path == "/reconcile_action":
            return (
                200,
                {"run_id": run_id, "action_id": "act-rec", "status": "completed"},
                std_headers,
            )
        if path == "/compensate_action":
            return (
                200,
                {"run_id": run_id, "action_id": "act-comp", "status": "compensated"},
                std_headers,
            )
        if path == "/validate":
            return 200, {"run_id": run_id, "mode": "resume", "safe": True}, std_headers

        return 200, {"status": "ok"}, std_headers

    return _call


def test_type_is_number_and_unmodeled_kind() -> None:
    assert _type_is(3.14, "number") is True
    assert _type_is(42, "number") is True
    assert _type_is(True, "number") is False
    assert _type_is("abc", "number") is False
    assert _type_is("anything", "unmodeled_unknown_keyword") is True


def test_wire_spec_defensive_lookups_and_circular_refs() -> None:
    spec = _WireSpec(
        {"openapi": "3.1.0", "paths": {"/str_node": "not_a_dict", "/empty": {"get": "not_a_dict"}}}
    )
    assert spec._lookup("https://example.com/remote") is None
    assert spec._lookup("#/components/missing") is None
    spec_with_non_dict = _WireSpec({"components": {"schemas": {"scalar": 123}}})
    assert spec_with_non_dict._lookup("#/components/schemas/scalar/child") is None

    circular = _WireSpec(
        {"components": {"schemas": {"loop": {"$ref": "#/components/schemas/loop"}}}}
    )
    resolved = circular.resolve({"$ref": "#/components/schemas/loop"})
    assert resolved == {"$ref": "#/components/schemas/loop"}

    assert spec.operation("/missing", "get") is None
    assert spec.operation("/str_node", "get") is None
    assert spec.operation("/empty", "get") is None


def test_wire_spec_body_and_response_schema_edges() -> None:
    spec = _WireSpec(
        {
            "openapi": "3.1.0",
            "paths": {
                "/no_body": {"post": {}},
                "/bad_content": {"post": {"requestBody": {"content": "not_dict"}}},
                "/bad_json": {
                    "post": {"requestBody": {"content": {"application/json": "not_dict"}}}
                },
                "/bad_schema": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": "not_dict"}}}
                    }
                },
                "/bad_responses": {"post": {"responses": "not_dict"}},
                "/unmatched_status": {"post": {"responses": {"200": "not_dict"}}},
                "/broken_ref": {
                    "post": {
                        "responses": {"200": {"$ref": "#/components/responses/NoSuchResponse"}}
                    }
                },
                "/good_ref": {
                    "post": {"responses": {"200": {"$ref": "#/components/responses/Success"}}}
                },
            },
            "components": {
                "responses": {
                    "Success": {"content": {"application/json": {"schema": {"type": "object"}}}}
                }
            },
        }
    )
    assert spec._body_schema(None) is None
    assert spec._body_schema({"content": "not_dict"}) is None
    assert spec._body_schema({"content": {"application/json": "not_dict"}}) is None
    assert spec._body_schema({"content": {"application/json": {"schema": "not_dict"}}}) is None
    assert spec.request_schema("/missing", "post") is None
    assert spec.request_schema("/no_body", "post") is None
    assert spec.request_schema("/bad_content", "post") is None
    assert spec.request_schema("/bad_json", "post") is None
    assert spec.request_schema("/bad_schema", "post") is None
    assert spec.response_schema("/missing", "post", 200) is None
    assert spec.response_schema("/bad_responses", "post", 200) is None
    assert spec.response_schema("/unmatched_status", "post", 500) is None
    assert spec.response_schema("/broken_ref", "post", 200) is None
    assert spec.response_schema("/good_ref", "post", 200) == {"type": "object"}


def test_example_value_typed_fallbacks() -> None:
    assert _example_value({"type": ["null", "string"]}, "title") == "conformance-title"
    assert _example_value({"type": ["string", "null"]}, "name") == "conformance-name"
    assert _example_value({"type": ["null"]}, "empty") == {}
    assert _example_value({"type": "integer"}, "count") == 1
    assert _example_value({"type": "number"}, "amount") == 1
    assert _example_value({"type": "boolean"}, "active") is False
    assert _example_value({"type": "array"}, "items") == []
    assert _example_value({"type": "object"}, "meta") == {}
    assert _example_value({}, "fallback") == {}


def test_wire_conformance_global_connection_failure_records_all_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionResetError("peer dropped connection")

    monkeypatch.setattr(conformance_module, "_http_call", boom)
    report = run_wire_conformance("http://127.0.0.1:9")

    assert report.passed is False
    assert report.failed_count == 12
    for r in report.results[:10]:
        assert r.passed is False
        assert "peer dropped connection" in r.detail
    assert report.results[10].passed is False
    assert "no served OpenAPI document" in report.results[10].detail
    assert report.results[11].passed is False
    assert "no served OpenAPI document" in report.results[11].detail


@pytest.mark.parametrize(
    "check_name,raise_condition",
    [
        ("health_check", lambda path, method, count: path == "/healthz" and count == 1),
        ("wire_version_header", lambda path, method, count: path == "/healthz" and count == 2),
        ("openapi_spec", lambda path, method, count: path == "/openapi.json"),
        (
            "progress_tracking",
            lambda path, method, count: path == "/record_progress" and count == 1,
        ),
        (
            "action_claim_and_completion",
            lambda path, method, count: path == "/intercept_action" and count == 1,
        ),
        (
            "action_deduplication_replay",
            lambda path, method, count: path == "/intercept_action" and count == 2,
        ),
        ("checkpoint_and_resume", lambda path, method, count: path == "/checkpoint"),
        ("list_actions_audit", lambda path, method, count: path == "/list_actions"),
        ("canonical_error_schema", lambda path, method, count: path == "/unknown_method_xyz"),
        (
            "confirm_credential_separation",
            lambda path, method, count: path == "/confirm" and count == 1,
        ),
        (
            "request_schema_conformance",
            lambda path, method, count: path == "/intercept_action" and count >= 3,
        ),
    ],
)
def test_wire_conformance_individual_check_exceptions(
    monkeypatch: pytest.MonkeyPatch, check_name: str, raise_condition: Any
) -> None:
    base = _canned_wire_call()

    def call_with_failure(
        url: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        call_counts = getattr(call_with_failure, "_counts", {})
        call_key = f"{method}:{path}"
        call_counts[call_key] = call_counts.get(call_key, 0) + 1
        call_with_failure._counts = call_counts
        if raise_condition(path, method, call_counts[call_key]):
            raise RuntimeError(f"synthetic error on {check_name}")
        return base(url, method=method, payload=payload, headers=headers, timeout=timeout)

    monkeypatch.setattr(conformance_module, "_http_call", call_with_failure)
    report = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )

    target = next(r for r in report.results if r.name == check_name)
    assert target.passed is False
    assert f"synthetic error on {check_name}" in target.detail

    if check_name not in (
        "openapi_spec",
        "request_schema_conformance",
        "response_schema_conformance",
    ):
        excluded = {check_name, "request_schema_conformance", "response_schema_conformance"}
        if check_name == "action_claim_and_completion":
            excluded.add("action_deduplication_replay")
        surrounding = [r for r in report.results if r.name not in excluded]
        assert all(r.passed for r in surrounding)


def test_response_schema_conformance_exception_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Driven directly through _http_call returning a schema with uniterable enum
    broken_spec = {
        "openapi": "3.1.0",
        "paths": {
            "/healthz": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"type": "object", "enum": 123}}
                            }
                        }
                    }
                }
            }
        },
        "components": {"schemas": {}},
    }
    monkeypatch.setattr(conformance_module, "_http_call", _canned_wire_call(spec=broken_spec))
    report = run_wire_conformance("http://canned")
    target = next(r for r in report.results if r.name == "response_schema_conformance")
    assert target.passed is False
    assert "argument of type 'int' is not iterable" in target.detail

    # 2. Driven by a response schema lookup exception
    monkeypatch.setattr(conformance_module, "_http_call", _canned_wire_call())

    def broken_response_schema(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("schema resolution crashed")

    monkeypatch.setattr(_WireSpec, "response_schema", broken_response_schema)
    report_lookup = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )

    target_lookup = next(
        r for r in report_lookup.results if r.name == "response_schema_conformance"
    )
    assert target_lookup.passed is False
    assert "schema resolution crashed" in target_lookup.detail
    other_results = [r for r in report_lookup.results if r.name != "response_schema_conformance"]
    assert all(r.passed for r in other_results)


def test_wire_conformance_check_failure_mismatch_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. health_check status != 200
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={"/healthz": (503, {"status": "degraded"}, {"Continuum-Version": "1.0"})}
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "health_check")
    assert res.passed is False
    assert "status=503" in res.detail

    # 2. wire_version_header missing header
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(overrides={"/healthz": (200, {"status": "ok", "version": "1.0.0"}, {})}),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "wire_version_header")
    assert res.passed is False
    assert "missing Continuum-Version header" in res.detail

    # 3. openapi_spec non-3.x
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={"/openapi.json": (200, {"openapi": "2.0"}, {"Continuum-Version": "1.0"})}
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "openapi_spec")
    assert res.passed is False
    assert "body={'openapi': '2.0'}" in res.detail

    # 4. progress_tracking mismatch completed/total
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={
                "/record_progress": (
                    200,
                    {"run_id": "r", "completed": 0, "total": 10},
                    {"Continuum-Version": "1.0"},
                )
            }
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "progress_tracking")
    assert res.passed is False
    assert "status=200" in res.detail

    # 5. action_claim_and_completion proceed=False on initial claim
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={"/intercept_action": (200, {"proceed": False}, {"Continuum-Version": "1.0"})}
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "action_claim_and_completion")
    assert res.passed is False
    assert "proceed': False" in res.detail

    # 6. action_deduplication_replay proceed=True on 2nd claim
    def dedup_proceeds(path: str, count: int, **kw: Any) -> Any:
        return (
            200,
            {"run_id": "r", "action_type": "stripe.charge", "proceed": True, "action_key": "k"},
            {"Continuum-Version": "1.0"},
        )

    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(overrides={"/intercept_action": dedup_proceeds}),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "action_deduplication_replay")
    assert res.passed is False
    assert "dedup_status=200" in res.detail

    # 7. checkpoint_and_resume missing checkpoint_id
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={"/checkpoint": (200, {"run_id": "r"}, {"Continuum-Version": "1.0"})}
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "checkpoint_and_resume")
    assert res.passed is False
    assert "cp_status=200" in res.detail

    # 8. list_actions_audit missing unresolved
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={
                "/list_actions": (200, {"run_id": "r", "actions": []}, {"Continuum-Version": "1.0"})
            }
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "list_actions_audit")
    assert res.passed is False
    assert "status=200" in res.detail

    # 9. canonical_error_schema bad code on 404
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={
                "/unknown_method_xyz": (404, {"code": "not_found"}, {"Continuum-Version": "1.0"})
            }
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "canonical_error_schema")
    assert res.passed is False
    assert "404=not_found" in res.detail

    # 10. confirm_credential_separation
    # 10a. client token was not rejected
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={
                "/confirm": (
                    200,
                    {"run_id": "r", "mode": "resume", "safe": True},
                    {"Continuum-Version": "1.0"},
                )
            }
        ),
    )
    rep = run_wire_conformance("http://canned", client_token="client-tok")
    res = next(r for r in rep.results if r.name == "confirm_credential_separation")
    assert res.passed is False
    assert "client token was not rejected: status=200" in res.detail

    # 10b. confirm with token status != 200
    def confirm_rejects_confirm_token(headers: dict[str, str], **kw: Any) -> Any:
        auth = (headers or {}).get("Authorization", "")
        if "client" in auth:
            return (
                403,
                {"error": "not authorized", "code": "not_authorized"},
                {"Continuum-Version": "1.0"},
            )
        return 500, {"error": "server error"}, {"Continuum-Version": "1.0"}

    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(overrides={"/confirm": confirm_rejects_confirm_token}),
    )
    rep = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )
    res = next(r for r in rep.results if r.name == "confirm_credential_separation")
    assert res.passed is False
    assert "confirm with token status=500" in res.detail

    # 10c. unauthenticated mode refusal
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={"/confirm": (500, {"error": "server error"}, {"Continuum-Version": "1.0"})}
        ),
    )
    rep = run_wire_conformance("http://canned")
    res = next(r for r in rep.results if r.name == "confirm_credential_separation")
    assert res.passed is False
    assert "status=500" in res.detail

    # 10d. client token with no confirm token passes
    monkeypatch.setattr(conformance_module, "_http_call", _canned_wire_call())
    rep = run_wire_conformance("http://canned", client_token="client-tok")
    res = next(r for r in rep.results if r.name == "confirm_credential_separation")
    assert res.passed is True


def test_schema_probes_internal_steering_and_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Probe claim failure (line 641) and compensate skip (line 649->666)
    def probe_claim_fails(path: str, payload: dict[str, Any] | None, **kw: Any) -> Any:
        act = (payload or {}).get("action_type", "")
        if act == "conformance.probe.complete":
            return 500, {"error": "claim failed"}, {"Continuum-Version": "1.0"}
        if act == "conformance.probe.compensate":
            return 500, {"error": "compensate claim failed"}, {"Continuum-Version": "1.0"}
        return (
            200,
            {"run_id": "r", "action_type": act, "proceed": True, "action_key": "k"},
            {"Continuum-Version": "1.0"},
        )

    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(overrides={"/intercept_action": probe_claim_fails}),
    )
    rep = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )
    req_res = next(r for r in rep.results if r.name == "request_schema_conformance")
    assert req_res.passed is False
    assert "could not claim a probe action for complete" in req_res.detail

    # Compensate complete fails (lines 661-665: pop compensate)
    def compensate_complete_fails(path: str, payload: dict[str, Any] | None, **kw: Any) -> Any:
        if (payload or {}).get("result", {}).get("settled"):
            return 500, {"error": "compensate settle failed"}, {"Continuum-Version": "1.0"}
        if not (payload and "action_key" in payload):
            return 400, {"error": "bad params", "code": "bad_params"}, {"Continuum-Version": "1.0"}
        return (
            200,
            {"run_id": "r", "action_id": "a", "status": "completed"},
            {"Continuum-Version": "1.0"},
        )

    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(overrides={"/complete_action": compensate_complete_fails}),
    )
    rep = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )
    req_res = next(r for r in rep.results if r.name == "request_schema_conformance")
    assert req_res.passed is False
    assert "could not complete the probe action the compensate probe names" in req_res.detail

    # Line 717: client_token present, confirm_token is None -> expected = 403 on /confirm probe
    monkeypatch.setattr(conformance_module, "_http_call", _canned_wire_call())
    rep = run_wire_conformance("http://canned", client_token="client-tok")
    req_res = next(r for r in rep.results if r.name == "request_schema_conformance")
    assert req_res.passed is True

    # Line 720: /confirm answered 200 when 403 expected
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={
                "/confirm": lambda count, **kw: (
                    (
                        403,
                        {"error": "no", "code": "not_authorized"},
                        {"Continuum-Version": "1.0"},
                    )
                    if count == 1
                    else (
                        200,
                        {"run_id": "r", "mode": "resume", "safe": True},
                        {"Continuum-Version": "1.0"},
                    )
                )
            }
        ),
    )
    rep = run_wire_conformance("http://canned", client_token="client-tok")
    req_res = next(r for r in rep.results if r.name == "request_schema_conformance")
    assert req_res.passed is False
    assert "/confirm: request carrying only the spec's required fields" in req_res.detail
    assert "expected 403" in req_res.detail

    # Undocumented response status code in observed calls (lines 766-770)
    monkeypatch.setattr(
        conformance_module,
        "_http_call",
        _canned_wire_call(
            overrides={
                "/healthz": lambda count, **kw: (
                    (200, {"status": "ok", "version": "1.0.0"}, {"Continuum-Version": "1.0"})
                    if count > 1
                    else (201, {"status": "ok", "version": "1.0.0"}, {"Continuum-Version": "1.0"})
                )
            }
        ),
    )
    rep = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )
    resp_res = next(r for r in rep.results if r.name == "response_schema_conformance")
    assert resp_res.passed is False
    assert "GET /healthz returned 201, which the served spec does not document" in resp_res.detail


def test_schema_probes_with_handwritten_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    handwritten_spec = {
        "openapi": "3.1.0",
        "paths": {
            "/no_body_op": {
                "post": {
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                    }
                }
            },
            "/fallback_fields": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "nullable_str",
                                        "num_field",
                                        "bool_field",
                                        "list_field",
                                        "obj_field",
                                    ],
                                    "properties": {
                                        "nullable_str": {"type": ["string", "null"]},
                                        "num_field": {"type": "integer"},
                                        "bool_field": {"type": "boolean"},
                                        "list_field": {"type": "array"},
                                        "obj_field": {"type": "object"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                    },
                }
            },
        },
        "components": {"schemas": {}},
    }

    wire = _WireSpec(handwritten_spec)
    # direct unit assertions
    assert wire.operation("/undeclared_path", "post") is None
    assert wire.request_schema("/no_body_op", "post") is None
    assert wire.response_schema("/no_body_op", "post", 404) is None
    unresolved_violations = wire.validate({}, {"$ref": "#/components/schemas/Unresolved"})
    assert any("unresolvable $ref" in v for v in unresolved_violations)

    # test run_wire_conformance schema probes with this spec
    canned = _canned_wire_call(spec=handwritten_spec)
    monkeypatch.setattr(conformance_module, "_http_call", canned)
    rep = run_wire_conformance(
        "http://canned", client_token="client-tok", confirm_token="confirm-tok"
    )
    # request schema probe will encounter /no_body_op (schema is None -> line 669)
    # and /fallback_fields (uses _example_value on typed fields)
    req_res = next(r for r in rep.results if r.name == "request_schema_conformance")
    assert req_res.passed is True
