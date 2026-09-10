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

import hashlib
import hmac
import http.client
import json
import os
from typing import Any

SECRET_ENV_VAR = "CONTINUUM_WEBHOOK_SECRET"
SIGNATURE_HEADER = "X-Continuum-Signature"
_SIGNATURE_PREFIX = "sha256="


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
