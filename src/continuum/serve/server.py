"""The CONTINUUM sidecar: a language-agnostic wire protocol.

This is the Tier 0 boundary from references/integration-architecture.md. Any
process, in any language, can drive CONTINUUM's recovery operations by speaking
a tiny newline-delimited JSON protocol, without embedding Python or the MCP
SDK. The protocol mirrors the MCP tool surface so the two stay in sync.

Request (one JSON object per line)::

    {"id": <any>, "method": "<name>", "params": {<kwargs>}}

Response::

    {"id": <same>, "result": {<json>}}
    {"id": <same>, "error": {"type": "<code>", "message": "<text>"}}

A line that is not a JSON object -- not valid JSON at all, or valid JSON of some
other type -- is not a request, and there is nowhere in it to read an ``id``
from. It is answered ``bad_request`` with ``"id": null``, and the session
continues (#582).

Only the core of CONTINUUM is imported here, so ``continuum serve`` does not
require the ``mcp`` extra. Authentication is a fail-closed shared secret (see
``SidecarAuth``), the same model as the MCP server's ``AuthPolicy``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, TextIO, cast

from continuum.actions.ledger import ActionLedger
from continuum.adapters.generic import GenericAgentAdapter
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import (
    ActionStatus,
    EnvironmentSnapshot,
    EnvResource,
    Origin,
    Run,
    StateStatus,
    UnknownSideEffect,
)
from continuum.recovery.contract import render_contract
from continuum.recovery.guidance import human_steps_for, self_report_guidance
from continuum.state.semantic import project
from continuum.storage import RunNotFound, Storage, open_storage

#: Every run-state write the sidecar performs is asserted by a remote caller
#: about its own work, so it is recorded as self-certified.
AGENT_SOURCE = Origin.EXTERNAL_AGENT

#: Which methods write run state, as descriptive metadata for clients that want
#: to treat a write differently from a read (confirm it, log it, retry it).
#:
#: This does **not** govern sidecar authentication, and never has: ``dispatch``
#: verifies the shared secret for *every* method, read-only ones included. The
#: name invites the opposite reading, so it is spelled out here -- a caller that
#: gates its own token on this set will be refused on ``resume``. See
#: ``SidecarServer.dispatch`` for why the sidecar is stricter than the MCP
#: server, whose policy really is mutating-only.
MUTATING = {
    "record_progress",
    "checkpoint",
    "confirm",
    "intercept_action",
    "complete_action",
    "fail_action",
    "reconcile_action",
}


class MalformedRunLog(RuntimeError):
    """A run's event log does not begin with RUN_STARTED."""


class SidecarError(Exception):
    code = "error"


class MethodNotFound(SidecarError):
    code = "method_not_found"


class NotAuthorized(SidecarError):
    code = "not_authorized"


class BadParams(SidecarError):
    code = "bad_params"


class BadRequest(SidecarError):
    """A body that could not be read as a request at all (issue #582).

    Distinct from :class:`BadParams`, which answers a request that *was* read
    and dispatched and so comes back carrying that request's ``id``. This one
    has no ``id`` to carry, and on the stdio wire it reuses the ``bad_request``
    type already answered for a line that is not JSON.
    """

    code = "bad_request"


class SidecarAuth:
    """Fail-closed shared-secret authentication for the sidecar.

    When ``expected`` is ``None`` (the default), authentication is disabled and
    the sidecar behaves as before. When set, every call must present the
    matching ``auth_token`` parameter or it is refused -- reads as well as
    writes, not only the methods in ``MUTATING``. A missing or wrong secret
    always refuses; an empty configured secret refuses rather than opening the
    door.
    """

    def __init__(self, expected: str | None = None) -> None:
        self.expected = expected

    @property
    def disabled(self) -> bool:
        return self.expected is None or self.expected == ""

    def verify(self, token: str | None) -> None:
        if self.disabled:
            return
        if not token or token != self.expected:
            raise NotAuthorized(
                "expected shared secret (set CONTINUUM_SERVE_TOKEN on the server "
                "and pass auth_token on the client)"
            )


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params or params[key] is None:
        raise BadParams(f"missing parameter {key!r}")
    return params[key]


