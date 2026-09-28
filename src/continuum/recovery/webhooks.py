"""Registry-driven notifications when a run blocks (issue #305).

:mod:`continuum.recovery.notify` is the delivery primitive: sign and POST one
payload, fail open. This module is everything around it for ``request_human``:

* the ``.continuum/webhooks.json`` registry (endpoints, event filter,
  re-notify interval, retry policy, dashboard base URL), following the same
  data-beside-code convention as ``gateway.json``
* the dedup that keeps a polling cron from spamming: one notification per
  *transition* into a blocked state, not per re-assessment, keyed on
  ``(run_id, mode, contract hash)`` with a re-notify interval. The dedup
  state is durable because it lives in the event log itself
  (``NOTIFICATION_SENT`` / ``NOTIFICATION_FAILED`` rows), so a restart does
  not re-ring the bell
* the dead-letter record: when delivery finally fails, a
  ``NOTIFICATION_FAILED`` event names the endpoint and the verdict that went
  undelivered

Notification failure never flips a recovery verdict: the safety decision and
the bell stay decoupled, so every failure mode here returns a record instead
of raising.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from continuum.events import Event, EventType
from continuum.models import RecoveryContract, utcnow
from continuum.recovery.notify import post_webhook
from continuum.security.hashing import stable_hash
from continuum.storage.base import Storage

__all__ = [
    "DEFAULT_WEBHOOKS_PATH",
    "EVENT_REQUEST_HUMAN",
    "EVENT_REQUIRES_REVIEW",
    "NOTIFY_TEST_EVENT",
    "WebhookConfigError",
    "WebhookEndpoint",
    "WebhookRegistry",
    "load_webhook_registry",
    "verdict_key",
    "notify_blocked",
]

DEFAULT_WEBHOOKS_PATH = ".continuum/webhooks.json"

#: The blocked-state events an endpoint can subscribe to. ``request_human``
#: is the whole point of #305; ``requires_review`` is opt-in for operators
#: who also want to hear about self-certified runs awaiting confirmation.
EVENT_REQUEST_HUMAN = "request_human"
EVENT_REQUIRES_REVIEW = "requires_review"

#: The event name ``continuum notify-test`` sends: a wiring probe that must
#: bypass dedup by design, because it is not a state transition.
NOTIFY_TEST_EVENT = "notify-test"

_KNOWN_EVENTS = frozenset({EVENT_REQUEST_HUMAN, EVENT_REQUIRES_REVIEW})


class WebhookConfigError(ValueError):
    """The webhook registry exists but cannot be honoured."""


@dataclass(frozen=True)
class WebhookEndpoint:
    """One operator-declared receiver of blocked-run notifications."""

    url: str
    secret: str | None = None
    #: Which blocked-state events this endpoint hears about.
    events: frozenset[str] = frozenset({EVENT_REQUEST_HUMAN})
    #: Dedup window: a repeat of the same verdict within this many seconds
    #: is assumed to be the same blockage still standing, not a new one.
    re_notify_seconds: int = 3600
    #: Extra attempts after the first, without backoff: notification runs on
    #: the CLI's critical path, so waiting out a receiver's brownout is not
    #: an option. The dead-letter event records what did not get through.
    retries: int = 2
    timeout: float = 5.0


@dataclass(frozen=True)
class WebhookRegistry:
    """The parsed ``webhooks.json``: endpoints plus the deep-link base."""

    endpoints: tuple[WebhookEndpoint, ...] = ()
    dashboard_base_url: str | None = None

    def for_event(self, event: str) -> list[WebhookEndpoint]:
        """Endpoints whose event filter subscribes to ``event``."""
        return [e for e in self.endpoints if event in e.events]


def load_webhook_registry(path: Path) -> WebhookRegistry:
    """Read the notification registry. Empty when absent; raise when malformed.

    A missing file means "no webhooks configured", which is the default and
    must stay silent. A file that exists but cannot be honoured raises
    :class:`WebhookConfigError` naming the resolved path, so the operator
    debugging a silent bell finds the file to open (same discipline as the
    gate and gateway registries, #333).
    """
    if not path.exists():
        return WebhookRegistry()
    location = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WebhookConfigError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise WebhookConfigError(f"{location}: expected a JSON object")

    base = raw.get("dashboard_base_url")
    if base is not None and (
        not isinstance(base, str) or not base.startswith(("http://", "https://"))
    ):
        raise WebhookConfigError(f"{location}: dashboard_base_url must be an http(s) URL")

    entries = raw.get("endpoints", [])
    if not isinstance(entries, list):
        raise WebhookConfigError(f"{location}: expected {{'endpoints': [...]}}")
    endpoints: list[WebhookEndpoint] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise WebhookConfigError(f"{location}: each endpoint must be an object")
        url = entry.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise WebhookConfigError(f"{location}: endpoint needs an http(s) 'url'")
        events = entry.get("events", [EVENT_REQUEST_HUMAN])
        if not isinstance(events, list) or not events:
            raise WebhookConfigError(f"{location}: 'events' must be a non-empty list")
        unknown = [e for e in events if e not in _KNOWN_EVENTS]
        if unknown:
            # A typo'd event name would silently never fire, which is the
            # quietest possible failure: refuse it up front.
            raise WebhookConfigError(
                f"{location}: unknown event(s) {unknown}; known: {sorted(_KNOWN_EVENTS)}"
            )
        secret = entry.get("secret")
        if secret is not None and not isinstance(secret, str):
            raise WebhookConfigError(f"{location}: 'secret' must be a string")
        re_notify = entry.get("re_notify_seconds", 3600)
        if not isinstance(re_notify, int) or isinstance(re_notify, bool) or re_notify < 0:
            raise WebhookConfigError(f"{location}: 're_notify_seconds' must be a non-negative int")
        retries = entry.get("retries", 2)
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise WebhookConfigError(f"{location}: 'retries' must be a non-negative int")
        timeout = entry.get("timeout", 5.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise WebhookConfigError(f"{location}: 'timeout' must be a positive number")
        endpoints.append(
            WebhookEndpoint(
                url=url,
                secret=secret,
                events=frozenset(events),
                re_notify_seconds=re_notify,
                retries=retries,
                timeout=float(timeout),
            )
        )
    return WebhookRegistry(endpoints=tuple(endpoints), dashboard_base_url=base)


#: Contract fields that describe *when* a verdict was formed or what was
#: observed around it, not what it decides. ``created_at`` is fresh on every
#: assessment; ``liveness`` carries a last-append age that changes on every
#: append; ``post_checkpoint_observations`` and ``integrity_hash`` are
#: informational bookkeeping the model documents as never affecting the
#: recovery decision. Hashing any of them would give a standing blockage a
#: new identity on every re-assessment, which is the spam the dedup exists
#: to prevent.
_VOLATILE_CONTRACT_FIELDS = frozenset(
    {
        "created_at",
        "integrity_hash",
        "liveness",
        "post_checkpoint_observations",
    }
)


def verdict_key(mode: str, contract: RecoveryContract) -> str:
    """Stable identity of one blocked verdict: mode plus the contract's content.

    Two assessments of the same unchanged blockage produce the same key, so
    dedup holds across them; a settled-then-reblocked run produces a
    different contract and therefore a different key, so the bell rings
    again. That is the ``per transition, not per re-assessment`` rule of
    issue #305. Only the decision content is hashed: the fields in
    ``_VOLATILE_CONTRACT_FIELDS`` change between assessments of the same
    blockage without changing what was decided.
    """
    content = {
        field: value
        for field, value in contract.model_dump(mode="json").items()
        if field not in _VOLATILE_CONTRACT_FIELDS
    }
    return stable_hash({"mode": mode, "contract": content})


@dataclass(frozen=True)
class DeliveryRecord:
    """What happened for one endpoint during a notification round."""

    url: str
    event: str
    #: ``sent`` (receiver answered 2xx), ``skipped`` (dedup window held) or
    #: ``failed`` (every attempt refused or timed out; dead-letter recorded).
    status: str
    detail: str = ""


def _within_dedup_window(
    history: Sequence[Event],
    endpoint: WebhookEndpoint,
    key: str,
    now: datetime,
) -> bool:
    """Whether this exact verdict was already delivered to ``endpoint`` recently.

    Takes the already-merged full history, not the storage: ``notify_blocked``
    serves every subscribed endpoint out of one read, and re-reading the
    archive per endpoint re-deserializes a long compacted run N times on the
    CLI critical path.

    Both SENT and FAILED rows count: a notification that did not get through
    still consumed the attempt, and re-firing it every minute against a dead
    receiver is the spam the dedup exists to prevent. The operator discovers
    the dead letter by polling the log, the same way they discovered the
    blockage before #305.

    The history must include the archived prefix: the NOTIFICATION_SENT row
    can predate a compaction (issue #1186). Compacting a long blocked run is
    exactly what the docs prescribe for it, and archived rows keep their
    original timestamps, so the window itself is unaffected by the archived
    prefix, only the scan has to look there.
    """
    for event in history:
        if event.type not in (EventType.NOTIFICATION_SENT, EventType.NOTIFICATION_FAILED):
            continue
        payload = event.payload
        if payload.get("verdict") != key or payload.get("url") != endpoint.url:
            continue
        if (now - event.timestamp).total_seconds() < endpoint.re_notify_seconds:
            return True
    return False


def notify_blocked(
    storage: Storage,
    run_id: str,
    *,
    mode: str,
    payload: dict[str, Any],
    contract: RecoveryContract,
    registry: WebhookRegistry,
    now: datetime | None = None,
) -> list[DeliveryRecord]:
    """Deliver one notification round for a blocked run. Never raises.

    For every endpoint subscribed to ``mode``: skip when the same verdict
    (``verdict_key``) was already delivered within the endpoint's
    re-notify window; otherwise POST ``payload`` (plus the event name and a
    dashboard deep link when the registry has a base URL) up to
    ``retries + 1`` times. Each terminal outcome is appended to the run's
    event log - ``NOTIFICATION_SENT`` or ``NOTIFICATION_FAILED`` - so dedup
    survives restarts and failures are auditable.

    Returns one :class:`DeliveryRecord` per endpoint. The caller decides how
    loudly to surface them; the recovery verdict is never this function's
    to change.
    """
    endpoints = registry.for_event(mode)
    if not endpoints:
        return []
    now = now or utcnow()
    key = verdict_key(mode, contract)
    # One archive-aware read serves every endpoint; re-reading per endpoint
    # would re-deserialise a long compacted run once per subscription.
    history = storage.read_all_events(run_id)
    records: list[DeliveryRecord] = []
    for endpoint in endpoints:
        if _within_dedup_window(history, endpoint, key, now):
            records.append(
                DeliveryRecord(
                    url=endpoint.url,
                    event=mode,
                    status="skipped",
                    detail="same verdict already notified within the re-notify window",
                )
            )
            continue
        body = dict(payload)
        body["event"] = mode
        if registry.dashboard_base_url:
            body["dashboard_url"] = f"{registry.dashboard_base_url}/runs/{run_id}"
        delivered = False
        attempts = endpoint.retries + 1
        for _ in range(attempts):
            if post_webhook(endpoint.url, body, secret=endpoint.secret, timeout=endpoint.timeout):
                delivered = True
                break
        if delivered:
            storage.append_event(
                run_id,
                EventType.NOTIFICATION_SENT,
                {"url": endpoint.url, "event": mode, "verdict": key},
            )
            records.append(DeliveryRecord(url=endpoint.url, event=mode, status="sent"))
        else:
            storage.append_event(
                run_id,
                EventType.NOTIFICATION_FAILED,
                {"url": endpoint.url, "event": mode, "verdict": key},
            )
            records.append(
                DeliveryRecord(
                    url=endpoint.url,
                    event=mode,
                    status="failed",
                    detail=f"delivery failed after {attempts} attempt(s); dead-letter recorded",
                )
            )
    return records
