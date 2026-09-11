"""Signed outbound webhooks for human notification (issue #305).

A blocked run is only useful if a human learns about it. This module is the
shared delivery primitive behind notification paths (liveness watch today,
request_human tomorrow): JSON POST, fail-open, with an HMAC-SHA256 signature
when the operator configures ``CONTINUUM_WEBHOOK_SECRET``. The signature lets
the receiver trust the notification came from CONTINUUM and not from the very
agent being reported on, the same discipline as ``CONTINUUM_MCP_CONFIRM_TOKEN``.

Without a secret the wire format is unchanged (plain JSON POST), so existing
receivers keep working. Delivery failure never raises: it returns False and
the caller decides how loudly to note it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
from typing import Any

SECRET_ENV_VAR = "CONTINUUM_WEBHOOK_SECRET"
SIGNATURE_HEADER = "X-Continuum-Signature"
_SIGNATURE_PREFIX = "sha256="

DEFAULT_WEBHOOKS_PATH: str = ".continuum/webhooks.json"

#: Events a webhook endpoint may subscribe to. Unknown names are refused at
#: load time so a typo fails loudly instead of silently never firing.
WEBHOOK_EVENTS = frozenset({"request_human", "requires_review", "liveness_breach"})

_MAX_RETRIES = 5
_DEFAULT_TIMEOUT = 5.0


def signature(payload: bytes, secret: str) -> str:
    """HMAC-SHA256 hex signature for a payload, with the header prefix."""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_signature(payload: bytes, secret: str, header: str | None) -> bool:
    """True when ``header`` is the valid signature for ``payload``."""
    if not header or not header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = signature(payload, secret)
    return hmac.compare_digest(expected, header)


def post_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """POST payload as JSON, signed when ``secret`` is set. Fail-open.

    Returns True when the receiver answered 2xx, False for any failure
    (unreachable, non-2xx, timeout). Never raises: notification failure
    must never flip a recovery verdict.
    """
    import urllib.error
    import urllib.request

    secret = secret if secret is not None else os.environ.get(SECRET_ENV_VAR)
    try:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if secret:
            headers[SIGNATURE_HEADER] = signature(data, secret)
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bool(200 <= response.status < 300)
    except (urllib.error.URLError, OSError, ValueError, TypeError, http.client.HTTPException):
        # TypeError covers non-serializable payloads: serialization is inside
        # the boundary so a bad payload fails open like a bad network.
        # HTTPException covers malformed status lines and truncated bodies,
        # which urlopen lets through unwrapped: all still fail open.
        return False


class WebhookConfigError(ValueError):
    """The webhook registry exists but cannot be honoured."""


@dataclasses.dataclass(frozen=True)
class WebhookEndpoint:
    """One outbound notification target (issue #305, unit 2)."""

    url: str
    secret: str | None = None
    events: tuple[str, ...] = ("request_human",)
    timeout: float = _DEFAULT_TIMEOUT
    max_retries: int = 0

    def wants(self, event: str) -> bool:
        """True when this endpoint subscribes to ``event``."""
        return event in self.events


def notify_endpoints(
    endpoints: list[WebhookEndpoint],
    event: str,
    payload: dict[str, Any],
) -> dict[str, bool]:
    """Deliver ``payload`` to every endpoint subscribed to ``event``.

    Returns ``{url: delivered}`` in registry order. Duplicate urls aggregate
    with AND: a url reports delivered only when every entry sharing it
    delivered, so one success can never mask another entry's failure.
    Each endpoint gets up to 1 + max_retries attempts with no delay between
    them; every failure mode fails open to False. Never raises: wrap
    defensively so a hostile registry object cannot crash the caller either.
    """
    results: dict[str, bool] = {}
    try:
        targets = [ep for ep in endpoints if ep.wants(event)]
    except Exception:
        return results
    for ep in targets:
        delivered = False
        try:
            for _ in range(1 + max(0, ep.max_retries)):
                if post_webhook(ep.url, payload, secret=ep.secret, timeout=ep.timeout):
                    delivered = True
                    break
        except Exception:
            delivered = False
        results[ep.url] = results.get(ep.url, True) and delivered
    return results


def load_webhooks(path: str | Path | None = None) -> list[WebhookEndpoint]:
    """Read the webhook registry. Empty list when absent; raise when malformed.

    File shape is ``{"webhooks": [{"url", "secret"?, "events"?, "timeout"?,
    "max_retries"?}]}``, mirroring the reconcilers.json registry convention.
    ``url`` must be http(s): anything else (file://, gopher://) is refused so
    a registry typo cannot turn the notifier into a local-file reader.
    ``events`` defaults to request_human only. ``timeout`` must be a positive
    number (bools refused, per the reconciler lesson in #322) and
    ``max_retries`` an int in 0..5, so a typo cannot retry forever.
    """
    target = Path(path) if path is not None else Path(DEFAULT_WEBHOOKS_PATH)
    if not target.exists():
        return []
    location = target.resolve()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WebhookConfigError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("webhooks", []), list):
        raise WebhookConfigError(f"{location}: expected {{'webhooks': [...]}}")
    endpoints: list[WebhookEndpoint] = []
    for i, spec in enumerate(raw.get("webhooks") or []):
        where = f"{location}: webhooks[{i}]"
        if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
            raise WebhookConfigError(f"{where} needs a string 'url'")
        url = spec["url"]
        if not url.lower().startswith(("http://", "https://")):
            raise WebhookConfigError(f"{where} url must be http(s), got {url!r}")
        secret = spec.get("secret")
        if secret is not None and not isinstance(secret, str):
            raise WebhookConfigError(f"{where} secret must be a string")
        events = spec.get("events", ["request_human"])
        if not isinstance(events, list) or not events:
            raise WebhookConfigError(f"{where} events must be a non-empty list")
        for name in events:
            if name not in WEBHOOK_EVENTS:
                raise WebhookConfigError(
                    f"{where} unknown event {name!r} (known: {sorted(WEBHOOK_EVENTS)})"
                )
        timeout = spec.get("timeout", _DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise WebhookConfigError(f"{where} timeout must be a positive number")
        retries = spec.get("max_retries", 0)
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise WebhookConfigError(f"{where} max_retries must be an int")
        if not 0 <= retries <= _MAX_RETRIES:
            raise WebhookConfigError(f"{where} max_retries must be 0..{_MAX_RETRIES}")
        endpoints.append(
            WebhookEndpoint(
                url=url,
                secret=secret,
                events=tuple(events),
                timeout=float(timeout),
                max_retries=retries,
            )
        )
    return endpoints


def record_delivery_failure(storage: Any, run_id: str, url: str, event: str, error: str) -> None:
    """Append a NOTIFY_FAILED dead letter for an undelivered notification.

    Audit only: the type is non-projecting, so the dead letter never changes
    state, verdicts, or resume behavior. Best effort itself: storage errors
    are swallowed because a failing audit write must not break the caller.
    """
    from contextlib import suppress

    from continuum.events import EventType

    with suppress(Exception):
        storage.append_event(
            run_id,
            EventType.NOTIFY_FAILED,
            {"url": url, "event": event, "error": str(error)[:512]},
        )
