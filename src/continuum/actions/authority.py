"""Authority consumption tracking (issue #289/#555).

Records that a one-time authority (credential, approval token, permission) was
consumed. Each call appends a distinct hash-chained AUTHORITY_CONSUMED event
with Origin.DETERMINISTIC, so replay never deduplicates and the audit trail is
append-only.

This module is deliberately small: it validates the bounded payload via
AuthorityConsumed and appends it. Enforcement (gate resurrection check) lives
in the next sub-issue (#289b), probe validation in #289c. Here we only
provide the durable fact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from continuum.events import Event, EventType
from continuum.models import AuthorityConsumed, Origin, utcnow

__all__ = ["record_authority_consumed"]


def record_authority_consumed(
    storage: Any,
    run_id: str,
    authority_id: str,
    *,
    consumer_run_id: str | None = None,
    consumed_at: datetime | None = None,
    via_action_id: str | None = None,
) -> Event:
    """Append an AUTHORITY_CONSUMED event and return it.

    The event is hash-chained, stamped Origin.DETERMINISTIC, and bounded by
    AuthorityConsumed validation (authority_id 1-128). Every call creates a
    distinct row, so duplicate consumptions of the same authority_id are not
    collapsed, which is intentional for audit.

    Parameters
    ----------
    storage:
        Engine exposing append_event(run_id, type, payload, source).
    run_id:
        Run that owns the event (the log partition).
    authority_id:
        Identifier of the consumed authority, 1-128 characters after strip.
    consumer_run_id:
        Run that performed the consumption. Defaults to run_id when omitted,
        which covers the common case where the event owner is the consumer.
    consumed_at:
        When the authority was consumed. Defaults to utcnow().
    via_action_id:
        Optional action that triggered the consumption, for linkage.

    Returns
    -------
    Event
        The sealed event as appended, with hash and provenance.
    """
    payload_model = AuthorityConsumed(
        authority_id=authority_id,
        consumer_run_id=consumer_run_id if consumer_run_id is not None else run_id,
        consumed_at=consumed_at if consumed_at is not None else utcnow(),
        via_action_id=via_action_id,
    )
    payload = payload_model.model_dump(mode="json")
    event = storage.append_event(
        run_id,
        EventType.AUTHORITY_CONSUMED,
        payload,
        source=Origin.DETERMINISTIC,
    )
    return event  # type: ignore[no-any-return]
