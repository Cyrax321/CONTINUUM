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

    # 1. Health check (/healthz)
    try:
        status, body, headers = _http_call(f"{base}/healthz", method="GET")
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
        status, body, headers = _http_call(f"{base}/healthz", method="GET")
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

    # 3. OpenAPI specification (/openapi.json)
    try:
        status, body, headers = _http_call(f"{base}/openapi.json", method="GET")
        if status == 200 and body.get("openapi", "").startswith("3."):
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
        status, body, headers = _http_call(
            f"{base}/record_progress",
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
        status_claim, body_claim, _ = _http_call(
            f"{base}/intercept_action",
            method="POST",
            payload={"run_id": run_id, "action_type": "stripe.charge", "key": action_key},
            headers=auth_headers(),
        )
        act_key = body_claim.get("action_key") or action_key
        status_comp, body_comp, _ = _http_call(
            f"{base}/complete_action",
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
        status_dedup, body_dedup, _ = _http_call(
            f"{base}/intercept_action",
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
        status, body_cp, _ = _http_call(
            f"{base}/checkpoint",
            method="POST",
            payload={"run_id": run_id, "reason": "conformance"},
            headers=auth_headers(),
        )
        status_res, body_res, _ = _http_call(
            f"{base}/resume",
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
        status, body_list, _ = _http_call(
            f"{base}/list_actions",
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
        status404, body404, _ = _http_call(
            f"{base}/unknown_method_xyz",
            method="POST",
            payload={"run_id": run_id},
            headers=auth_headers(),
        )
        status400, body400, _ = _http_call(
            f"{base}/complete_action",
            method="POST",
            payload={"run_id": run_id},  # missing required action_key
            headers=auth_headers(),
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
        status_conf, body_conf, _ = _http_call(
            f"{base}/confirm",
            method="POST",
            payload={"run_id": run_id},
            headers=auth_headers(),  # passes client token
        )
        # If client auth is active on server, status_conf is 403 Forbidden
        if client_token:
            if status_conf == 403 and body_conf.get("code") == "not_authorized":
                # If confirm token was also provided, test that confirm token succeeds
                if confirm_token:
                    status_ok, body_ok, _ = _http_call(
                        f"{base}/confirm",
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
