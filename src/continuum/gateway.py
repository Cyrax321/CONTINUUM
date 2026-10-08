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
anywhere would be an open relay wearing CONTINUUM's name. The route prefix is
enforced the same way -- a live claim for ``/v1/invoices`` does not authorise
``/v1/refunds`` on the same host, because the prefix is the only per-path
scope a route has (issue #1051).

A route's upstream scheme is ``https`` unless the route says otherwise, so a
registry written before schemes existed keeps the transport it was built for.
``http://`` on the host, or a ``scheme`` field naming it, reaches an upstream
that does not terminate TLS itself: a local service, an internal address behind
a TLS terminator. The scheme is kept off the host, which stays a bare authority,
and it selects the connection the proxy opens -- an ``http`` route read as
``https`` was unreachable at all, the TLS handshake to its cleartext port
failing before a byte was forwarded.
"""

from __future__ import annotations

import json
import posixpath
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from continuum.events import EventType
from continuum.gate import is_memory_key, is_memory_template, normalize_key_value
from continuum.models import Origin

__all__ = [
    "DEFAULT_GATEWAY_CONFIG_PATH",
    "GatewayConfigError",
    "Route",
    "Decision",
    "render_key",
    "load_gateway_config",
    "load_gateway_tenant",
    "match_route",
    "GatewayServer",
]

DEFAULT_GATEWAY_CONFIG_PATH = ".continuum/gateway.json"


class _BodyTooLarge(Exception):
    """Internal signal: the request body exceeded the configured cap."""


class _MalformedBody(Exception):
    """Internal signal: the request body could not be read as JSON (issue #323)."""


#: Requests larger than this are refused with 413 before the body is read.
#: A proxy that reads unbounded bodies into memory is a denial-of-service
#: surface against the very agent it protects.
MAX_BODY_BYTES = 10 * 1024 * 1024

#: Upper bound on how much we will drain-and-discard before giving up.
DRAIN_LIMIT_BYTES = 256 * 1024 * 1024

#: Replies larger than this are refused with 502 rather than buffered whole.
#: The request cap bounds what a client can make the proxy hold; this bounds
#: what an upstream can. Without it a hostile or merely broken upstream sends
#: a large body and holds the agent's own proxy and memory hostage, which is
#: the DoS docs/threat_model.md describes as fended off (#1055).
MAX_RESPONSE_BYTES = MAX_BODY_BYTES


class GatewayConfigError(ValueError):
    """The gateway registry exists but cannot be honoured."""


def _read_bounded_response(resp: Any, cap: int) -> bytes | None:
    """Read an upstream reply, or refuse it once it passes ``cap``.

    Returns the reply bytes when they fit and ``None`` when they do not, so
    the caller answers with a 502 instead of echoing an oversized body.
    ``None`` is also the answer to a reply that declares no length at all and
    streams past the cap, which is why the bound is checked against the
    running total and not only against a declared one: a chunked reply has
    no length to check up front, and reading it whole is precisely the
    unbounded allocation the cap exists to prevent.
    """
    declared = getattr(resp, "length", None)
    if declared is not None and declared > cap:
        return None
    buffer = bytearray()
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            return bytes(buffer)
        buffer += chunk
        if len(buffer) > cap:
            return None


@dataclass(frozen=True)
class Route:
    host: str
    methods: tuple[str, ...]
    prefix: str
    action_type: str
    key_template: str
    #: ``https`` by default, so a registry written before schemes existed keeps
    #: its meaning. ``http`` reaches an upstream that terminates TLS elsewhere
    #: or never had it -- a local service on plain HTTP, an internal address
    #: behind a TLS-terminating load balancer.
    scheme: str = "https"


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    route: Route | None = None
    key: str | None = None


_ALLOWED_SCHEMES = ("http", "https")


def _split_scheme(location: Path, entry: Mapping[str, Any]) -> tuple[str, str]:
    """Read one entry's ``(host, scheme)``, accepting either spelling.

    A registry written before schemes existed has a bare ``host`` and no
    ``scheme``, and means https, which the module was built around. Both ways
    of naming a plain-HTTP upstream are accepted because they read naturally to
    different people: a ``scheme`` key beside the fields it belongs with, and a
    ``http://`` prefix on the host, which is how the address is written
    everywhere else it appears (a browser, ``curl``, an env var). An entry that
    gives both has to agree with itself, or the file names two different
    upstreams in one place and the operator cannot tell which one would run.

    The host is stored bare either way: every consumer downstream -- the
    collision check, ``match_route``, the connection's ``netloc`` -- splits or
    compares ``host`` as a bare authority, and a scheme left on it would make
    ``_normalize_host`` see ``http`` as the name.
    """
    host = str(entry["host"])
    declared = entry.get("scheme")
    if declared is not None:
        declared = str(declared).strip().lower()
        if declared not in _ALLOWED_SCHEMES:
            raise GatewayConfigError(
                f"{location}: upstream scheme {declared!r} is not one of {list(_ALLOWED_SCHEMES)}"
            )
    # Only a ``scheme://`` prefix is read as one. ``urlsplit`` would read
    # ``a.com:8443`` as scheme ``a.com``, so a bare host that spells its port --
    # the ordinary way to name a non-default upstream -- would be refused as an
    # unknown scheme for naming a destination.
    prefix, sep, rest = host.partition("://")
    if not sep:
        return host, declared if declared is not None else "https"
    found = prefix.strip().lower()
    if found not in _ALLOWED_SCHEMES:
        raise GatewayConfigError(
            f"{location}: upstream host {host!r} uses scheme {found!r}, "
            f"which is not one of {list(_ALLOWED_SCHEMES)}"
        )
    if declared is not None and declared != found:
        raise GatewayConfigError(
            f"{location}: upstream {host!r} says scheme {found!r} while its "
            f"'scheme' field says {declared!r}"
        )
    if not rest:
        raise GatewayConfigError(f"{location}: upstream host {host!r} names a scheme but no host")
    return rest, found


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
            kt = str(entry["key_template"])
            if is_memory_template(kt):
                import string as _string

                fields = {name for _, name, _, _ in _string.Formatter().parse(kt) if name}
                has_tenant = "tenant" in fields or "tenant_id" in fields
                required = (
                    ("store_id", "namespace", "record_key")
                    if kt.startswith("memory:")
                    else ("store_id", "record_key")
                )
                missing = [f for f in required if f not in fields]
                if not has_tenant:
                    missing.append("tenant")
                if missing:
                    raise GatewayConfigError(
                        f"{location}: upstream key template {kt!r} missing required placeholder(s): {', '.join(missing)}"
                    )
            host, scheme = _split_scheme(location, entry)
            routes.append(
                Route(
                    host=host,
                    methods=tuple(m.upper() for m in entry.get("methods", ("POST",))),
                    prefix=str(entry.get("prefix", "/")),
                    action_type=str(entry["action_type"]),
                    key_template=kt,
                    scheme=scheme,
                )
            )
        except KeyError as exc:
            raise GatewayConfigError(f"{location}: upstream missing required field {exc}") from exc
    _reject_colliding_routes(location, routes)
    return routes


def _reject_colliding_routes(location: Path, routes: list[Route]) -> None:
    """Refuse two routes the matcher cannot tell apart.

    ``match_route`` selects by (host, port, prefix) and then by method, so two
    routes that agree on all three and share a method are indistinguishable and
    registry order would silently pick one -- the wrong-claim class issue #1341
    closed elsewhere, except here no ordering can repair it. Fail at load time,
    where the operator still has the file open, instead of routing live traffic
    to whichever upstream happened to be listed first.
    """
    seen: dict[tuple[str, str | None, str, str], str] = {}
    for route in routes:
        name, port = _normalize_host(route.host, route.scheme)
        for method in route.methods:
            key = (name, port, _normalize_path(route.prefix), method)
            earlier = seen.get(key)
            if earlier is not None:
                raise GatewayConfigError(
                    f"{location}: upstream {route.host!r} repeats {method} on "
                    f"prefix {route.prefix!r} already served by {earlier!r}; "
                    f"drop one or give them different hosts, ports or prefixes"
                )
            seen[key] = route.host


def load_gateway_tenant(path: Path) -> str | None:
    """Read optional bound tenant from gateway config.

    When present, memory-store routes (``mem:`` or ``memory:``) are tenant-scoped: a
    request whose tenant field does not match the bound identity is
    denied at the gateway rather than surfacing later as a breach. This
    is configuration and a check, not new infrastructure (issue #566,
    parent #304, issue #1415).
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    bound = raw.get("bound_tenant") or raw.get("tenant") or raw.get("tenant_id")
    if isinstance(bound, str) and bound.strip():
        return bound.strip()
    return None


def render_key(template: str, body: dict[str, Any]) -> str:
    """Substitute ``{field}`` placeholders from the request body.

    Values are normalised exactly as ``gate`` normalises tool arguments
    (:func:`continuum.gate.normalize_key_value`): the proxy and the hook must
    derive the same key for the same operation, or a call claimed through one
    seam looks unclaimed at the other.

    A memory template's segments are colon-delimited
    (``mem:{store_id}:{tenant}:{record_key}``), so a placeholder value that
    itself contains a colon shifts the segments and defeats the positional
    tenant check in :func:`match_route` -- a caller controlling ``store_id``
    could make ``parts[2]`` read as the bound tenant while the real ``tenant``
    was something else (#1149). Such a value is rejected at the boundary so the
    flattened key can only ever be re-parsed one way. Only the placeholders
    before the terminal segment are guarded: a colon in the last field cannot
    move ``parts[2]`` and is re-parsed downstream as ``":".join(parts[3:])``,
    so a record key like ``doc:section:1`` stays valid.
    """
    import string

    fields = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    missing = [f for f in fields if f not in body]
    if missing:
        raise GatewayConfigError(f"key template {template!r} needs body field(s) {missing}")
    values = {f: normalize_key_value(body[f]) for f in fields}
    if is_memory_template(template):
        # Only the placeholders before the terminal segment can shift the
        # positions, and the check reads the formatted value rather than only
        # ``str`` ones: ``str.format`` renders a non-string such as
        # ``["x:acme:"]`` as ``['x:acme:']``, which carries the same shifting
        # colon without ever being a string.
        guarded = set(fields[:-1])
        for field, value in values.items():
            if field in guarded and ":" in str(value):
                raise GatewayConfigError(
                    f"memory template {template!r} field {field!r} must not contain ':' "
                    f"(it would shift the key's colon-delimited segments), got {value!r}"
                )
    return template.format(**values)


def _normalize_path(raw: str) -> str:
    """The path the upstream will route on, as the gateway sees ``raw``.

    Strips the query and fragment (a prefix is a path scope, not a query
    scope) and collapses ``.``/``..`` segments, because the upstream rewrites
    ``/v1/invoices/../refunds`` into ``/v1/refunds`` before it dispatches and
    the gateway's refusal has to be about the path that is actually served
    (issue #1051). Percent-encoding is decoded first for the same reason: the
    upstream decodes before it routes, so ``/v1/invoices/%2e%2e/refunds``
    reaches ``/v1/refunds`` and has to be judged as that path. Decoding can
    only make the comparison see more of the path, so it fails closed. A
    request line without a leading slash is made absolute so the comparison is
    always between two absolute paths.
    """
    from urllib.parse import unquote

    path = unquote(urlsplit(raw).path)
    if not path.startswith("/"):
        path = f"/{path}"
    # normpath keeps the root and drops a trailing slash, so both sides of the
    # comparison land on one canonical spelling per path.
    collapsed = posixpath.normpath(path)
    return collapsed or "/"


#: The port each scheme treats as its default. A route registered without a
#: port and a client that spells the default explicitly are one destination,
#: whichever side omits it.
_DEFAULT_PORTS = {"http": "80", "https": "443"}

#: Both schemes' defaults, for the request side: a ``Host`` header carries no
#: scheme, so a client's explicit ``:80`` and ``:443`` must both fold to reach
#: whichever upstream the route turned out to be.
_ANY_DEFAULT_PORT = frozenset(_DEFAULT_PORTS.values())


def _normalize_host(host: str, scheme: str = "https") -> tuple[str, str | None]:
    """Canonical ``(name, port)`` for route matching: name case-folded, port kept.

    Two rules, one from each half of issue #1342.

    HTTP host names are case-insensitive (RFC 7230 §5.4, and DNS before it),
    so the name is folded on both sides or a client that sends
    ``Host: API.EXAMPLE.COM`` against a route registered as ``api.example.com``
    is refused for a spelling the protocol says is not one.

    The port, though, is part of the destination, and dropping it from the
    *route* side merges routes that are not the same upstream -- ``a.com:8443``
    and ``a.com`` would collapse into one candidate list and registry order
    would decide between them again, which is the selection bug #1341 closed.
    So the port stays in the comparison: a request carrying ``:8443`` matches
    only a route registered with that port, and a request with no port matches
    only a route registered without one. The one fold left is the *scheme's*
    default port to absent -- ``a.com`` and ``https://a.com:443`` really are one
    destination, and a client that spells the default port explicitly must still
    reach it. ``scheme`` defaults to https for a caller that has no route in
    hand yet. IPv6 literals are out of scope, matching the port handling the
    request side already does.
    """
    name, sep, port = host.partition(":")
    default_port = _DEFAULT_PORTS.get(scheme, "443")
    return name.casefold(), None if not sep or port == default_port else port


def _normalize_request_host(host: str) -> tuple[str, str | None]:
    """:func:`_normalize_host` for a request, whose scheme the header cannot name.

    A ``Host`` header is a bare authority, so a request cannot say whether its
    explicit port is http's default or https's. Fold either, the way the route
    side folded its own, or a client writing ``http://a.com:80`` against a route
    registered as ``http://a.com`` is refused as unregistered for spelling a
    port the scheme does not consider one.
    """
    name, port = _normalize_host(host)
    return name, None if port in _ANY_DEFAULT_PORT else port


def _path_under_prefix(path: str, prefix: str) -> bool:
    """Whether ``path`` is within the route's ``prefix``.

    ``prefix`` is the only per-path scope a route has, so the boundary is a
    whole segment, not a string prefix: ``/v1/invoices`` admits
    ``/v1/invoices/49`` but not ``/v1/invoices-archived``, which is a
    different resource the claim says nothing about (issue #1051). A route
    registered without a prefix keeps the whole host, which is what the
    default ``"/"`` has always meant. Both sides are normalised here so a
    caller cannot hand in an uncollapsed path and slip past the boundary.
    Callers pass the raw request line, not an already-normalised path, since
    ``unquote`` is not idempotent: a second pass decodes a doubly-encoded
    separator into a real one and judges a path the upstream never serves.
    """
    normalized = _normalize_path(prefix)
    if normalized == "/":
        return True
    requested = _normalize_path(path)
    if requested == normalized:
        return True
    return requested.startswith(f"{normalized}/")


def match_route(
    routes: list[Route],
    *,
    host: str,
    method: str,
    path: str,
    body: dict[str, Any],
    actions_by_key: dict[str, Any],
    run_id: str,
    bound_tenant: str | None = None,
    storage: Any | None = None,
    consumed_authorities: Any | None = None,
) -> Decision:
    """The gateway's verdict for one request, mirroring gate's table."""
    from continuum.actions.idempotency import idempotency_key
    from continuum.gate import consumed_authority_reason, find_consumed_authority
    from continuum.models import ActionStatus

    # Authority resurrection check (issue #289b): refuse if body carries a
    # consumed authority, at any depth in the argument structure (issue #1074).
    if consumed_authorities:
        spent_id, ev = find_consumed_authority(body, consumed_authorities)
        if spent_id is not None:
            return Decision(
                False,
                consumed_authority_reason(spent_id, ev),
                route=None,
            )

    request_host = _normalize_request_host(host)
    candidates = [r for r in routes if _normalize_host(r.host, r.scheme) == request_host]
    if not candidates:
        return Decision(False, f"no upstream registered for host {host!r}")

    # The prefix is the only per-path scope a route has, so it narrows before
    # the key is even rendered: without it, one claim for /v1/invoices spends
    # itself on every path the host serves, and the recorded evidence says the
    # invoice was sent while the upstream saw something else (issue #1051).
    # The raw request line goes in, not a pre-normalised one: ``unquote`` is
    # not idempotent, so normalising here and again inside
    # ``_path_under_prefix`` would decode a doubly-encoded separator twice and
    # see a path the upstream, which decodes once, never serves. The claim
    # would then be spent on a request the prefix never really admitted.
    requested = _normalize_path(path)
    scoped = [r for r in candidates if _path_under_prefix(path, r.prefix)]
    if not scoped:
        return Decision(
            False,
            f"host {host!r} is registered but {requested!r} is not under any of "
            f"its prefixes {sorted(r.prefix for r in candidates)}",
        )

    # Most specific prefix wins, regardless of the order the registry lists
    # routes in (issue #1341). A broad route must not shadow a narrower one
    # that also admits the path: without this sort, two configs identical but
    # for the order of their ``upstreams`` array render different keys for the
    # same request and so consult, or spend, a different claim. The empty-prefix
    # whole-host default normalises to ``/`` and sorts last, which is what it
    # means.
    scoped.sort(key=lambda r: len(_normalize_path(r.prefix)), reverse=True)

    # Method selection stays inside that most specific prefix. Once a route has
    # won the prefix race it governs the path, so falling through to a broader
    # route when the winner does not admit the method would authorise the
    # request under a claim less specific than the scope that now owns the
    # path -- the same wrong-claim class #1341 is about, one axis over. Routes
    # sharing the winning prefix (one route per method, the natural way to
    # express a resource family) are one scope and are all eligible here.
    best_prefix = len(_normalize_path(scoped[0].prefix))
    eligible = [r for r in scoped if len(_normalize_path(r.prefix)) == best_prefix]
    route = next((r for r in eligible if method.upper() in r.methods), None)
    if route is None:
        allowed = sorted({m.lower() for r in eligible for m in r.methods})
        return Decision(
            False,
            f"host {host!r} is registered but {method} is not among its allowed methods {allowed}",
        )

    # A memory key's segments are colon-delimited, so a colon in a placeholder
    # value before the terminal segment shifts them and defeats the positional
    # tenant check below (#1415). Deny as malformed here, before rendering, so
    # ``render_key``'s boundary check is never the seam that answers a proxy
    # request: the hook raises ``GateConfigError`` and the proxy answers
    # "malformed memory key", and each test targets its own seam. Only the
    # fields before the terminal one are checked -- a colon in the last segment
    # cannot move the tenant position and is re-parsed downstream as
    # ``":".join(parts[3:])``, so a record key such as ``doc:section:1`` stays
    # a valid write (#1149 review). The two seams must agree on which colons
    # are allowed, or a call denied at the proxy would render and claim at the
    # hook.
    if is_memory_template(route.key_template):
        import string as _fields_string

        _fields = [f for _, f, _, _ in _fields_string.Formatter().parse(route.key_template) if f]
        for _field in _fields[:-1]:
            _value = body.get(_field, "")
            if ":" in str(normalize_key_value(_value)):
                return Decision(
                    False,
                    f"malformed memory key: template {route.key_template!r} field {_field!r} "
                    f"must not contain ':' (it would shift the key's colon-delimited "
                    f"segments), got {_value!r}",
                    route=route,
                )

    try:
        rendered = render_key(route.key_template, body)
    except GatewayConfigError as exc:
        return Decision(False, f"gateway configuration error: {exc}")

    # Tenant boundary enforcement (issue #566, issue #1415): memory keys carry
    # tenant in the rendered identity. When a bound tenant is configured or
    # present in the run context, a claim whose tenant namespace does not match
    # is denied at the gate rather than surfacing later as a breach.
    if bound_tenant is None and storage is not None and hasattr(storage, "get_run"):
        try:
            run_obj = storage.get_run(run_id)
            if run_obj and getattr(run_obj, "metadata", None):
                meta_tenant = run_obj.metadata.get("tenant_id") or run_obj.metadata.get("tenant")
                if meta_tenant and str(meta_tenant).strip():
                    bound_tenant = str(meta_tenant).strip()
        except Exception:
            pass

    if is_memory_key(rendered) and bound_tenant is not None:
        import string as _string

        fields = [f for _, f, _, _ in _string.Formatter().parse(route.key_template) if f]
        tenant_field = "tenant_id" if "tenant_id" in fields else "tenant"
        tenant_in_key = str(normalize_key_value(body.get(tenant_field, "")))
        if tenant_in_key != bound_tenant:
            return Decision(
                False,
                f"tenant mismatch: bound {bound_tenant!r} but key {rendered!r} carries tenant {tenant_in_key!r}",
                route=route,
            )

    # Memory keys are global to the store, not the run, so use scope=None
    # to let the action_index catch cross-run double-writes.
    if is_memory_key(rendered):
        key = str(idempotency_key(route.action_type, None, scope=None, key=rendered))
    else:
        key = str(idempotency_key(route.action_type, None, scope=run_id, key=rendered))
    # For memory keys, also check foreign index when local misses
    foreign_action = None
    action = actions_by_key.get(key)
    if action is None and is_memory_key(rendered) and storage is not None:
        try:
            if getattr(storage, "supports_action_index", False):
                foreign_action = storage.foreign_action(key, exclude_run=run_id)
                if foreign_action is not None:
                    # Mirror gate.decide's status table for a foreign record
                    # (the docstring promises this) instead of one blanket
                    # "reconcile it first": reconcile only fits UNKNOWN, and
                    # a terminal foreign record (FAILED/COMPENSATED) left no
                    # live effect, so the way forward is a fresh claim, not a
                    # reconcile that has nothing to settle (#765e4bc). A
                    # foreign STARTED is still denied here: a live claim in
                    # another run must not authorise a parallel write to the
                    # same global key.
                    fstatus = foreign_action.status
                    if fstatus is ActionStatus.COMPLETED:
                        return Decision(
                            False,
                            f"side effect {route.action_type!r} key {rendered!r} "
                            f"was already completed in another run"
                            + (
                                f" (external id {foreign_action.external_id!r})"
                                if foreign_action.external_id
                                else ""
                            )
                            + "; do not repeat it",
                            route=route,
                        )
                    if fstatus is ActionStatus.UNKNOWN:
                        return Decision(
                            False,
                            f"side effect {route.action_type!r} key {rendered!r} has an "
                            f"unknown outcome in another run; reconcile it first "
                            f"(continuum_reconcile_action)",
                            route=route,
                        )
                    if fstatus is ActionStatus.STARTED:
                        return Decision(
                            False,
                            f"side effect {route.action_type!r} key {rendered!r} is "
                            f"claimed live in another run; it must settle before this "
                            f"run can claim it",
                            route=route,
                        )
                    return Decision(
                        False,
                        f"the previous attempt of {route.action_type!r} with key "
                        f"{rendered!r} in another run is closed (status "
                        f"{fstatus.value}); claim it again through "
                        f"continuum_intercept_action before retrying",
                        route=route,
                    )
        except Exception:
            action = None
    if action is None or getattr(action, "action_type", None) != route.action_type:
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
        bound_tenant: str | None = None,
    ) -> None:
        """Bind the proxy and build the handler that enforces ``routes``.

        ``port=0`` takes an ephemeral port, readable afterwards as
        :attr:`port`. ``run_id`` of ``None`` resolves the active run per
        request, so the proxy can start before the run does.
        """
        self._storage_factory = storage_factory
        self._run_id = run_id
        self._routes = routes
        self._bound_tenant = bound_tenant
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence test noise
                """Drop the stdlib access log: evidence belongs in the event log."""
                pass

            def _body(self, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
                """Read the request body as a mapping, or answer and raise.

                Returns an empty mapping for a body that is absent, and for one
                that parses to valid JSON of some other shape: neither can bind
                a key template field, and the missing-field refusal downstream
                names that correctly.

                A body that cannot be read at all does not come back. This
                writes the refusal itself and raises, 413 with
                :class:`_BodyTooLarge` when it is longer than ``max_bytes``, 400
                with :class:`_MalformedBody` when it is not JSON this proxy can
                decode (issue #323). Callers catch both and return, since the
                response is already on the wire.
                """
                transfer_encodings = self.headers.get_all("Transfer-Encoding", [])
                if transfer_encodings:
                    self.close_connection = True
                    self._respond(400, {"error": "transfer encoding is not supported"})
                    raise _MalformedBody

                content_lengths = self.headers.get_all("Content-Length", [])
                if len(content_lengths) > 1:
                    self.close_connection = True
                    self._respond(
                        400, {"error": "multiple Content-Length headers are not supported"}
                    )
                    raise _MalformedBody

                cl_header = content_lengths[0] if content_lengths else None
                if cl_header is not None:
                    try:
                        length = int(cl_header)
                        if length < 0:
                            raise ValueError("Content-Length must be non-negative")
                    except ValueError as exc:
                        self.close_connection = True
                        self._respond(400, {"error": f"malformed Content-Length header: {exc}"})
                        raise _MalformedBody from exc
                else:
                    length = 0

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
                if not raw:
                    # A genuinely empty body stays an empty mapping: a route
                    # whose template needs no fields is legitimately callable
                    # with no body at all.
                    return {}
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    # Answering with an empty mapping instead would send the
                    # request on to be refused for a missing template field,
                    # naming the wrong problem: the body was never read as the
                    # caller wrote it. Say so, in the status code too (#323).
                    #
                    # UnicodeDecodeError is the other half of "cannot be read":
                    # json.loads decodes bytes before parsing them, so a body
                    # that is not valid UTF-8 raises from the decode rather than
                    # the parse. Left uncaught it escapes the handler entirely
                    # and the connection closes with no response at all, which
                    # is the same misreport as the missing field, only quieter.
                    self._respond(400, {"error": f"invalid JSON in request body: {exc}"})
                    raise _MalformedBody from exc
                return parsed if isinstance(parsed, dict) else {}

            def _respond(self, code: int, payload: dict[str, Any]) -> None:
                """Answer with one JSON body, keeping the framing self-consistent.

                ``Connection: close`` is sent only when the handler has already
                decided to close, so a refusal that left the request body unread
                does not advertise keep-alive it cannot honour.
                """
                body = json.dumps(payload).encode()
                self.send_response(code)
                if getattr(self, "close_connection", False):
                    self.send_header("Connection", "close")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                """Route one request through the gate and answer it.

                The bare ``return`` on a body failure is not a swallowed error:
                :meth:`_body` has already written the 413 or the 400, and going
                on would put a second response on the same connection.
                """
                host = self.headers.get("Host", "")
                try:
                    body = self._body()
                except (_BodyTooLarge, _MalformedBody):
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
                    bound_tenant = getattr(server, "_bound_tenant", None)
                    header_tenant = self.headers.get("X-Continuum-Tenant")
                    if header_tenant:
                        header_tenant = str(header_tenant).strip()

                    try:
                        run_obj = storage.get_run(run_id) if hasattr(storage, "get_run") else None
                    except Exception:
                        run_obj = None
                    run_metadata = getattr(run_obj, "metadata", {}) or {}
                    run_tenant = run_metadata.get("tenant_id") or run_metadata.get("tenant")
                    if run_tenant:
                        run_tenant = str(run_tenant).strip()

                    if bound_tenant and header_tenant and bound_tenant != header_tenant:
                        self._respond(
                            403,
                            {
                                "error": "denied by CONTINUUM gateway",
                                "reason": (
                                    f"tenant mismatch: header {header_tenant!r} "
                                    f"does not match bound tenant {bound_tenant!r}"
                                ),
                            },
                        )
                        return

                    if run_tenant and header_tenant and run_tenant != header_tenant:
                        self._respond(
                            403,
                            {
                                "error": "denied by CONTINUUM gateway",
                                "reason": (
                                    f"tenant mismatch: header {header_tenant!r} "
                                    f"does not match run tenant {run_tenant!r}"
                                ),
                            },
                        )
                        return

                    effective_tenant = bound_tenant or header_tenant or run_tenant

                    from continuum.actions.ledger import fold_action_events

                    history = storage.read_all_events(run_id)
                    actions = fold_action_events(history)
                    from continuum.gate import collect_consumed_authorities

                    consumed = collect_consumed_authorities(history)

                    decision = match_route(
                        server._routes,
                        # The raw header, port included: the port is part of the
                        # destination and ``match_route`` needs it to tell
                        # ``a.com:8443`` from ``a.com`` apart (issue #1342). The
                        # route's own host, not this header, is what the upstream
                        # connection is opened to.
                        host=host,
                        method=method,
                        path=self.path,
                        body=body,
                        actions_by_key=actions,
                        run_id=run_id,
                        bound_tenant=effective_tenant,
                        storage=storage,
                        consumed_authorities=consumed,
                    )
                    if not decision.allow or decision.route is None or decision.key is None:
                        self._respond(
                            403, {"error": "denied by CONTINUUM gateway", "reason": decision.reason}
                        )
                        return

                    from continuum.actions.ledger import ActionLedger

                    # The route's scheme picks the transport: an http upstream
                    # gets a plain connection, an https one the TLS one. Hardcoded
                    # https, a plain-HTTP upstream -- a local service, an internal
                    # address behind a TLS terminator -- was unreachable at all:
                    # the handshake to its cleartext port failed as
                    # WRONG_VERSION_NUMBER before any byte was forwarded, the
                    # claim settled UNKNOWN, and nothing was recorded.
                    scheme = decision.route.scheme
                    parts = urlsplit(f"{scheme}://{decision.route.host}{self.path}")
                    import http.client as http_client

                    conn: Any = (
                        http_client.HTTPConnection(parts.netloc, timeout=30)
                        if scheme == "http"
                        else http_client.HTTPSConnection(parts.netloc, timeout=30)
                    )
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
                        resp_body = _read_bounded_response(resp, MAX_RESPONSE_BYTES)
                        if resp_body is None:
                            # An upstream that cannot answer inside the cap is
                            # the same class of failure as one that cannot
                            # answer at all: the side effect's state is
                            # unknown, not completed.
                            ActionLedger(storage, run_id).fail(
                                decision.key,
                                f"upstream response exceeds {MAX_RESPONSE_BYTES} bytes",
                                certain=False,
                            )
                            self._respond(502, {"error": "upstream response too large"})
                            return
                        status = resp.status
                    except (OSError, http_client.HTTPException) as exc:
                        # A dropped connection (OSError) or a malformed/truncated
                        # upstream response (http.client.HTTPException: IncompleteRead,
                        # BadStatusLine, ...) is an *uncertain* outcome: the request may
                        # already have reached the upstream and fired the effect. Settle
                        # the claim UNKNOWN so recovery forces reconciliation. Catching
                        # only OSError let HTTPException escape with the claim still
                        # STARTED, and a retry then re-fired the effect the gateway
                        # exists to make exactly-once.
                        ledger = ActionLedger(storage, run_id)
                        ledger.fail(decision.key, f"upstream I/O error: {exc}", certain=False)
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

            def do_POST(self) -> None:
                """Route a POST through the claim check."""
                self._handle("POST")

            def do_PUT(self) -> None:
                """Route a PUT through the claim check."""
                self._handle("PUT")

            def do_PATCH(self) -> None:
                """Route a PATCH through the claim check."""
                self._handle("PATCH")

            def do_DELETE(self) -> None:
                """Route a DELETE through the claim check."""
                self._handle("DELETE")

            def do_GET(self) -> None:
                """Route a GET through the claim check, if a route registers it."""
                self._handle("GET")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = int(self.httpd.server_address[1])

    def serve_forever(self) -> None:
        """Serve until :meth:`shutdown`, blocking the calling thread."""
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop serving and release the socket.

        The stop runs on its own thread because ``shutdown`` cannot be called
        from the thread currently inside ``serve_forever``.
        """
        threading.Thread(target=self.httpd.shutdown).start()
        self.httpd.server_close()
