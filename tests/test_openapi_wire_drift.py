"""The OpenAPI document must describe the wire the sidecar actually speaks (#1894).

The spec served at ``GET /openapi.json`` is the contract generated clients are
built from, so a request built from nothing but its ``required`` list has to be
accepted, and every 200 body has to satisfy the response schema it documents.
These tests drive the reference sidecar over HTTP and compare the two in both
directions. Without them the spec and the handlers drifted apart in plain
sight: a client generated from the document was refused with 400 on
``record_progress`` and ``fail_action``, and ``complete_action``/``fail_action``
bodies failed the response contract, while the whole suite stayed green.

Direction chosen for the repair: the document was corrected to the wire the
reference implementation speaks, not the other way around. The implementation
is the shipped, behaviour-tested surface; the document was new and wrong on
every point listed in the issue.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from continuum.serve.openapi import get_openapi_spec
from continuum.serve.server import SidecarHTTP, SidecarServer, list_methods
from continuum.storage import SQLiteStorage

_RUN_ID = "spec-drift-run"
_WEB_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "number": (int, float),
}


def _start_sidecar(db: Path) -> tuple[SidecarHTTP, SidecarServer]:
    sidecar = SidecarServer(storage=SQLiteStorage(str(db)))
    http = SidecarHTTP(sidecar, port=0, bind="127.0.0.1")
    http.start_background()
    time.sleep(0.1)
    return http, sidecar


def _post(base: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def _get(base: str, path: str) -> tuple[int, dict[str, Any]]:
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def _spec_from_server(base: str) -> dict[str, Any]:
    """The document as a client reads it off the wire, not from the module."""
    return _get(base, "/openapi.json")[1]


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Follow a single $ref to its target schema."""
    ref = schema.get("$ref")
    if ref is None:
        return schema
    target: dict[str, Any] = spec["components"]["schemas"][ref.rsplit("/", 1)[1]]
    return target


def _request_schema(spec: dict[str, Any], path: str) -> dict[str, Any]:
    body = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    return _resolve(spec, body)


def _response_schema(spec: dict[str, Any], path: str) -> dict[str, Any]:
    body = spec["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    return _resolve(spec, body)


def _type_allows(schema: dict[str, Any], value: Any) -> bool:
    """OpenJSON type check for a single observed value."""
    declared = schema.get("type")
    if declared is None:
        return True  # the schema places no constraint on the value
    if isinstance(declared, str):
        declared = [declared]
    if value is None:
        return "null" in declared
    for kind in declared:
        expected = _WEB_TYPES[kind]
        if expected is int and isinstance(value, bool):
            continue  # bool is an int in Python but not on the wire
        if isinstance(value, expected):
            return True
    return False


@pytest.fixture(scope="module")
def wire(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, Any]]]:
    """A live sidecar and the spec it serves."""
    db = tmp_path_factory.mktemp("openapi_drift") / "drift.db"
    http, sidecar = _start_sidecar(db)
    base = f"http://127.0.0.1:{http.port}"
    yield base, _spec_from_server(base)
    http.shutdown()
    sidecar.close()


@pytest.fixture(scope="module")
def session(
    wire: tuple[str, dict[str, Any]],
) -> dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]]:
    """One endpoint's (request, status, body) triples, for the comparisons below."""
    return _drive_session(wire[0], wire[1])