def _env_versions(env: Any) -> dict[str, str]:
    """Normalize the two accepted ``env`` shapes to ``{resource: version}``.

    Callers send either a mapping or a list of ``name=version`` strings, and both
    the snapshot and the dependency declaration have to agree on what was pinned.
    Strict as CLI ``_environment``: an entry without ``=`` or with an empty
    version raises ``BadParams`` with the same message, so switching transports
    cannot silently ignore a typo.
    """
    versions: dict[str, str] = {}
    if isinstance(env, dict):
        for name, version in env.items():
            name_s = str(name)
            version_s = str(version)
            if not name_s or "=" in name_s:
                raise BadParams(f"--env expects name=version, got {name_s!r}")
            if not version_s:
                raise BadParams(
                    f"--env {name_s}= has an empty version; "
                    f"omit the flag entirely if the value is unknown"
                )
            versions[name_s] = version_s
    elif isinstance(env, list):
        for item in env:
            if not isinstance(item, str):
                raise BadParams(f"--env expects name=version, got {item!r}")
            name, separator, version = item.partition("=")
            if not name or not separator:
                raise BadParams(f"--env expects name=version, got {item!r}")
            if not version:
                raise BadParams(
                    f"--env {name}= has an empty version; "
                    f"omit the flag entirely if the value is unknown"
                )
            versions[name] = version
    return versions


def _environment(run_id: str, env: Any) -> EnvironmentSnapshot | None:
    if not env:
        return None
    versions = _env_versions(env)
    if not versions:
        return None
    resources = {
        name: EnvResource(name=name, version=version) for name, version in versions.items()
    }
    return capture(run_id, StaticProvider(resources))


def _declare_dependencies(server: SidecarServer, run_id: str, env: Any) -> None:
    """Record the pinned environment as declared dependencies of the run.

    A snapshot alone cannot invalidate anything: the validator decides staleness
    per declared dependency and returns early when a state has none, so a
    checkpoint carrying only a snapshot reports ``safe_to_resume`` even after the
    resource underneath it moved. Mirrors ``continuum.mcp.server`` — the two
    surfaces expose the same recovery semantics and must not disagree about
    whether drift is safe.
    """
    if not env:
        return
    versions = _env_versions(env)
    if not versions:
        return

    declared = {
        dependency.resource: dependency.version
        for dependency in project(
            run_id, server.storage.read_events(run_id), on_unprojectable="degrade"
        ).external_dependencies
    }
    for name, version in versions.items():
        if declared.get(name) == version:
            continue
        server.storage.append_event(
            run_id,
            EventType.DEPENDENCY_DECLARED,
            {"resource": name, "version": version},
            source=AGENT_SOURCE,
        )


