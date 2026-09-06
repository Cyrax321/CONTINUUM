"""Resolving actions whose outcome is unknown.

When a crash lands between an effect and its record, the ledger cannot tell
what happened. Only the external system knows. A reconciler asks it.

Strategies, and when each is defensible
---------------------------------------

``ProbeReconciler``
    Ask the external system directly, search for the issue, look up the charge.
    The only strategy that produces evidence. Prefer it wherever a read exists.

``AssumeNotOccurredReconciler``
    Retry. Correct **only** for naturally idempotent operations, where a second
    attempt is indistinguishable from the first. Requires an explicit assertion
    that the operation is idempotent, so nobody reaches for it by reflex.

``ManualReconciler``
    Escalate to a human. The right answer when no cheap read exists and the
    operation is not idempotent. Slow, and honest about it.

There is deliberately no ``AssumeOccurred`` strategy. Assuming success without
evidence silently drops work, and a dropped side effect is invisible, nothing
in the system will ever contradict it. Optimism is the one default that cannot
be audited after the fact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from continuum.actions.ledger import ActionLedger
from continuum.models import Action, ActionStatus

__all__ = [
    "Reconciler",
    "Resolution",
    "ProbeReconciler",
    "AssumeNotOccurredReconciler",
    "ManualReconciler",
    "reconcile_pending",
    "ReconciliationReport",
]


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a probe concluded about an uncertain effect."""

    occurred: bool
    external_id: str | None = None
    result: Mapping[str, Any] | None = None
    note: str = ""


class Reconciler(ABC):
    """Determines whether an uncertain side effect actually happened."""

    name: str = "reconciler"

    @abstractmethod
    def resolve(self, action: Action) -> Resolution | None:
        """Return a resolution, or ``None`` if this reconciler cannot decide.

        Returning ``None`` is a legitimate answer and leaves the action
        uncertain, better than a confident wrong one.
        """


class ProbeReconciler(Reconciler):
    """Asks the external system whether the effect exists.

    The probe receives the recorded action and returns a ``Resolution``, or
    ``None`` if it could not find out. A probe that raises is treated as "could
    not find out" rather than as evidence of absence, an unreachable API tells
    you nothing about whether your earlier request landed.
    """

    name = "probe"

    def __init__(self, probe: Callable[[Action], Resolution | None]) -> None:
        self._probe = probe
        self.last_error: Exception | None = None

    def resolve(self, action: Action) -> Resolution | None:
        try:
            return self._probe(action)
        except Exception as exc:  # noqa: BLE001 - an unreachable probe is not evidence
            self.last_error = exc
            return None


class AssumeNotOccurredReconciler(Reconciler):
    """Assumes the effect did not happen, permitting a retry.

    Only valid for genuinely idempotent operations. ``idempotent=True`` must be
    passed explicitly: the assertion belongs to the caller who knows the
    operation, not to a default.
    """

    name = "assume_not_occurred"

    def __init__(self, *, idempotent: bool) -> None:
        if not idempotent:
            raise ValueError(
                "AssumeNotOccurredReconciler is only safe for idempotent operations; "
                "pass idempotent=True to assert that, or use ProbeReconciler / ManualReconciler"
            )

    def resolve(self, action: Action) -> Resolution:
        return Resolution(
            occurred=False,
            note="assumed not to have occurred (operation declared idempotent)",
        )


class ManualReconciler(Reconciler):
    """Defers to a human. Never resolves on its own."""

    name = "manual"

    def __init__(self, reason: str = "requires human confirmation") -> None:
        self.reason = reason

    def resolve(self, action: Action) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Outcome of reconciling a run's uncertain actions."""

    resolved_completed: tuple[str, ...] = ()
    resolved_failed: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True when nothing is left in doubt."""
        return not self.unresolved

    def render(self) -> str:
        lines = []
        if self.resolved_completed:
            lines.append(f"confirmed as performed: {', '.join(self.resolved_completed)}")
        if self.resolved_failed:
            lines.append(f"confirmed as not performed: {', '.join(self.resolved_failed)}")
        if self.unresolved:
            lines.append(f"STILL UNKNOWN: needs human review: {', '.join(self.unresolved)}")
        return "\n".join(lines) or "nothing to reconcile"


def reconcile_pending(
    ledger: ActionLedger,
    reconcilers: Mapping[str, Reconciler] | Reconciler | None = None,
    *,
    default: Reconciler | None = None,
) -> ReconciliationReport:
    """Resolve every uncertain action in a run.

    ``reconcilers`` may map action types to strategies, so a GitHub issue and a
    payment can be reconciled differently. Anything unresolved is flagged for
    review and reported, never quietly retried.
    """
    if isinstance(reconcilers, Reconciler):
        default = default or reconcilers
        reconcilers = {}
    by_type: Mapping[str, Reconciler] = reconcilers or {}

    completed: list[str] = []
    failed: list[str] = []
    unresolved: list[str] = []

    for action in ledger.pending():
        key = _key_for(ledger, action)
        if key is None:  # pragma: no cover - pending actions always have a key
            continue

        reconciler = by_type.get(action.action_type, default)
        resolution = reconciler.resolve(action) if reconciler is not None else None

        if resolution is None:
            ledger.flag_for_review(
                key,
                f"outcome unknown and {reconciler.name if reconciler else 'no'} "
                f"reconciler could not determine it",
            )
            unresolved.append(action.action_type)
            continue

        ledger.reconcile(
            key,
            occurred=resolution.occurred,
            external_id=resolution.external_id,
            result=resolution.result,
            note=resolution.note,
        )
        (completed if resolution.occurred else failed).append(action.action_type)

    return ReconciliationReport(
        resolved_completed=tuple(completed),
        resolved_failed=tuple(failed),
        unresolved=tuple(unresolved),
    )


def _key_for(ledger: ActionLedger, action: Action) -> str | None:
    for key, candidate in ledger._replay().items():
        if candidate.action_id == action.action_id:
            return key
    return None  # pragma: no cover - pending() only yields actions read from the ledger


def unresolved_actions(ledger: ActionLedger) -> tuple[Action, ...]:
    """Actions a human must judge before the run can safely continue."""
    return tuple(
        a for a in ledger.all() if a.status in (ActionStatus.UNKNOWN, ActionStatus.REQUIRES_REVIEW)
    )