def _drive_session(
    base: str, spec: dict[str, Any]
) -> dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]]:
    """Speak the wire exactly as a spec-generated client would, across every path.

    Two rounds:

    - A hand-written round that walks every response path the handlers have
      (fresh claim, cached replay, unknown effect, grant denial, failure,
      compensation, ...), so the union of observed bodies is the whole wire.
    - A spec-driven round: one request per endpoint carrying *exactly* the
      ``required`` list of the request schema the server serves. This is the
      round a generated client runs, so a parameter the server demands but the
      document omits shows up here as a 400 ``missing parameter``.
    """
    seen: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]] = {}

    def call(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = _post(base, path, payload)
        seen.setdefault(path, []).append((payload, status, body))
        assert status == 200, f"{path} {payload} -> {status} {body}"
        return body

    def action_key(body: dict[str, Any]) -> str:
        key = body.get("action_key")
        assert key, f"claim did not return an action_key: {body}"
        return str(key)

    # resume with no run_id is legal: the spec marks it optional
    call("/resume", {})

    # record_progress: the first call creates the run and must carry goal
    call("/record_progress", {"run_id": _RUN_ID, "completed": 0, "total": 4, "goal": "ship it"})
    call("/record_progress", {"run_id": _RUN_ID, "completed": 1})

    # a claim, then completing it (the minimal request for complete_action)
    charge = call("/intercept_action", {"run_id": _RUN_ID, "action_type": "stripe.charge"})
    charge_key = action_key(charge)
    call("/complete_action", {"run_id": _RUN_ID, "action_key": charge_key})

    # re-claiming the completed action: cached result, proceed=False
    call("/intercept_action", {"run_id": _RUN_ID, "action_type": "stripe.charge"})

    # a claim whose attempt is left interrupted: re-claiming reports an unknown
    # effect, and reconcile settles it from outside evidence
    post = call("/intercept_action", {"run_id": _RUN_ID, "action_type": "slack.post"})
    call("/intercept_action", {"run_id": _RUN_ID, "action_type": "slack.post"})
    settled = call(
        "/reconcile_action",
        {"run_id": _RUN_ID, "action_key": action_key(post), "occurred": False},
    )
    assert settled["status"] == "failed", settled

    # an in-flight action fails; certain is unset, so the side effect stays uncertain
    email = call("/intercept_action", {"run_id": _RUN_ID, "action_type": "email.send"})
    failed = call(
        "/fail_action",
        {"run_id": _RUN_ID, "action_key": action_key(email), "error": "boom"},
    )
    assert failed["side_effect_uncertain"] is True, failed

    # a consumed single-use grant is refused on the next claim carrying it
    granted = call(
        "/intercept_action",
        {
            "run_id": _RUN_ID,
            "action_type": "payout.issue",
            "key": "payout-key",
            "grant": {"id": "grant-1", "scope": "payouts"},
        },
    )
    call(
        "/complete_action",
        {"run_id": _RUN_ID, "action_key": action_key(granted), "external_id": "ext-9"},
    )
    call(
        "/intercept_action",
        {
            "run_id": _RUN_ID,
            "action_type": "payout.issue",
            "key": "payout-key",
            "grant": {"id": "grant-1", "scope": "payouts"},
        },
    )

    # only a completed effect can be compensated
    call("/compensate_action", {"run_id": _RUN_ID, "action_key": charge_key})

    call("/checkpoint", {"run_id": _RUN_ID})
    call("/validate", {"run_id": _RUN_ID})
    call("/resume", {"run_id": _RUN_ID})
    call("/confirm", {"run_id": _RUN_ID})
    call("/list_actions", {"run_id": _RUN_ID})

    # one action per status the action endpoints need, for the spec-driven round
    to_complete = call("/intercept_action", {"run_id": _RUN_ID, "action_type": "task.edit"})
    to_fail = call("/intercept_action", {"run_id": _RUN_ID, "action_type": "task.delete"})
    to_reconcile = call("/intercept_action", {"run_id": _RUN_ID, "action_type": "task.sync"})
    call("/intercept_action", {"run_id": _RUN_ID, "action_type": "task.sync"})  # -> unknown

    _minimal_round(
        base,
        spec,
        seen,
        action_keys={
            "/complete_action": action_key(to_complete),
            "/fail_action": action_key(to_fail),
            "/reconcile_action": action_key(to_reconcile),
            "/compensate_action": charge_key,
        },
    )
    return seen


def _minimal_round(
    base: str,
    spec: dict[str, Any],
    seen: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]],
    *,
    action_keys: dict[str, str],
) -> None:
    """Send exactly each request schema's required list and expect it accepted.

    This is the generated-client round: the spec a client is built from is the
    one the server serves, so the required lists are read off the wire rather
    than duplicated here.
    """
    # The order matters only within an action's lifecycle: each key is consumed
    # by its endpoint, and re-running the round would reuse settled actions.
    for path in ("/record_progress", "/list_actions", "/checkpoint", "/validate", "/resume"):
        _minimal_call(base, spec, seen, path, {}, run_id=_RUN_ID)
    for path, key in action_keys.items():
        _minimal_call(base, spec, seen, path, {"action_key": key}, run_id=_RUN_ID)
    _minimal_call(
        base, spec, seen, "/intercept_action", {"action_type": "minimal.claim"}, run_id=_RUN_ID
    )
    _minimal_call(base, spec, seen, "/confirm", {}, run_id=_RUN_ID)


