"""Minimal dashboard over CONTINUUM runs.

Renders run state, validation outcomes, recovery contracts, and events as
HTML. It is a presentation layer only, using the same Storage, CheckpointManager
and RecoveryEngine the CLI uses.
"""

from __future__ import annotations

import html
import http.server
import socketserver
from typing import Any

from continuum.recovery import RecoveryEngine
from continuum.storage.base import Storage

MAX_DASHBOARD_BODY = 1 * 1024 * 1024

#: Upper bound on how much to drain before giving up, matching gateway (#317).
DASHBOARD_DRAIN_LIMIT_BYTES = 256 * 1024 * 1024


def render_dashboard_html(storage: Storage) -> str:
    """Render the run index: one row per run with its recovery verdict.

    Each row assesses recovery independently, and an assessment that raises is
    shown as ``error: ...`` in that row rather than failing the whole page: an
    unreadable run must not hide the readable ones.
    """
    runs = storage.list_runs()
    rows: list[str] = []
    for run in runs:
        engine = RecoveryEngine(storage)
        try:
            decision = engine.assess(run.run_id)
            mode = html.escape(decision.mode.value)
            safe = "yes" if decision.safe else "no"
        except Exception as exc:
            mode = html.escape(f"error: {exc}")
            safe = "unknown"
        rows.append(
            f"<tr><td>{html.escape(run.run_id)}</td>"
            f"<td>{html.escape(run.goal)}</td>"
            f"<td>{html.escape(run.status.value)}</td>"
            f"<td>{mode}</td><td>{safe}</td></tr>"
        )
    body = "\n".join(rows) if rows else "<tr><td colspan=5>No runs</td></tr>"
    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>CONTINUUM Dashboard</title></head>
