"""Human-in-the-loop actions over the dashboard (issue #242).

request_human walls are correct but their only door was a CLI. This module
adds three operator verbs served as authenticated POST endpoints and rendered
as buttons on the run page:

- confirm      : REVIEW_CONFIRMED (Origin.HUMAN), clearing self-certification gates
- reconcile    : ACTION_RECONCILED through ActionLedger.reconcile, settling an
                 uncertain side effect from real evidence
- complete     : RUN_COMPLETED plus a COMPLETED run row

Audit parity is the invariant: every button maps 1:1 onto the human CLI verb,
lands the same event types with the same provenance, and nothing here widens
what agents may certify themselves. Mutating endpoints are refused unless
``CONTINUUM_DASHBOARD_TOKEN`` is set - fail-closed by default, matching the
MCP authz posture.
"""

from __future__ import annotations

from typing import Any

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import ActionStatus, Origin, RunStatus
from continuum.storage.base import Storage

__all__ = [
    "DEFAULT_HITL_TOKEN_ENV",
    "pending_actions_with_keys",
    "confirm_run",
    "complete_run",
    "reconcile_action",
    "authorize_hitl",
]

DEFAULT_HITL_TOKEN_ENV = "CONTINUUM_DASHBOARD_TOKEN"


class HitlUnauthorized(Exception):
    """Mutating endpoint called without the operator token."""


def authorize_hitl(token_from_request: str | None) -> None:
    """Verify the request token matches ``CONTINUUM_DASHBOARD_TOKEN``.

    Fails closed: raises ``HitlUnauthorized`` if the environment variable is
    unset or empty, or if ``token_from_request`` is missing or does not match
    the expected token.
    """
    import os

    expected = os.environ.get(DEFAULT_HITL_TOKEN_ENV)
    if not expected:
        raise HitlUnauthorized(f"set {DEFAULT_HITL_TOKEN_ENV} to enable dashboard mutations")
    if not token_from_request or token_from_request != expected:
        raise HitlUnauthorized("invalid dashboard token")


def confirm_run(storage: Storage, run_id: str) -> None:
    """Record an operator review confirmation for a run.

    Appends a ``REVIEW_CONFIRMED`` event with ``Origin.HUMAN`` provenance
    covering goal and progress components, clearing self-certification gates
    with audit parity to the CLI. Raises ``RunNotFound`` if ``run_id`` is
    missing from storage.
    """
    storage.get_run(run_id)
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"], "via": "dashboard"},
        source=Origin.HUMAN,
    )


def complete_run(storage: Storage, run_id: str, summary: str = "") -> None:
    """Mark a run as completed by a human operator from the dashboard.

    Appends a ``RUN_COMPLETED`` event with ``Origin.HUMAN`` provenance,
    attaches an optional summary note, and transitions the run row to
    ``RunStatus.COMPLETED``. Raises ``RunNotFound`` if ``run_id`` is missing
    from storage.
    """
    run = storage.get_run(run_id)
    note = {"closed_by": "dashboard"}
    if summary:
        note["summary"] = summary
    storage.append_event(run_id, EventType.RUN_COMPLETED, note, source=Origin.HUMAN)
    storage.update_run(run.touch(status=RunStatus.COMPLETED))


def reconcile_action(
    storage: Storage,
    run_id: str,
    ledger_key: str,
    *,
    occurred: bool,
    external_id: str | None = None,
) -> None:
    """Settle an uncertain side effect in the action ledger from operator evidence.

    Dispatches through ``ActionLedger.reconcile``, updating the action to
    ``COMPLETED`` (if ``occurred=True``) or ``FAILED`` (if ``occurred=False``)
    and recording an ``ACTION_RECONCILED`` event with provenance. Raises
    ``KeyError`` if ``ledger_key`` is not found in the ledger.
    """
    ActionLedger(storage, run_id).reconcile(
        ledger_key,
        occurred=occurred,
        external_id=external_id,
        note="settled from the dashboard",
    )


def pending_actions_with_keys(storage: Storage, run_id: str) -> list[tuple[str, Any]]:
    """Uncertain actions paired with their full ledger key (for buttons)."""
    from continuum.actions.ledger import fold_action_events

    folded = fold_action_events(storage.read_all_events(run_id))
    out: list[tuple[str, Any]] = []
    for key, action in folded.items():
        if action.status in (ActionStatus.STARTED, ActionStatus.UNKNOWN):
            out.append((key, action))
    return out