def _minimal_call(
    base: str,
    spec: dict[str, Any],
    seen: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]],
    path: str,
    values: dict[str, Any],
    *,
    run_id: str,
) -> None:
    schema = _request_schema(spec, path)
    required = schema.get("required", [])
    defaults: dict[str, Any] = {
        "run_id": run_id,
        "completed": 3,
        "error": "minimal-request failure",
        "occurred": False,
    }
    payload = {field: values.get(field, defaults.get(field)) for field in required}
    if None in payload.values():
        missing = [f for f, v in payload.items() if v is None]
        raise AssertionError(f"no minimal value known for {path} field(s) {missing}")
    status, body = _post(base, path, payload)
    seen.setdefault(path, []).append((payload, status, body))
    assert status == 200, (
        f"a request of exactly {path}'s documented required list {required} "
        f"was refused: {status} {body}"
    )


def test_spec_documents_every_dispatched_method() -> None:
    spec = get_openapi_spec()
    methods = {f"/{name}" for name in list_methods()}
    posts = {path for path, ops in spec["paths"].items() if "post" in ops}
    assert posts == methods, (
        f"POST paths and dispatched methods disagree: "
        f"undocumented={sorted(methods - posts)} unimplemented={sorted(posts - methods)}"
    )
    for probe in ("/healthz", "/readyz", "/openapi.json", "/spec"):
        assert probe in spec["paths"], f"{probe} is served but not documented"


def test_documented_schemas_resolve(wire: tuple[str, dict[str, Any]]) -> None:
    """Every schema referenced by a path must be defined."""
    spec = wire[1]
    referenced: set[str] = set()
    for ops in spec["paths"].values():
        for op in ops.values():
            request_schema = (
                op.get("requestBody", {}).get("content", {}).get("application/json", {})
            )
            if "$ref" in request_schema.get("schema", {}):
                referenced.add(request_schema["schema"]["$ref"].rsplit("/", 1)[1])
            for response in op.get("responses", {}).values():
                content = response.get("content", {}).get("application/json", {})
                if "$ref" in content.get("schema", {}):
                    referenced.add(content["schema"]["$ref"].rsplit("/", 1)[1])
    for name in referenced:
        assert name in spec["components"]["schemas"], f"{name} is referenced but undefined"


def test_spec_matches_the_wire(
    wire: tuple[str, dict[str, Any]],
    session: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]],
) -> None:
    """Every endpoint: minimal requests accepted, bodies satisfy the contract.

    Both drift directions are caught here:

    - A server demanding a parameter the document does not require answers 400
      ``missing parameter`` to a request carrying exactly the schema's
      ``required`` list.
    - A server omitting a field the document marks ``required`` leaves a 200
      body without it, and a documented field that never appears on the wire
      is never observed by the session.
    """
    spec = wire[1]

    for path, triples in session.items():
        schema = _response_schema(spec, path)
        required = schema.get("required", [])
        documented = set(schema.get("properties", {}))
        observed_union: set[str] = set()

        for request, status, body in triples:
            assert status == 200, f"{path} {request} -> {status} {body}"
            for field in required:
                assert field in body, (
                    f"{path} marks {field!r} required but the 200 body omits it: {sorted(body)}"
                )
            for field, value in body.items():
                if field in documented:
                    assert _type_allows(schema["properties"][field], value), (
                        f"{path}.{field} documented as "
                        f"{schema['properties'][field].get('type')} but the wire sent {value!r}"
                    )
            observed_union |= set(body)

        never_sent = documented - observed_union
        assert not never_sent, (
            f"{path} documents {sorted(never_sent)} but the server never sends them"
        )

        # request side: every parameter sent is documented, and nothing the
        # spec calls required is left out
        request_schema = _request_schema(spec, path)
        request_required = set(request_schema.get("required", []))
        request_documented = set(request_schema.get("properties", {}))
        for request, _, _ in triples:
            sent = set(request)
            unknown = sent - request_documented - request_required
            assert not unknown, (
                f"{path} sends parameters the spec does not document: {sorted(unknown)}"
            )
            missing = request_required - sent
            assert not missing, (
                f"{path} request omits a documented required field: {sorted(missing)}"
            )