<body><h1>CONTINUUM Dashboard</h1>
<table border=\"1\" cellpadding=\"6\"><tr><th>Run</th><th>Goal</th><th>Status</th><th>Recovery</th><th>Safe</th></tr>
{body}
</table></body></html>"""


def _run_exists(storage: Storage, run_id: str) -> bool:
    """Whether the run is real, so the handler can answer 404 rather than 200."""
    from continuum.storage import RunNotFound

    try:
        storage.get_run(run_id)
    except RunNotFound:
        return False
    return True


def _advisory_trust_html(storage: Storage, run_id: str) -> str:
    """Small read-only advisory display for prefix trust (issue #401)."""
    try:
        from continuum.analysis.prefix_trust import trust_over_prefix
        from continuum.state.semantic import project

        # Use the latest version if present, else project the raw events
        state = storage.latest_version(run_id)
        if state is None:
            try:
                state = project(run_id, storage.read_events(run_id))
            except Exception:
                return ""
        advisory = trust_over_prefix(state)
        breakdown = advisory.get("breakdown", {})
        score = advisory.get("trust_score", 1.0)
        return (
            f'<div style="margin:8px 0;padding:8px;border:1px solid #ccc">'
            f"<b>Advisory prefix trust:</b> {score:.3f} "
            f"(role={breakdown.get('role', 1.0):.3f} "
            f"goal={breakdown.get('goal', 1.0):.3f} "
            f"evidence={breakdown.get('evidence', 1.0):.3f})"
            f"</div>"
        )
    except Exception:
        return ""


def _pins_html(storage: Storage, run_id: str) -> str:
    """Read-only advisory display for constraint pins (issue #419)."""
    try:
        from continuum.checkpoint.context import build_recovery_context
        from continuum.state.semantic import constraint_pins_payload, project

        state = storage.latest_version(run_id)
        if state is None:
            try:
                state = project(run_id, storage.read_events(run_id))
            except Exception:
                return ""
        if not state.pins:
            return ""
        ctx = build_recovery_context(state).render()
        block = constraint_pins_payload(state, ctx)
        pins = block.get("pins", {})
        flagged = block.get("flagged", [])
        if not pins:
            return ""
        rows = ""
        for pin_id in sorted(pins.keys()):
            info = pins[pin_id]
            status = html.escape(str(info.get("status", "")))
            prefix = html.escape(str(info.get("sha256_prefix", "")))
            is_flagged = " flagged" if pin_id in flagged else ""
            rows += (
                f"<tr><td>{html.escape(pin_id)}</td>"
                f"<td>{status}</td><td>{prefix}</td><td>{is_flagged}</td></tr>"
            )
        flagged_str = ", ".join(html.escape(p) for p in flagged) if flagged else "none"
        return (
            f'<div style="margin:8px 0;padding:8px;border:1px solid #c00">'
            f"<b>Constraint pins:</b> flagged: {flagged_str}"
            f'<table border="1" cellpadding="4"><tr><th>Pin</th><th>Status</th>'
            f"<th>Prefix</th><th>Flag</th></tr>{rows}</table></div>"
        )
    except Exception:
        return ""


def render_run_detail_html(storage: Storage, run_id: str) -> str:
    """Render one run: contract, validation, advisories, HITL controls, events.

    Only the last 20 events are shown, with a pointer to ``continuum events``
    for the full log. A run that cannot be read yields a "Run not found" page;
    the handler pairs that body with 404 rather than 200.
    """
    try:
        run = storage.get_run(run_id)
    except Exception as exc:
        return f"""<!doctype html><html><body><h1>Run not found</h1><p>{html.escape(str(exc))}</p></body></html>"""
    engine = RecoveryEngine(storage)
    decision = None
    try:
        decision = engine.assess(run_id)
        contract_html = html.escape(decision.contract.model_dump_json(indent=2))
        validation_rows = "".join(
            f"<tr><td>{html.escape(e.component.value)}</td><td>{html.escape(e.status.value)}</td><td>{html.escape(e.detail or '')}</td></tr>"
            for e in decision.validation.report.statuses
        )
        ledger_html = f"<pre>{contract_html}</pre>"
        validation_html = f'<table border="1" cellpadding="4"><tr><th>Component</th><th>Status</th><th>Detail</th></tr>{validation_rows}</table>'
        advisory_html = _advisory_trust_html(storage, run_id)
        pins_html = _pins_html(storage, run_id)
    except Exception as exc:
        ledger_html = f"<p>{html.escape(str(exc))}</p>"
        validation_html = ""
        advisory_html = ""
        pins_html = ""
    events = storage.read_events(run_id)
    try:
        archived = storage.read_archived_events(run_id)
    except Exception:
        archived = []
    total = len(events) + len(archived)
    hint = (
        f"<p>Showing last 20 of {total} events, see continuum events {html.escape(run_id)} for full log.</p>"
        if total > 20
        else ""
    )
    events_rows = "".join(
        f"<tr><td>{e.sequence}</td><td>{html.escape(e.type.value)}</td><td>{html.escape(str(e.payload))}</td></tr>"
        for e in events[-20:]
    )
    events_html = f'{hint}<table border="1" cellpadding="4"><tr><th>Seq</th><th>Type</th><th>Payload</th></tr>{events_rows}</table>'

    # Human-in-the-loop surface (issue #242): buttons only when there is
    # something a person can actually settle.
    hitl_html = ""
    try:
        from continuum.dashboard.hitl import pending_actions_with_keys

        pending = pending_actions_with_keys(storage, run_id)
        needs_confirm = decision is not None and decision.mode.value == "request_human"
        if pending or needs_confirm:
            rows = []
            if needs_confirm:
                rows.append(
                    '<form method="post" action="/action/confirm">'
                    f'<input type="hidden" name="run_id" value="{html.escape(run_id)}">'
                    '<button type="submit">Confirm goal + progress (human review done)</button>'
                    "</form>"
                )
            for key, action in pending:
                esc_key = html.escape(key)
                rows.append(
                    f'<div style="margin:6px 0"><code>{esc_key}</code> '
                    f"{html.escape(action.action_type)} is "
                    f"<b>{html.escape(action.status.value)}</b><br>"
                    '<form method="post" action="/action/reconcile" '
                    'style="display:inline">'
                    f'<input type="hidden" name="run_id" value="{html.escape(run_id)}">'
                    f'<input type="hidden" name="ledger_key" value="{esc_key}">'
                    '<input type="hidden" name="occurred" value="true">'
                    '<button type="submit">Settle: it DID happen</button></form> '
                    '<form method="post" action="/action/reconcile" '
                    'style="display:inline">'
                    f'<input type="hidden" name="run_id" value="{html.escape(run_id)}">'
                    f'<input type="hidden" name="ledger_key" value="{esc_key}">'
                    '<input type="hidden" name="occurred" value="false">'
                    '<button type="submit">Settle: it did NOT happen</button></form>'
                    "</div>"
                )
            hitl_html = "<h2>Human-in-the-loop</h2>" + "".join(rows)

        complete_html = ""
        if run.status.value != "completed":
            complete_html = (
                '<form method="post" action="/action/complete">'
                f'<input type="hidden" name="run_id" value="{html.escape(run_id)}">'
                '<input name="summary" placeholder="closing summary (optional)">'
                '<button type="submit">Mark completed</button></form>'
            )
            hitl_html += complete_html
    except Exception as exc:  # presentation must not crash the page
        hitl_html = f"<p>[hitl unavailable: {html.escape(str(exc))}]</p>"

    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Run {html.escape(run_id)}</title></head>
<body><h1>Run {html.escape(run_id)}</h1>
<p>Goal: {html.escape(run.goal)} | Status: {html.escape(run.status.value)}</p>
{advisory_html}
{pins_html}
{hitl_html}
<h2>Contract</h2>{ledger_html}
<h2>Validation</h2>{validation_html}
<h2>Recent events</h2>{events_html}
<p><a href=\"/\">Back to dashboard</a></p></body></html>"""


def make_dashboard_server(
    storage: Storage, port: int = 8000, host: str = "127.0.0.1"
) -> socketserver.ThreadingTCPServer:
    """Construct the dashboard HTTP server bound to ``host``.

    Defaults to loopback (#270): the dashboard renders recovery contracts
    with goals and side-effect details, which must not be reachable from
    off-host by default. Operators who understand the exposure can pass
    ``--host 0.0.0.0`` explicitly.

    POST bodies are capped at 1 MB (#317) to prevent unbounded reads. Bodies
    exceeding the cap are drained up to 256 MB (matching gateway) and answered
    with 413, so a client can finish writing and read the refusal instead of
    dying on a broken pipe.
    """
    import urllib.parse

    from continuum.dashboard.hitl import (
        HitlUnauthorized,
        authorize_hitl,
        complete_run,
        confirm_run,
        reconcile_action,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Drop the stdlib access log: the event log is the record of truth."""
            return

        def _html(self, content: str, code: int = 200) -> None:
            """Answer with one HTML body and an explicit ``Content-Length``.

            ``Connection: close`` is advertised only when the handler already
            decided to close, so a refusal that left the request body unread
            does not promise keep-alive it cannot honour.
            """
            body = content.encode("utf-8")
            self.send_response(code)
            if getattr(self, "close_connection", False):
                self.send_header("Connection", "close")
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            """Serve the run index, or one run's detail page under ``/runs/<id>``."""
            if self.path.startswith("/runs/"):
                run_id = self.path.split("/runs/")[1].split("?")[0].split("/")[0]
                content = render_run_detail_html(storage, run_id)
                # A run nobody has ever written to must not answer 200. The body
                # already says "Run not found", but the status is what anything
                # other than a human reads, and the CLI holds the same line: a
                # typo'd run name never looks like a clean bill of health.
                code = 200 if _run_exists(storage, run_id) else 404
                self._html(content, code=code)
                return
            self._html(render_dashboard_html(storage))

        def do_POST(self) -> None:  # noqa: N802
            """Apply one human-in-the-loop action: confirm, reconcile, or complete.

            The body is form-encoded and capped at ``MAX_DASHBOARD_BODY``; an
            oversized one is drained (up to the drain limit) so the client can
            read the 413 instead of hitting a broken pipe. Every action is token
            gated, and a rejected token answers 403 before anything is written.
            An action that raises answers 400: settling a claim is a write, and
            a half-applied one must be visible rather than reported as success.
            """
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_DASHBOARD_BODY:
                drained = 0
                while drained < length:
                    chunk = self.rfile.read(min(1024 * 1024, length - drained))
                    if not chunk:
                        break
                    drained += len(chunk)
                    if drained > DASHBOARD_DRAIN_LIMIT_BYTES:
                        self.close_connection = True
                        self._html(
                            "<h1>413 Body too large</h1><p>request body too large to drain</p>",
                            code=413,
                        )
                        return
                self._html(
                    f"<h1>413 Body too large</h1><p>request body exceeds {MAX_DASHBOARD_BODY} bytes</p>",
                    code=413,
                )
                return
            raw = self.rfile.read(length) if length else b""
            form = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
            token = form.get("token") or self.headers.get("X-Dashboard-Token")

            run_id = form.get("run_id", "")

            def ok(msg: str) -> None:
                """Answer 200 with ``msg`` above the refreshed run detail."""
                detail = render_run_detail_html(storage, run_id) if run_id else ""
                self._html(
                    f"<p>[ok] {html.escape(msg)}</p>{detail}"
                    '<p><a href="/">Back to dashboard</a></p>',
                )

            try:
                authorize_hitl(token)
            except HitlUnauthorized as exc:
                self._html(
                    f"<!doctype html><html><body><h1>403 Forbidden</h1>"
                    f"<p>{html.escape(str(exc))}</p>"
                    '<p><a href="/">Back</a></p></body></html>',
                    code=403,
                )
                return

            action = self.path.strip("/").split("?")[0]
            try:
                if action == "action/confirm":
                    confirm_run(storage, run_id)
                    ok("goal and progress confirmed (REVIEW_CONFIRMED)")
                elif action == "action/reconcile":
                    key = form.get("ledger_key", "")
                    occurred = form.get("occurred") == "true"
                    reconcile_action(
                        storage,
                        run_id,
                        key,
                        occurred=occurred,
                        external_id=form.get("external_id") or None,
                    )
                    ok(f"reconciled {key[:16]}... occurred={occurred}")
                elif action == "action/complete":
                    complete_run(storage, run_id, summary=form.get("summary", ""))
                    ok("run completed")
                else:
                    self._html("<h1>404 Not Found</h1>", code=404)
            except Exception as exc:
                self._html(
                    f"<!doctype html><html><body><h1>400 Bad Request</h1>"
                    f"<pre>{html.escape(str(exc))}</pre>"
                    '<p><a href="/">Back</a></p></body></html>',
                    code=400,
                )

    server_class = socketserver.ThreadingTCPServer
    server_class.allow_reuse_address = True
    server = server_class((host, port), Handler)
    return server


def serve_dashboard(storage: Storage, port: int = 8000, host: str = "127.0.0.1") -> None:
    """Serve the dashboard until interrupted, closing the socket on the way out.

    Blocks the calling thread. See :func:`make_dashboard_server` for the
    loopback default and the body cap.
    """
    httpd = make_dashboard_server(storage, port=port, host=host)
    print(f"Serving dashboard at http://{host}:{port}")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