class SidecarServer:
    """Dispatches wire-protocol methods onto CONTINUUM's core operations."""

    def __init__(self, database: str | None = None, *, storage: Storage | None = None) -> None:
        self.database = database or os.environ.get("CONTINUUM_DB") or "continuum.db"
        self.storage: Storage = storage or open_storage(self.database)
        self.adapter = GenericAgentAdapter(self.storage)
        self.auth = SidecarAuth(os.environ.get("CONTINUUM_SERVE_TOKEN") or None)

    # -- core helpers (mirror ContinuumMCP) ---------------------------------- #

    def _ensure_run(self, run_id: str, goal: str | None = None) -> Run:
        try:
            run = self.storage.get_run(run_id)
        except RunNotFound:
            if goal is None:
                raise
            run = self.storage.create_run(Run(run_id=run_id, goal=goal))
        first = self.storage.read_events(run_id, upto=1)
        if not first:
            self.storage.append_event(
                run_id, EventType.RUN_STARTED, {"goal": goal or run.goal}, source=AGENT_SOURCE
            )
        elif first[0].type is not EventType.RUN_STARTED:
            raise MalformedRunLog(
                f"run {run_id!r} does not begin with RUN_STARTED "
                f"(first event is {first[0].type.value})"
            )
        return run

    def _ledger(self, run_id: str) -> ActionLedger:
        return ActionLedger(self.storage, run_id)

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Authenticate, then route to the handler for ``method``.

        The secret is required for every method, not just the ones in
        ``MUTATING``. That is deliberate, and it is where the sidecar diverges
        from the MCP server's mutating-only policy: the MCP server talks to a
        client the user launched on their own machine, whereas any process that
        can reach this pipe can speak the protocol. Reads are worth closing
        because of what they return -- ``resume`` hands back the goal string,
        ``list_actions`` the arguments and results of external side effects.
        Gating those on the same secret costs nothing by default, since
        ``CONTINUUM_SERVE_TOKEN`` unset disables authentication entirely.

        A ``MUTATING``-only variant of this check used to exist here as an
        unreferenced helper. Restoring it would open reads to unauthenticated
        callers in exactly the deployments that bothered to set a secret, so the
        policy is pinned by tests in ``tests/test_serve.py`` (issue #95).
        """
        handler = _HANDLERS.get(method)
        if handler is None:
            raise MethodNotFound(method)
        self.auth.verify(params.get("auth_token"))
        return cast("dict[str, Any]", handler(self, params))

    # -- wire loop ---------------------------------------------------------- #

    def serve_stdio(self, instream: TextIO | None = None, outstream: TextIO | None = None) -> int:
        instream = instream or sys.stdin
        outstream = outstream or sys.stdout
        for raw in instream:
            line = raw.strip()
            if not line:
                continue
            try:
                req = _request_object(json.loads(line))
            except (json.JSONDecodeError, BadRequest) as exc:
                # One arm for both: unreadable as JSON, or readable and not an
                # object. Neither is a request, so neither answer can name an
                # id, and the session has to outlive both.
                _write(
                    outstream, {"id": None, "error": {"type": "bad_request", "message": str(exc)}}
                )
                continue
            rid = req.get("id")
            try:
                result = self.dispatch(str(req.get("method")), dict(req.get("params") or {}))
            except SidecarError as exc:
                _write(outstream, {"id": rid, "error": {"type": exc.code, "message": str(exc)}})
            except Exception as exc:  # noqa: BLE001 - report, never crash the loop
                _write(
                    outstream,
                    {
                        "id": rid,
                        "error": {"type": "internal", "message": f"{type(exc).__name__}: {exc}"},
                    },
                )
            else:
                _write(outstream, {"id": rid, "result": result})
        return 0

    def close(self) -> None:
        self.storage.close()


class SidecarHTTP:
    """HTTP transport over the sidecar dispatch (issue #238).

    POST /<method> with a JSON body is dispatched exactly like the stdio
    wire: same handlers, same token auth (``CONTINUUM_SERVE_TOKEN``), same
    errors mapped to status codes (404 unknown method, 403 unauthorized,
    400 bad params, 500 sidecar failure). Binds 127.0.0.1 by default; the
    transport exists so non-Python agents get the durability plane without
    embedding the library.
    """

    def __init__(self, sidecar: SidecarServer, port: int = 8765, bind: str = "127.0.0.1") -> None:
        import http.server

        sidecar_ref = sidecar

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _json(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                method = self.path.strip("/").split("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    params = _request_object(json.loads(raw))
                except json.JSONDecodeError as exc:
                    self._json(400, {"error": f"invalid JSON body: {exc}"})
                    return
                except BadRequest as exc:
                    # The arm now sits on the statement that raises. This
                    # refusal was already spelled out here, then escaped the
                    # handler and closed the connection unanswered (#582).
                    self._json(400, {"error": str(exc)})
                    return
                try:
                    result = sidecar_ref.dispatch(method, params)
                except MethodNotFound as exc:
                    self._json(404, {"error": str(exc)})
                    return
                except NotAuthorized as exc:
                    self._json(403, {"error": str(exc)})
                    return
                except BadParams as exc:
                    self._json(400, {"error": str(exc)})
                    return
                except SidecarError as exc:
                    self._json(500, {"error": str(exc)})
                    return
                except Exception as exc:  # storage errors (missing run etc.)
                    self._json(500, {"error": str(exc)})
                    return
                self._json(200, result)

        server = http.server.ThreadingHTTPServer((bind, port), Handler)
        self.httpd = server
        self.port = int(server.server_address[1])
        self._thread: threading.Thread | None = None

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# -- wire loop ---------------------------------------------------------- #


def _write(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, default=str) + "\n")
    stream.flush()


def _request_object(parsed: Any) -> dict[str, Any]:
    """Refuse a parsed body that is not a JSON object (issue #582).

    One check for both transports. Neither had one that worked. ``json.loads``
    returns whatever the body happened to be, and stdio simply assumed a
    mapping, reading ``.get`` off it one line above the guard whose comment
    promises to report and never crash the loop, so a single ``[]`` line killed
    a long-lived session and every request still queued behind it. The HTTP
    handler did spell out this exact refusal, but inside a ``try`` that caught
    only ``JSONDecodeError``, so it escaped ``do_POST`` and the connection closed
    with no response -- indistinguishable, to a client, from a sidecar that had
    died.
    """
    if not isinstance(parsed, dict):
        raise BadRequest("body must be a JSON object")
    return parsed


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #


def _decision_payload(decision: Any, *, goal: str) -> dict[str, Any]:
    report = decision.validation.report
    try:
        from continuum.checkpoint.context import build_recovery_context
        from continuum.state.semantic import constraint_pins_payload

        rendered = build_recovery_context(decision.state).render()
        constraint_pins = constraint_pins_payload(decision.state, rendered)
    except Exception:
        constraint_pins = {"pins": {}, "flagged": [], "grace_seconds": None}
    return {
        "checkpoint_version": report.checkpoint_version,
        "validation_reason": report.reason,
        "run_id": decision.state.run_id,
        "goal": goal,
        "mode": decision.mode.value,
        "safe": decision.safe,
        "next_allowed_action": decision.next_allowed_action,
        "human_steps": human_steps_for(decision, run_id=decision.state.run_id),
        "rationale": list(decision.rationale),
        "repairs": [
            {
                "action": step.action_name,
                "kind": step.kind.value,
                "target": step.target,
                "reason": step.reason,
                "requires_human": step.requires_human,
            }
            for step in decision.plan.steps
        ],
        "uncertain_actions": [
            {"action_id": a.action_id, "action_type": a.action_type, "status": a.status.value}
            for a in decision.uncertain_actions
        ],
        "progress": {
            "completed": decision.state.progress.completed,
            "pending": decision.state.progress.pending,
            "failed": decision.state.progress.failed,
            "total": decision.state.progress.total,
        },
        "tail_evidence": decision.tail_evidence,
        "informed_retry": getattr(decision, "informed_retry", None),
        "attempt_lessons": [
            lesson.model_dump(mode="json") for lesson in decision.state.attempt_lessons
        ],
        "contract": decision.contract.model_dump(mode="json"),
        "contract_text": render_contract(decision.contract),
        "report": decision.render(),
        "environment_changes": [d.render() for d in decision.environment_diff.breaking],
        **self_report_guidance(decision),
        "constraint_pins": constraint_pins,
    }


def _h_record_progress(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    completed = _require(params, "completed")
    total = params.get("total")
    failed = params.get("failed", 0)
    goal = params.get("goal")
    if completed < 0 or failed < 0:
        raise BadParams("progress counters must be non-negative")
    if total is not None and completed + failed > total:
        raise BadParams(f"completed ({completed}) + failed ({failed}) exceeds total ({total})")
    server._ensure_run(run_id, goal)
    payload: dict[str, Any] = {"completed": completed, "failed": failed}
    if total is not None:
        payload["total"] = total
        payload["pending"] = max(total - completed - failed, 0)
    server.storage.append_event(run_id, EventType.TASK_UPDATED, payload, source=AGENT_SOURCE)
    # Degrade, not raise (issue #383): the event is already committed by the
    # time this fold runs, so dying here would report an error while leaving
    # the write in place. Reporting the last-good figures plus the break is the
    # honest answer; the caller can see the log needs attention.
    state = project(run_id, server.storage.read_events(run_id), on_unprojectable="degrade")
    response = {
        "run_id": run_id,
        "completed": state.progress.completed,
        "pending": state.progress.pending,
        "failed": state.progress.failed,
        "total": state.progress.total,
        "source_sequence": state.source_sequence,
    }
    if state.status is StateStatus.INVALID:
        response["projection_failed_at"] = {
            "sequence": state.unprojectable_at_sequence,
            "type": state.unprojectable_event_type,
            "reason": state.unprojectable_reason,
        }
    return response


def _h_checkpoint(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    server._ensure_run(run_id)
    _declare_dependencies(server, run_id, params.get("env"))
    state = project(run_id, server.storage.read_events(run_id))
    checkpoint = server.adapter.capture_state(
        run_id,
        state,
        environment=_environment(run_id, params.get("env")),
        reason=params.get("reason", ""),
    )
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "run_id": run_id,
        "version": checkpoint.version,
        "trigger": checkpoint.trigger,
        "integrity_hash": checkpoint.integrity_hash,
        "completed": checkpoint.state.progress.completed,
        "source_sequence": checkpoint.state.source_sequence,
    }


def _h_validate(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    decision = server.adapter.resume(
        run_id,
        current_environment=_environment(run_id, params.get("env")),
        expected_model=params.get("expected_model"),
    )
    report = decision.validation.report
    try:
        from continuum.checkpoint.context import build_recovery_context
        from continuum.state.semantic import constraint_pins_payload

        rendered = build_recovery_context(decision.state).render()
        constraint_pins = constraint_pins_payload(decision.state, rendered)
    except Exception:
        constraint_pins = {"pins": {}, "flagged": [], "grace_seconds": None}
    try:
        from continuum.liveness import advisory_for_storage

        liveness = advisory_for_storage(server.storage, run_id)
    except Exception:
        liveness = {"breached": False, "silence_seconds": None, "threshold_seconds": None}
    return {
        "run_id": run_id,
        "safe": decision.safe,
        "mode": decision.mode.value,
        "checkpoint_version": report.checkpoint_version,
        "reason": report.reason,
        "components": [
            {
                "component": e.component.value,
                "component_id": e.component_id,
                "status": e.status.value,
                "detail": e.detail,
            }
            for e in report.statuses
        ],
        "environment_changes": [d.render() for d in decision.environment_diff.breaking],
        "constraint_pins": constraint_pins,
        "liveness": liveness,
    }


def _h_resume(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    # run_id is optional, exactly as it is on continuum_resume: a session that
    # was interrupted and restarted has no id to send, so an omitted one means
    # "the run that was interrupted". Reported as a mode rather than a protocol
    # error, so a client branches on mode alone across both transports.
    run_id = params.get("run_id")
    if not run_id:
        active = server.storage.get_active_run()
        if active is None:
            return {
                "run_id": None,
                "mode": "no_active_run",
                "safe": False,
                "message": (
                    "No active run to resume. Start one with "
                    "record_progress(run_id, completed, total, goal=...)."
                ),
            }
        run_id = active.run_id
    decision = server.adapter.resume(
        run_id,
        current_environment=_environment(run_id, params.get("env")),
        expected_model=params.get("expected_model"),
    )
    # The goal travels with the decision so a resumed client knows what to
    # continue without keeping its own task file. Read-only: returning the
    # self-reported goal confirms nothing, so a self-certified run still comes
    # back as request_human.
    payload = _decision_payload(decision, goal=server.storage.get_run(run_id).goal)
    try:
        from continuum.liveness import advisory_for_storage

        payload["liveness"] = advisory_for_storage(server.storage, run_id)
    except Exception:
        payload["liveness"] = {
            "breached": False,
            "silence_seconds": None,
            "threshold_seconds": None,
        }
    return payload


def _h_confirm(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    server.storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )
    decision = server.adapter.resume(run_id, expected_model=params.get("expected_model"))
    return {
        "run_id": run_id,
        "mode": decision.mode.value,
        "safe": decision.safe,
        "next_allowed_action": decision.next_allowed_action,
        "report": decision.render(),
    }


def _h_intercept_action(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    action_type = _require(params, "action_type")
    server._ensure_run(run_id)
    ledger = server._ledger(run_id)
    from continuum.actions.grants import GrantDenied, normalize_grant

    grant_clean = normalize_grant(params.get("grant"))
    try:
        outcome = ledger.claim(
            action_type,
            arguments=params.get("arguments"),
            key=params.get("key"),
            scoped_to_run=params.get("scoped_to_run", True),
            grant=grant_clean,
        )
    except UnknownSideEffect as exc:
        return {
            "run_id": run_id,
            "action_type": action_type,
            "proceed": False,
            "status": ActionStatus.UNKNOWN.value,
            "reason": str(exc),
            "guidance": (
                "A previous attempt was interrupted and its outcome is unknown. "
                "Do not retry. Verify with the external system, then report via "
                "reconcile_action."
            ),
        }
    except GrantDenied as exc:
        return {
            "run_id": run_id,
            "action_type": action_type,
            "proceed": False,
            "reason_code": "grant_denied",
            "grant_id": exc.grant_id,
            "reason": str(exc),
            "guidance": (
                "This single-use authority was already consumed (recorded in the "
                "ledger). It does not come back after a restore. Ask the operator "
                "for a fresh grant."
            ),
        }
    if outcome.fresh:
        return {
            "run_id": run_id,
            "action_type": action_type,
            "proceed": True,
            "action_key": str(outcome.key),
            "status": outcome.action.status.value,
            "guidance": "Perform the action now, then call complete_action with this action_key.",
        }
    return {
        "run_id": run_id,
        "action_type": action_type,
        "proceed": False,
        "action_key": str(outcome.key),
        "status": outcome.action.status.value,
        "external_id": outcome.external_id,
        "previous_result": dict(outcome.result) if outcome.result else None,
        "guidance": "Already performed. Reuse the previous result; do not repeat it.",
    }


def _h_complete_action(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    action_key = _require(params, "action_key")
    action = server._ledger(run_id).complete(
        action_key, external_id=params.get("external_id"), result=params.get("result")
    )
    return {
        "run_id": run_id,
        "action_id": action.action_id,
        "action_type": action.action_type,
        "status": action.status.value,
        "external_id": action.external_id,
    }


def _h_fail_action(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    action_key = _require(params, "action_key")
    error = _require(params, "error")
    action = server._ledger(run_id).fail(action_key, error, certain=params.get("certain", False))
    return {
        "run_id": run_id,
        "action_id": action.action_id,
        "status": action.status.value,
        "side_effect_uncertain": action.side_effect_uncertain,
    }


def _h_reconcile_action(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    action_key = _require(params, "action_key")
    occurred = _require(params, "occurred")
    action = server._ledger(run_id).reconcile(
        action_key,
        occurred=occurred,
        external_id=params.get("external_id"),
        note=params.get("note", ""),
    )
    return {
        "run_id": run_id,
        "action_id": action.action_id,
        "status": action.status.value,
        "external_id": action.external_id,
        "side_effect_uncertain": action.side_effect_uncertain,
    }


def _h_list_actions(server: SidecarServer, params: dict[str, Any]) -> dict[str, Any]:
    run_id = _require(params, "run_id")
    server.storage.get_run(run_id)
    ledger = server._ledger(run_id)
    actions = ledger.all()
    unresolved = {a.action_id for a in ledger.pending()}
    return {
        "run_id": run_id,
        "actions": [
            {
                "action_id": a.action_id,
                "action_type": a.action_type,
                "status": a.status.value,
                "external_id": a.external_id,
                "side_effect_uncertain": a.side_effect_uncertain,
                # The durable flag above is only set once an action has been
                # escalated to UNKNOWN, so one left STARTED by a crash reads
                # false while its outcome is in fact unresolved.
                "outcome_unresolved": a.action_id in unresolved,
            }
            for a in actions
        ],
        "unresolved": len(unresolved),
    }


_HANDLERS: dict[str, Any] = {
    "record_progress": _h_record_progress,
    "checkpoint": _h_checkpoint,
    "validate": _h_validate,
    "resume": _h_resume,
    "confirm": _h_confirm,
    "intercept_action": _h_intercept_action,
    "complete_action": _h_complete_action,
    "fail_action": _h_fail_action,
    "reconcile_action": _h_reconcile_action,
    "list_actions": _h_list_actions,
}


def list_methods() -> list[str]:
    """The methods the sidecar exposes, for tooling and docs."""
    return sorted(_HANDLERS)


__all__ = [
    "SidecarServer",
    "SidecarAuth",
    "SidecarError",
    "MethodNotFound",
    "NotAuthorized",
    "BadParams",
    "BadRequest",
    "MalformedRunLog",
    "MUTATING",
    "list_methods",
]