def test_resume_without_run_id_answers_no_active_run(tmp_path: Path) -> None:
    """resume's run_id is optional: with no active run the server says so, 200."""
    http, sidecar = _start_sidecar(tmp_path / "empty.db")
    try:
        base = f"http://127.0.0.1:{http.port}"
        schema = _response_schema(_spec_from_server(base), "/resume")
        status, body = _post(base, "/resume", {})
        assert status == 200, body
        assert body["mode"] == "no_active_run", body
        for field in schema.get("required", []):
            assert field in body, f"resume without run_id omits required {field!r}"
        assert "null" in schema["properties"]["run_id"].get("type", []), (
            "resume documents run_id as a plain string but the server answers null"
        )
    finally:
        http.shutdown()
        sidecar.close()


def test_the_drift_from_the_issue_is_gone(
    wire: tuple[str, dict[str, Any]],
    session: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]],
) -> None:
    """The rows of the #1894 report, checked in the order the issue lists them."""
    base, spec = wire
    schemas = spec["components"]["schemas"]

    # the server demands no parameter the spec leaves optional, and accepts
    # what the spec forbids (resume with no run_id). record_progress on a run
    # that already exists needs only its required list; a brand-new run needs
    # goal too, which the spec documents on the field itself.
    status, body = _post(base, "/record_progress", {"run_id": _RUN_ID, "completed": 2})
    assert status == 200, f"record_progress was refused: {status} {body}"
    status, body = _post(base, "/resume", {})
    assert status == 200, f"resume without run_id was refused: {status} {body}"

    # the session exercises both endpoints that used to 400 a spec-generated
    # client, and both responses the document misdescribed
    for path in ("/record_progress", "/fail_action", "/complete_action", "/intercept_action"):
        assert path in session, f"the session never exercised {path}"

    # complete_action and fail_action return action_id, never action_key
    complete = schemas["CompleteActionResponse"]
    assert "action_key" not in complete["properties"]
    assert "action_id" in complete["properties"]
    status_response = schemas["ActionStatusResponse"]
    assert "action_key" not in status_response["properties"]
    assert "action_id" in status_response["properties"]

    # the fields the server never sends are no longer documented, and the ones
    # it does send are
    checkpoint = schemas["CheckpointResponse"]["properties"]
    assert "sequence" not in checkpoint and "hash" not in checkpoint
    for name in ("version", "trigger", "integrity_hash", "completed", "source_sequence"):
        assert name in checkpoint, f"CheckpointResponse does not document {name}"
    intercept = schemas["InterceptActionResponse"]["properties"]
    assert "reusable_result" not in intercept and "reconciliation_required" not in intercept
    assert "notes" not in schemas["RecordProgressRequest"]["properties"]
    assert "external_id" in schemas["CompleteActionRequest"]["properties"]


def test_error_and_health_bodies_match_the_spec(
    wire: tuple[str, dict[str, Any]],
) -> None:
    """The shared error schema and the health probes hold up too."""
    base, spec = wire
    schemas = spec["components"]["schemas"]

    status, body = _post(base, "/record_progress", {"run_id": "no-completed"})
    assert status == 400 and body.get("code") == "bad_params", body
    for field in schemas["ErrorResponse"]["required"]:
        assert field in body, f"the 400 body omits {field}"

    for probe in ("/healthz", "/readyz"):
        status, body = _get(base, probe)
        assert status == 200, (probe, status)
        for field in schemas["HealthResponse"]["required"]:
            assert field in body, f"{probe} omits {field}"
