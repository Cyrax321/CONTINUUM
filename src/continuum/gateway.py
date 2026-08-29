"""Enforcing HTTP gateway: claim-before-fire for outbound requests (seam 4).

The last blind spot in the durability story is the one no harness hook can
see: an agent's code making an outbound HTTP call (from Python, Rust, Bash
curl, anything). This module closes it by interposing a local proxy that the
application points at instead of the real upstream:

    app -> localhost:8765  --[claim required]--> api.example.com

Decision semantics mirror `gate` exactly (issue #217): a request matching a
registered route proceeds only when a live STARTED ledger claim exists for
its derived key; duplicates are refused because the effect already happened;
uncertain outcomes demand reconciliation first. After forwarding, the gateway
settles the claim itself - COMPLETED on 2xx/3xx, FAILED-certain on 4xx (the
upstream definitively rejected it), FAILED-uncertain on 5xx/timeouts (the
effect may or may not have landed) - and records TOOL_COMPLETED evidence
with the response status, all in the run's hash-chained log.

Configuration lives in `.continuum/gateway.json`::

    {
      "upstreams": [
        {"host": "api.example.com", "methods": ["POST"],
         "prefix": "/v1/invoices", "action_type": "send_invoice",
         "key_template": "invoice:{id}"}
      ]
    }

Key templates substitute top-level JSON body fields, identical to `gate`.
Unknown hosts are refused (fail-closed): a proxy silently forwarding
anywhere would be an open relay wearing CONTINUUM's name.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from continuum.events import EventType
from continuum.gate import normalize_key_value
from continuum.models import Origin

__all__ = [
    "DEFAULT_GATEWAY_CONFIG_PATH",
    "GatewayConfigError",
    "load_gateway_config",
    "match_route",
    "GatewayServer",
]

DEFAULT_GATEWAY_CONFIG_PATH = ".continuum/gateway.json"


class _BodyTooLarge(Exception):
    """Internal signal: the request body exceeded the configured cap."""


#: Requests larger than this are refused with 413 before the body is read.
#: A proxy that reads unbounded bodies into memory is a denial-of-service
#: surface against the very agent it protects.
MAX_BODY_BYTES = 10 * 1024 * 1024

#: Upper bound on how much we will drain-and-discard before giving up.
DRAIN_LIMIT_BYTES = 256 * 1024 * 1024


class GatewayConfigError(ValueError):
    """The gateway registry exists but cannot be honoured."""


@dataclass(frozen=True)
class Route:
    host: str
    methods: tuple[str, ...]
    prefix: str
    action_type: str
    key_template: str


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    route: Route | None = None
    key: str | None = None


def load_gateway_config(path: Path) -> list[Route]:
    """Read upstream routes. Empty list when absent; raise when malformed."""
    if not path.exists():
        return []
    # Absolute, so the message names a file the operator can open: the
    # relative form depends on the cwd of whatever loaded the registry
    # (a hook, the sidecar, a CI step). Matches gate.py per #333.
    location = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GatewayConfigError(f"{location} is not valid JSON ({exc})") from exc
    routes: list[Route] = []
    entries = raw.get("upstreams", []) if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise GatewayConfigError(f"{location}: expected {{'upstreams': [...]}}")
    for entry in entries:
        try:
            routes.append(
                Route(
                    host=str(entry["host"]),
                    methods=tuple(m.upper() for m in entry.get("methods", ("POST",))),
                    prefix=str(entry.get("prefix", "/")),
                    action_type=str(entry["action_type"]),
                    key_template=str(entry["key_template"]),
                )
            )
        except KeyError as exc:
            raise GatewayConfigError(f"{location}: upstream missing required field {exc}") from exc
    return routes


def render_key(template: str, body: dict[str, Any]) -> str:
    """Substitute ``{field}`` placeholders from the request body.

    Values are normalised exactly as ``gate`` normalises tool arguments
    (:func:`continuum.gate.normalize_key_value`): the proxy and the hook must
    derive the same key for the same operation, or a call claimed through one
    seam looks unclaimed at the other.
    """
    import string

    fields = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    missing = [f for f in fields if f not in body]
    if missing:
        raise GatewayConfigError(f"key template {template!r} needs body field(s) {missing}")
    return template.format(**{f: normalize_key_value(body[f]) for f in fields})


def match_route(
    routes: list[Route],
    *,
    host: str,
    method: str,
    body: dict[str, Any],
    actions_by_key: dict[str, Any],
    run_id: str,
) -> Decision:
    """The gateway's verdict for one request, mirroring gate's table."""
    from continuum.actions.idempotency import idempotency_key
    from continuum.models import ActionStatus

    candidates = [r for r in routes if r.host == host]
    if not candidates:
        return Decision(False, f"no upstream registered for host {host!r}")

    route = next((r for r in candidates if method.upper() in r.methods), None)
    if route is None:
        return Decision(
            False,
            f"host {host!r} is registered but {method} is not among its allowed "
            f"methods {[m.lower() for m in candidates[0].methods]}",
        )

    try:
        rendered = render_key(route.key_template, body)
    except GatewayConfigError as exc:
        return Decision(False, f"gateway configuration error: {exc}")

    key = str(idempotency_key(route.action_type, None, scope=run_id, key=rendered))
    action = actions_by_key.get(key)
    if action is None or action.action_type != route.action_type:
        return Decision(
            False,
            f"side effect {route.action_type!r} key {rendered!r} has no ledger claim. "
            f"Call continuum_intercept_action(run_id={run_id!r}, "
            f"action_type={route.action_type!r}, key={rendered!r}) first.",
            route=route,
        )
    if action.status is ActionStatus.STARTED:
        return Decision(True, "live claim", route=route, key=key)
    if action.status is ActionStatus.COMPLETED:
        return Decision(
            False,
            f"{route.action_type!r} {rendered!r} already completed; refusing duplicate",
            route=route,
        )
    if action.status is ActionStatus.UNKNOWN:
        return Decision(
            False,
            f"{route.action_type!r} {rendered!r} outcome unknown; reconcile first "
            f"(continuum_reconcile_action)",
            route=route,
        )
    return Decision(
        False,
        f"previous attempt closed ({action.status.value}); claim again before retrying",
        route=route,
    )


class GatewayServer:
    """Threaded local proxy enforcing claims for registered upstreams."""

    def __init__(
        self,
        storage_factory: Any,
        run_id: str | None,
        routes: list[Route],
        port: int = 0,
    ) -> None:
        self._storage_factory = storage_factory
        self._run_id = run_id
        self._routes = routes
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence test noise
                pass

            def _body(self, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length > max_bytes:
                    # Drain (without buffering) so the client can finish
                    # writing and read our 413, instead of dying on a broken
                    # pipe mid-send. Refuse to drain beyond a sanity bound.
                    drained = 0
                    while drained < length:
                        chunk = self.rfile.read(min(1024 * 1024, length - drained))
                        if not chunk:
                            break
                        drained += len(chunk)
                        if drained > DRAIN_LIMIT_BYTES:
                            self.close_connection = True
                            self._respond(
                                413,
                                {"error": "request body too large to drain"},
                            )
                            raise _BodyTooLarge
                    self._respond(
                        413,
                        {"error": f"request body exceeds {max_bytes} bytes"},
                    )
                    raise _BodyTooLarge
                raw = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw) if raw else {}
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    return {}

            def _respond(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                if getattr(self, "close_connection", False):
                    self.send_header("Connection", "close")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                host = self.headers.get("Host", "")
                try:
                    body = self._body()
                except _BodyTooLarge:
                    return

                # Resolve the run lazily so hooks can precede any explicit start.
                run_id = server._run_id
                storage = server._storage_factory()
                try:
                    if run_id is None:
                        active = storage.get_active_run()
                        run_id = active.run_id if active else None
                    if run_id is None:
                        self._respond(403, {"error": "no active CONTINUUM run"})
                        return
                    from continuum.actions.ledger import fold_action_events

                    actions = fold_action_events(storage.read_events(run_id))
                    decision = match_route(
                        server._routes,
                        host=host.split(":")[0],
                        method=method,
                        body=body,
                        actions_by_key=actions,
                        run_id=run_id,
                    )
                    if not decision.allow or decision.route is None or decision.key is None:
                        self._respond(
                            403, {"error": "denied by CONTINUUM gateway", "reason": decision.reason}
                        )
                        return

                    from urllib.parse import urlsplit

                    from continuum.actions.ledger import ActionLedger

                    parts = urlsplit(f"https://{decision.route.host}{self.path}")
                    import http.client as http_client

                    scheme = "https"
                    conn: Any = http_client.HTTPSConnection(parts.netloc, timeout=30)
                    headers = {
                        k: v
                        for k, v in self.headers.items()
                        if k.lower() not in ("host", "content-length")
                    }
                    payload = json.dumps(body).encode() if body else None
                    if payload is not None:
                        headers["Content-Type"] = "application/json"
                        headers["Content-Length"] = str(len(payload))
                    try:
                        conn.request(method, self.path, body=payload, headers=headers)
                        resp = conn.getresponse()
                        resp_body = resp.read()
                        status = resp.status
                    except OSError as exc:
                        ledger = ActionLedger(storage, run_id)
                        ledger.fail(decision.key, f"network error: {exc}", certain=False)
                        self._respond(
                            502, {"error": "upstream unreachable", "detail": str(exc)[:200]}
                        )
                        return
                    finally:
                        close = getattr(conn, "close", None)
                        if close:
                            close()

                    ledger = ActionLedger(storage, run_id)
                    if status < 400:
                        ledger.complete(
                            decision.key, external_id=f"{method} {self.path} -> {status}"
                        )
                        storage.append_event(
                            run_id,
                            EventType.TOOL_COMPLETED,
                            {
                                "tool": "http",
                                "path": f"{scheme}://{parts.netloc}{self.path}",
                                "status": status,
                                "via": "gateway",
                            },
                            source=Origin.EXTERNAL_AGENT,
                        )
                    elif status < 500:
                        ledger.fail(decision.key, f"upstream rejected: HTTP {status}", certain=True)
                    else:
                        ledger.fail(
                            decision.key, f"upstream server error: HTTP {status}", certain=False
                        )

                    self.send_response(status)
                    self.send_header(
                        "Content-Type", resp.getheader("Content-Type", "application/json")
                    )
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                finally:
                    close_storage = getattr(storage, "close", None)
                    if close_storage:
                        close_storage()

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_PATCH(self) -> None:  # noqa: N802
                self._handle("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = int(self.httpd.server_address[1])

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        threading.Thread(target=self.httpd.shutdown).start()
        self.httpd.server_close()
