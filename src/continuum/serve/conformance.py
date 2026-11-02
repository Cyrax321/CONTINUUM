"""Wire protocol conformance test runner for CONTINUUM servers.

Validates that any server implementation (Python, Go, Node, or proxy) conforms
to the CONTINUUM wire specification:
- HTTP headers (Continuum-Version: 1.0)
- Health endpoints (/healthz)
- OpenAPI 3.1 specification (/openapi.json)
- Idempotent action claims (/intercept_action) and deduplication
- Action completion and cached outcome replay (/complete_action)
- Progress tracking (/record_progress)
- Checkpoint and semantic resume contract (/checkpoint, /resume)
- Audit log query (/list_actions)
- Canonical wire error schema (error message + code)
- Authentication enforcement and confirm credential separation
- Schema conformance against the served specification (#1895): every request
  the suite sends and every response it receives is validated against the
  ``components.schemas`` of the OpenAPI document the server itself serves at
  ``GET /openapi.json``, and every documented endpoint is probed with a
  request carrying only the fields that document marks required. A server
  whose 200 responses omit a field its own spec marks required, or whose
  handlers demand a parameter its spec does not list, therefore fails the
  suite instead of passing it on status codes alone.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, TextIO


@dataclass
class WireTestResult:
    """Outcome of a single wire conformance test."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class WireConformanceReport:
    """Aggregated report of all wire conformance test outcomes."""

    target_url: str
    results: list[WireTestResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def render(self) -> str:
        lines = [
            "CONTINUUM Wire Conformance Suite",
            f"Target: {self.target_url}",
            "",
        ]
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            suffix = f": {r.detail}" if r.detail else ""
            lines.append(f"  [{status}] {r.name}{suffix}")
        lines.append("")
        if self.passed:
            lines.append(f"All {len(self.results)} conformance tests passed.")
        else:
            lines.append(
                f"FAILED: {self.failed_count} of {len(self.results)} conformance tests failed."
            )
        return "\n".join(lines)


def _type_is(instance: Any, kind: str) -> bool:
    """Whether ``instance`` satisfies one OpenAPI ``type`` keyword.

    Booleans are not integers here, even though Python's ``isinstance`` says
    otherwise: JSON treats them as distinct types, and a spec that declares
    ``integer`` should not accept ``true``.
    """
    if kind == "object":
        return isinstance(instance, dict)
    if kind == "array":
        return isinstance(instance, list)
    if kind == "string":
        return isinstance(instance, str)
    if kind == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if kind == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if kind == "boolean":
        return isinstance(instance, bool)
    if kind == "null":
        return instance is None
    return True  # a keyword this validator does not model is not a violation


class _WireSpec:
    """A served OpenAPI document, resolved enough to validate bodies against it.

    Covers only the subset this specification uses: ``$ref`` pointers into the
    document, the ``type`` keyword in its scalar and 3.1 list form, ``enum``,
    ``required``, ``properties`` and ``items``. A full JSON Schema
    implementation is a dependency the runner must not take, since its whole
    point is to run against servers deployed into a stdlib-only environment.
    """

    #: How many ``$ref`` hops to follow before deciding a document is circular.
    MAX_REF_DEPTH = 8

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec

    def _lookup(self, ref: str) -> dict[str, Any] | None:
        """Resolve a local JSON pointer like ``#/components/schemas/Name``."""
        if not ref.startswith("#/"):
            return None
        node: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, dict) else None

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Follow ``$ref`` links to the schema they name, if it is reachable."""
        for _ in range(self.MAX_REF_DEPTH):
            ref = schema.get("$ref") if isinstance(schema, dict) else None
            if ref is None:
                return schema if isinstance(schema, dict) else {}
            target = self._lookup(str(ref))
            if target is None:
                raise KeyError(ref)
            schema = target
        return schema

    def operation(self, path: str, method: str) -> dict[str, Any] | None:
        """The operation object for ``path`` and ``method``, if documented."""
        node = (self.spec.get("paths") or {}).get(path)
        if not isinstance(node, dict):
            return None
        op = node.get(method.lower())
        return op if isinstance(op, dict) else None

    def post_paths(self) -> list[str]:
        """Every documented POST path, sorted for stable report output."""
        paths = self.spec.get("paths") or {}
        return sorted(
            path for path, node in paths.items() if isinstance(node, dict) and "post" in node
        )

    def _body_schema(self, media: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(media, dict):
            return None
        content = media.get("content")
        if not isinstance(content, dict):
            return None
        entry = content.get("application/json")
        if not isinstance(entry, dict):
            return None
        schema = entry.get("schema")
        return schema if isinstance(schema, dict) else None

    def request_schema(self, path: str, method: str) -> dict[str, Any] | None:
        """The request body schema for an operation, if that operation declares one."""
        op = self.operation(path, method)
        if op is None:
            return None
        return self._body_schema(op.get("requestBody"))

    def response_schema(self, path: str, method: str, status: int) -> dict[str, Any] | None:
        """The response body schema an operation pairs with ``status``, if any."""
        op = self.operation(path, method)
        if op is None:
            return None
        responses = op.get("responses")
        if not isinstance(responses, dict):
            return None
        entry = responses.get(str(status)) or responses.get("default")
        if not isinstance(entry, dict):
            return None
        if "$ref" in entry:
            try:
                entry = self.resolve(entry)
            except KeyError:
                return None
        return self._body_schema(entry)

    def validate(self, instance: Any, schema: dict[str, Any], where: str = "$") -> list[str]:
        """Validate ``instance`` against ``schema``; return violation messages.

        An empty schema accepts anything, which the published spec relies on
        for free-form fields like ``result``, so ``{}`` is a pass rather than
        an error.
        """
        try:
            schema = self.resolve(schema)
        except KeyError as exc:
            return [f"{where}: unresolvable $ref {exc}"]
        if not schema:
            return []
        violations: list[str] = []
        declared = schema.get("type")
        if declared is not None:
            allowed = declared if isinstance(declared, list) else [declared]
            if not any(_type_is(instance, kind) for kind in allowed):
                violations.append(
                    f"{where}: expected type {declared}, got {type(instance).__name__}"
                )
                return violations
        if "enum" in schema and instance not in schema["enum"]:
            violations.append(f"{where}: {instance!r} is not one of {schema['enum']}")
        if isinstance(instance, dict):
            properties = schema.get("properties") or {}
            for field_name in schema.get("required") or []:
                if field_name not in instance:
                    violations.append(f"{where}: missing required field {field_name!r}")
            for field_name, value in instance.items():
                if field_name in properties:
                    violations.extend(
                        self.validate(value, properties[field_name], f"{where}.{field_name}")
                    )
        elif isinstance(instance, list) and "items" in schema:
            for index, item in enumerate(instance):
                violations.extend(self.validate(item, schema["items"], f"{where}[{index}]"))
        return violations


def _example_value(schema: dict[str, Any], field_name: str) -> Any:
    """A stand-in value of the type a property declares, for minimal requests.

    Every required property this specification names is given an explicit
    value by the caller, so this fallback exists for fields added to the spec
    later, not for any in use today.
    """
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((kind for kind in declared if kind != "null"), declared[0])
    if declared == "string":
        return f"conformance-{field_name}"
    if declared in ("integer", "number"):
        return 1
    if declared == "boolean":
        return False
    if declared == "array":
        return []
    return {}


def _http_call(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data: bytes | None = None
    if payload is not None or method == "POST":
        data = json.dumps(payload or {}).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            raw_headers = dict(resp.headers.items())
            parsed = json.loads(body.decode("utf-8")) if body else {}
            return resp.status, parsed, raw_headers
    except urllib.error.HTTPError as exc:
        raw_body = exc.read()
        raw_headers = dict(exc.headers.items())
        try:
            parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            parsed = {"raw": raw_body.decode("utf-8", errors="replace")}
        return exc.code, parsed, raw_headers


def run_wire_conformance(
    base_url: str,
    *,
    client_token: str | None = None,
    confirm_token: str | None = None,
) -> WireConformanceReport:
    """Run the complete wire conformance suite against a target server."""
    base = base_url.rstrip("/")
    report = WireConformanceReport(target_url=base)
    run_id = f"conformance-run-{int(time.time() * 1000)}"

    def auth_headers(tok: str | None = None) -> dict[str, str]:
        active_tok = tok if tok is not None else client_token
        if active_tok:
            return {"Authorization": f"Bearer {active_tok}"}
        return {}

    #: Every exchange of the run, so the schema tests can judge the whole
    #: conversation rather than only the calls made for their own sake.
    #: Tuple: (path, method, status, response body, request body or None).
    observed: list[tuple[str, str, int, Any, Any]] = []

    def call(
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        record_request: bool = True,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Make one request and remember it for the schema tests.

        ``record_request=False`` marks a request the suite sends *because* it
        violates the published request schema; its body is recorded as ``None``
        so the request schema test does not flag the suite's own deliberate
        violation.
        """
        status, body, raw_headers = _http_call(
            f"{base}{path}", method=method, payload=payload, headers=headers
        )
        request = payload if method == "POST" and record_request else None
        observed.append((path, method, status, body, request))
        return status, body, raw_headers

    wire: _WireSpec | None = None

    # 1. Health check (/healthz)
    try:
        status, body, headers = call("/healthz", method="GET")
        if status == 200 and body.get("status") == "ok":
            report.results.append(WireTestResult("health_check", True))
        else:
            report.results.append(
                WireTestResult("health_check", False, f"status={status}, body={body}")
            )
    except Exception as exc:
        report.results.append(WireTestResult("health_check", False, str(exc)))

    # 2. Wire version header (Continuum-Version: 1.0)
    try:
        status, body, headers = call("/healthz", method="GET")
        version_header = headers.get("Continuum-Version") or headers.get("continuum-version")
        if version_header:
            report.results.append(
                WireTestResult("wire_version_header", True, f"version={version_header}")
            )
        else:
            report.results.append(
                WireTestResult("wire_version_header", False, "missing Continuum-Version header")
            )
    except Exception as exc:
        report.results.append(WireTestResult("wire_version_header", False, str(exc)))

    # 3. OpenAPI specification (/openapi.json), kept for the schema tests
    try:
        status, body, headers = call("/openapi.json", method="GET")
        if status == 200 and str(body.get("openapi", "")).startswith("3."):
            wire = _WireSpec(body)
            report.results.append(
                WireTestResult("openapi_spec", True, f"openapi={body.get('openapi')}")
            )
        else:
            report.results.append(
                WireTestResult("openapi_spec", False, f"status={status}, body={body}")
            )
    except Exception as exc:
        report.results.append(WireTestResult("openapi_spec", False, str(exc)))

    # 4. Progress tracking (/record_progress)
    try:
        status, body, headers = call(
            "/record_progress",
            method="POST",
            payload={"run_id": run_id, "completed": 1, "total": 10, "goal": "conformance task"},
            headers=auth_headers(),
        )
        if status == 200 and body.get("completed") == 1 and body.get("total") == 10:
            report.results.append(WireTestResult("progress_tracking", True))
        else:
            report.results.append(
                WireTestResult("progress_tracking", False, f"status={status}, body={body}")
            )
    except Exception as exc:
        report.results.append(WireTestResult("progress_tracking", False, str(exc)))

    # 5. Action claim and completion (/intercept_action, /complete_action)
    action_key = f"key-conf-{int(time.time() * 1000)}"
    try:
        # First claim: must proceed
        status_claim, body_claim, _ = call(
            "/intercept_action",
            method="POST",
            payload={"run_id": run_id, "action_type": "stripe.charge", "key": action_key},
            headers=auth_headers(),
        )
        act_key = body_claim.get("action_key") or action_key
        status_comp, body_comp, _ = call(
            "/complete_action",
            method="POST",
            payload={"run_id": run_id, "action_key": act_key, "result": {"tx_id": "tx_999"}},
            headers=auth_headers(),
        )
        if (
            status_claim == 200
            and body_claim.get("proceed") is True
            and status_comp == 200
            and body_comp.get("status") == "completed"
        ):
            report.results.append(WireTestResult("action_claim_and_completion", True))
        else:
            report.results.append(
                WireTestResult(
                    "action_claim_and_completion",
                    False,
                    f"claim={body_claim}, complete={body_comp}",
                )
            )
    except Exception as exc:
        report.results.append(WireTestResult("action_claim_and_completion", False, str(exc)))

    # 6. Action deduplication and cached replay (/intercept_action)
    try:
        # Re-intercepting a completed action: must not proceed, must return cached result
        status_dedup, body_dedup, _ = call(
            "/intercept_action",
            method="POST",
            payload={"run_id": run_id, "action_type": "stripe.charge", "key": action_key},
            headers=auth_headers(),
        )
        prev = body_dedup.get("previous_result") or body_dedup.get("reusable_result") or {}
        if (
            status_dedup == 200
            and body_dedup.get("proceed") is False
            and body_dedup.get("status") == "completed"
            and prev.get("tx_id") == "tx_999"
        ):
            report.results.append(WireTestResult("action_deduplication_replay", True))
        else:
            report.results.append(
                WireTestResult(
                    "action_deduplication_replay",
                    False,
                    f"dedup_status={status_dedup}, body={body_dedup}",
                )
            )
    except Exception as exc:
        report.results.append(WireTestResult("action_deduplication_replay", False, str(exc)))

    # 7. Checkpoint and semantic resume (/checkpoint, /resume)
    try:
        status, body_cp, _ = call(
            "/checkpoint",
            method="POST",
            payload={"run_id": run_id, "reason": "conformance"},
            headers=auth_headers(),
        )
        status_res, body_res, _ = call(
            "/resume",
            method="POST",
            payload={"run_id": run_id},
            headers=auth_headers(),
        )
        if (
            status == 200
            and body_cp.get("checkpoint_id")
            and status_res == 200
            and "mode" in body_res
        ):
            report.results.append(WireTestResult("checkpoint_and_resume", True))
        else:
            report.results.append(
                WireTestResult(
                    "checkpoint_and_resume",
                    False,
                    f"cp_status={status}, res_status={status_res}",
                )
            )
    except Exception as exc:
        report.results.append(WireTestResult("checkpoint_and_resume", False, str(exc)))

    # 8. Action audit ledger query (/list_actions)
    try:
        status, body_list, _ = call(
            "/list_actions",
            method="POST",
            payload={"run_id": run_id},
            headers=auth_headers(),
        )
        if status == 200 and "actions" in body_list and "unresolved" in body_list:
            report.results.append(WireTestResult("list_actions_audit", True))
        else:
            report.results.append(
                WireTestResult("list_actions_audit", False, f"status={status}, body={body_list}")
            )
    except Exception as exc:
        report.results.append(WireTestResult("list_actions_audit", False, str(exc)))

    # 9. Canonical error schema on unknown method and bad params
    try:
        status404, body404, _ = call(
            "/unknown_method_xyz",
            method="POST",
            payload={"run_id": run_id},
            headers=auth_headers(),
        )
        # The bad-params probe deliberately violates CompleteActionRequest:
        # the request schema test below skips it, and this is the only call
        # whose 400 is the point rather than a side effect.
        status400, body400, _ = call(
            "/complete_action",
            method="POST",
            payload={"run_id": run_id},  # missing required action_key
            headers=auth_headers(),
            record_request=False,
        )
        if (
            status404 == 404
            and body404.get("code") == "method_not_found"
            and status400 == 400
            and body400.get("code") == "bad_params"
        ):
            report.results.append(WireTestResult("canonical_error_schema", True))
        else:
            report.results.append(
                WireTestResult(
                    "canonical_error_schema",
                    False,
                    f"404={body404.get('code')}, 400={body400.get('code')}",
                )
            )
    except Exception as exc:
        report.results.append(WireTestResult("canonical_error_schema", False, str(exc)))

    # 10. Confirm credential separation (/confirm)
    try:
        # A normal client token MUST be rejected on confirm
        status_conf, body_conf, _ = call(
            "/confirm",
            method="POST",
            payload={"run_id": run_id},
            headers=auth_headers(),  # passes client token
        )
        # If client auth is active on server, status_conf is 403 Forbidden
        if client_token:
            if status_conf == 403 and body_conf.get("code") == "not_authorized":
                # If confirm token was also provided, test that confirm token succeeds
                if confirm_token:
                    status_ok, body_ok, _ = call(
                        "/confirm",
                        method="POST",
                        payload={"run_id": run_id},
                        headers={"Authorization": f"Bearer {confirm_token}"},
                    )
                    if status_ok == 200:
                        report.results.append(WireTestResult("confirm_credential_separation", True))
                    else:
                        report.results.append(
                            WireTestResult(
                                "confirm_credential_separation",
                                False,
                                f"confirm with token status={status_ok}",
                            )
                        )
                else:
                    report.results.append(WireTestResult("confirm_credential_separation", True))
            else:
                report.results.append(
                    WireTestResult(
                        "confirm_credential_separation",
                        False,
                        f"client token was not rejected: status={status_conf}",
                    )
                )
        else:
            # Unauthenticated server mode: confirm succeeds
            if status_conf == 200:
                report.results.append(
                    WireTestResult("confirm_credential_separation", True, "unauthenticated mode")
                )
            else:
                report.results.append(
                    WireTestResult("confirm_credential_separation", False, f"status={status_conf}")
                )
    except Exception as exc:
        report.results.append(WireTestResult("confirm_credential_separation", False, str(exc)))

    # 11. Request schema conformance (#1895). Two separate questions, both
    # answered against the served document: does every request the suite sends
    # satisfy the request schema its endpoint declares, and does every
    # documented endpoint accept a request carrying only the fields the spec
    # marks required? The second question is what catches a handler that
    # demands a parameter the published spec does not list: a client generated
    # from that spec sends the minimal request and is refused 400.
    try:
        if wire is None:
            report.results.append(
                WireTestResult(
                    "request_schema_conformance",
                    False,
                    "no served OpenAPI document to validate requests against",
                )
            )
        else:
            failures: list[str] = []
            for path, method, _status, _body, request in observed:
                schema = wire.request_schema(path, method)
                if schema is None or request is None:
                    continue
                failures.extend(wire.validate(request, schema, f"POST {path}"))
            # Claims under explicit keys for the probes that cannot land on a
            # cold ledger: complete_action, fail_action and reconcile_action
            # all name an action that must already exist, and compensate_action
            # names one that must already be completed. A key invented only to
            # satisfy the request schema would be answered 500 for missing
            # state, which is a precondition the schema cannot express, not a
            # contract defect, so the probes settle real actions instead.
            probe_keys: dict[str, str] = {}
            for purpose in ("complete", "fail", "reconcile", "compensate"):
                status_claim, body_claim, _ = call(
                    "/intercept_action",
                    method="POST",
                    payload={
                        "run_id": run_id,
                        "action_type": f"conformance.probe.{purpose}",
                        "key": f"key-probe-{purpose}-{int(time.time() * 1000)}",
                    },
                    headers=auth_headers(),
                )
                if status_claim != 200 or not body_claim.get("action_key"):
                    failures.append(
                        f"/intercept_action: could not claim a probe action for {purpose} "
                        f"(status={status_claim})"
                    )
                else:
                    probe_keys[purpose] = str(body_claim["action_key"])
            # compensate_action is documented as the undo of an effect that
            # already completed, so the action it names is completed first.
            if "compensate" in probe_keys:
                status_done, _body_done, _ = call(
                    "/complete_action",
                    method="POST",
                    payload={
                        "run_id": run_id,
                        "action_key": probe_keys["compensate"],
                        "result": {"settled": True},
                    },
                    headers=auth_headers(),
                )
                if status_done != 200:
                    failures.append(
                        "/complete_action: could not complete the probe action the "
                        f"compensate probe names (status={status_done})"
                    )
                    probe_keys.pop("compensate")
            for path in wire.post_paths():
                schema = wire.request_schema(path, "post")
                if schema is None:
                    continue
                resolved = wire.resolve(schema)
                required = resolved.get("required") or []
                properties = resolved.get("properties") or {}
                if path == "/complete_action":
                    overrides: dict[str, Any] = {
                        "run_id": run_id,
                        "action_key": probe_keys.get("complete", "unset"),
                    }
                elif path == "/fail_action":
                    overrides = {
                        "run_id": run_id,
                        "action_key": probe_keys.get("fail", "unset"),
                        "error": "conformance probe failure",
                    }
                elif path == "/reconcile_action":
                    overrides = {
                        "run_id": run_id,
                        "action_key": probe_keys.get("reconcile", "unset"),
                    }
                elif path == "/compensate_action":
                    overrides = {
                        "run_id": run_id,
                        "action_key": probe_keys.get("compensate", "unset"),
                    }
                else:
                    overrides = {"run_id": run_id}
                # record_progress must keep moving forward for the fold, and
                # the run already reported completed=1 in test 4 above.
                if "completed" in required:
                    overrides["completed"] = 2
                payload: dict[str, Any] = {}
                for field_name in required:
                    if field_name in overrides:
                        payload[field_name] = overrides[field_name]
                    else:
                        payload[field_name] = _example_value(
                            properties.get(field_name) or {}, field_name
                        )
                failures.extend(wire.validate(payload, schema, f"POST {path}"))
                headers = auth_headers()
                expected = 200
                if path == "/confirm":
                    if confirm_token:
                        headers = {"Authorization": f"Bearer {confirm_token}"}
                    elif client_token:
                        # No confirm credential to present: the documented
                        # answer to a client token on confirm is 403.
                        expected = 403
                status, body, _ = call(path, method="POST", payload=payload, headers=headers)
                if status != expected:
                    failures.append(
                        f"{path}: request carrying only the spec's required fields "
                        f"({sorted(payload)}) was answered {status} "
                        f"({body.get('code') or body.get('error')}), expected {expected}"
                    )
            if failures:
                report.results.append(
                    WireTestResult(
                        "request_schema_conformance",
                        False,
                        "; ".join(sorted(set(failures))),
                    )
                )
            else:
                report.results.append(
                    WireTestResult(
                        "request_schema_conformance",
                        True,
                        "every documented endpoint accepted a spec-minimal request",
                    )
                )
    except Exception as exc:
        report.results.append(WireTestResult("request_schema_conformance", False, str(exc)))

    # 12. Response schema conformance (#1895). Every body the suite received
    # must satisfy the schema the served spec pairs with that path and status,
    # so a 200 that omits a field the server's own spec marks required fails
    # the suite. A status the spec does not document for that operation fails
    # it too. Paths the suite probes *because* they are undocumented, like the
    # unknown-method call in test 9, are not judged here.
    try:
        if wire is None:
            report.results.append(
                WireTestResult(
                    "response_schema_conformance",
                    False,
                    "no served OpenAPI document to validate responses against",
                )
            )
        else:
            violations: list[str] = []
            for path, method, status, body, _request in observed:
                if wire.operation(path, method) is None:
                    continue
                schema = wire.response_schema(path, method, status)
                if schema is None:
                    violations.append(
                        f"{method} {path} returned {status}, which the served "
                        f"spec does not document"
                    )
                    continue
                violations.extend(wire.validate(body, schema, f"{method} {path} {status}"))
            if violations:
                report.results.append(
                    WireTestResult(
                        "response_schema_conformance",
                        False,
                        "; ".join(sorted(set(violations))),
                    )
                )
            else:
                report.results.append(
                    WireTestResult(
                        "response_schema_conformance",
                        True,
                        "every observed response satisfied the served schema",
                    )
                )
    except Exception as exc:
        report.results.append(WireTestResult("response_schema_conformance", False, str(exc)))

    return report


def cmd_conformance(args: Any, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    """CLI entry point for ``continuum conformance``."""
    target_url = getattr(args, "url", "http://127.0.0.1:8765")
    client_token = getattr(args, "token", None)
    confirm_token = getattr(args, "confirm_token", None)

    report = run_wire_conformance(
        target_url, client_token=client_token, confirm_token=confirm_token
    )
    out.write(report.render() + "\n")
    out.flush()
    return 0 if report.passed else 1
