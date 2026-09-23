"""Dispatch registered ``ActionReconciler`` plugins over a run's uncertain actions (#765).

The ``ActionReconciler`` seam in ``continuum.plugins.seams`` existed since Phase 7
with no consumer, which is why ``docs/ARCHITECTURE_EVOLUTION.md`` lists it as
"declared, not load-bearing". This module is the consumer.

It is deliberately not a telemetry client. A reconciler here is any object that
takes an ``Action`` and reports what it found in the world it watches: an OpenTelemetry
span store, a payment provider's API, a filesystem outbox, a queue's dead-letter
table. The seam is what keeps that set open (issue #268 is one such source), and
this module defines the contract every source meets: how evidence is merged, how
disagreement is resolved, and how failure is reported.

The result contract
-------------------

Every action assessed here lands in exactly one of four categories:

``CONFIRMED_OCCURRED``
    Evidence the effect exists was found. The action is settled as completed and
    never repeated.

``CONFIRMED_NOT_OCCURRED``
    Evidence of absence was found. The action is settled as failed and may be retried.

``UNAVAILABLE``
    No evidence was obtainable (no reconciler applied, every reconciler that did
    declined, or any reconciler errored). ``UNAVAILABLE`` is *explicitly* not
    evidence of absence; the action is escalated for review rather than retried,
    because "could not check" and "nothing happened" are different facts. An
    errored reconciler lands here even when others confirm: a broken source
    cannot say whether it agrees, so its silence is not neutrality, and settling
    on the remainder would let one broken plugin quietly veto a real conflict.

``CONFLICTING``
    Reconcilers that did return evidence disagreed. Also escalated for review: a
    machine cannot arbitrate between two sources it has no way to rank, and
    picking either side would let a broken reconciler veto a correct one or vice
    versa.

Selection and merge order
-------------------------

Reconcilers are normalized to a deterministic order (their declared ``name``)
before they run, so registration order, dict iteration order, or the order a
caller happens to hold them in can never change a verdict. The evidence list in
every report reflects that same order, so the JSON and text diagnostics are
reproducible and diffable, and a reviewer can see exactly which reconciler
contributed what.

The trust boundary
------------------

A reconciler receives the ``Action`` record and nothing else: no storage, no
ledger, no run state. It cannot write, only advise. Every mutation flows through
``ActionLedger.reconcile`` / ``flag_for_review`` inside
:func:`settle_with_reconcilers`, the same settlement path the probe registry
(issue #218) uses, so plugin evidence can never bypass the ledger's settlement
rules or lower a more cautious verdict. A reconciler that raises, or returns
something malformed, is recorded as an error and contributes no evidence; it
cannot make an action *more* settlable by failing.

No auto-discovery
-----------------

Nothing here discovers plugins. A caller passes the collection explicitly, or a
``Registry`` to resolve it from. A resume or reconcile operation must not
implicitly execute untrusted code, so registration is always an explicit act by
the operator.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from continuum.models import Action
from continuum.plugins.registry import Registry
from continuum.plugins.seams import ActionReconciler, Reconciliation

if TYPE_CHECKING:
    from continuum.storage.base import Storage

__all__ = [
    "ReconciliationOutcome",
    "ReconcilerEvidence",
    "ReconciliationAssessment",
    "SettlementReport",
    "resolve_reconcilers",
    "select_reconcilers",
    "assess_action",
    "settle_with_reconcilers",
]


class ReconciliationOutcome(Enum):
    """The four categories every assessed action lands in."""

    CONFIRMED_OCCURRED = "confirmed_occurred"
    CONFIRMED_NOT_OCCURRED = "confirmed_not_occurred"
    UNAVAILABLE = "unavailable_evidence"
    CONFLICTING = "conflicting_evidence"


@dataclass
class ReconcilerEvidence:
    """What one reconciler concluded about one action, with provenance.

    Exactly one of ``error`` and a meaningful ``occurred`` is set. ``error`` is
    populated only when the reconciler raised or returned a malformed value, and
    in that case ``occurred`` is ``None`` and the evidence contributes nothing to
    the verdict: it is reported so an operator can see the plugin is broken.
    """

    name: str
    occurred: bool | None = None
    external_id: str | None = None
    note: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Report this evidence as plain data."""
        return {
            "reconciler": self.name,
            "occurred": self.occurred,
            "external_id": self.external_id,
            "note": self.note,
            "error": self.error,
        }


@dataclass
class ReconciliationAssessment:
    """The merged verdict for one uncertain action (#765)."""

    action_id: str
    action_type: str
    outcome: ReconciliationOutcome
    evidence: list[ReconcilerEvidence] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Report the assessment as plain data, evidence in dispatch order."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "evidence": [item.as_dict() for item in self.evidence],
        }

    def render(self) -> str:
        """Human-readable rendering with identifiers and provenance."""
        lines = [
            f"{_action_label(self)}: {self.outcome.value}",
            f"  reason: {self.reason}",
        ]
        for item in self.evidence:
            if item.error is not None:
                lines.append(f"  [{item.name}] ERROR: {item.error}")
            else:
                verdict = (
                    ("occurred" if item.occurred else "not occurred")
                    if item.occurred is not None
                    else "no evidence"
                )
                detail = f" ({item.external_id})" if item.external_id else ""
                lines.append(f"  [{item.name}] {verdict}{detail}")
                if item.note:
                    lines.append(f"      {item.note}")
        return "\n".join(lines)


@dataclass
class SettlementReport:
    """What plugin reconciliation did for one run."""

    settled_true: list[str] = field(default_factory=list)
    settled_false: list[str] = field(default_factory=list)
    escalated: list[tuple[str, str]] = field(default_factory=list)
    skipped_no_reconciler: list[str] = field(default_factory=list)
    assessments: list[ReconciliationAssessment] = field(default_factory=list)

    @property
    def settled(self) -> int:
        """Total actions settled either way."""
        return len(self.settled_true) + len(self.settled_false)

    def as_dict(self) -> dict[str, Any]:
        """Report the settlement outcome as plain data."""
        return {
            "settled_occurred": self.settled_true,
            "settled_not_occurred": self.settled_false,
            "escalated": [
                {"action_id": action_id, "outcome": outcome}
                for action_id, outcome in self.escalated
            ],
            "no_reconciler_applied": self.skipped_no_reconciler,
            "settled_total": self.settled,
            "assessments": [item.as_dict() for item in self.assessments],
        }


def _action_label(assessment: ReconciliationAssessment) -> str:
    """Identify an assessment as the diagnostics headers do."""
    return f"{assessment.action_type}:{assessment.action_id[:12]}"


def resolve_reconcilers(
    source: Registry | Collection[ActionReconciler] | None,
) -> list[ActionReconciler]:
    """Normalize an explicit collection or a ``Registry`` into one list.

    ``None`` and empty both yield an empty list, which is the default path: no
    reconcilers configured means no plugin evidence, and the existing probe and
    human paths are untouched.

    A ``Registry`` is resolved by seam membership rather than by name lookup, so
    every registered ``ActionReconciler`` participates regardless of what it was
    registered under. The seam is ``runtime_checkable``, so structural
    conformance is all that is required of a plugin.
    """
    if source is None:
        return []
    if isinstance(source, Registry):
        return [
            service for service in source.all_services() if isinstance(service, ActionReconciler)
        ]
    return [item for item in source if isinstance(item, ActionReconciler)]


def select_reconcilers(
    action: Action, reconcilers: Iterable[ActionReconciler]
) -> list[ActionReconciler]:
    """The reconcilers to run for ``action``, in the order they will run.

    Order is by declared ``name`` after de-duplication, so the dispatch is
    deterministic regardless of how the collection was built. Selection itself is
    permissive: a reconciler that does not apply to this action says so by
    returning ``occurred=None``, which is cheaper and more honest than asking
    every plugin to declare the action types it covers.
    """
    by_name: dict[str, ActionReconciler] = {}
    for reconciler in reconcilers:
        by_name.setdefault(_reconciler_name(reconciler), reconciler)
    return [by_name[name] for name in sorted(by_name)]


def _reconciler_name(reconciler: ActionReconciler) -> str:
    """The reconciler's declared name, falling back to its class.

    The seam declares ``name`` but cannot enforce it, so a plugin that omits it
    still dispatches under its class name rather than failing the whole run.
    """
    declared = getattr(reconciler, "name", None)
    return declared if isinstance(declared, str) and declared else type(reconciler).__name__


def assess_action(
    action: Action, reconcilers: Iterable[ActionReconciler]
) -> ReconciliationAssessment:
    """Run every selected reconciler over ``action`` and merge their evidence.

    Never raises. A reconciler that raises or returns a malformed value is
    recorded as an error and blocks confirmation, landing the assessment in
    ``UNAVAILABLE`` rather than trusting the sources that survived it. That is
    the fail-closed property: a broken plugin cannot make an action more
    settlable, only less.
    """
    selected = select_reconcilers(action, reconcilers)
    evidence: list[ReconcilerEvidence] = []
    for reconciler in selected:
        evidence.append(_invoke(reconciler, action))
    return _merge(action, evidence)


def _invoke(reconciler: ActionReconciler, action: Action) -> ReconcilerEvidence:
    """Call one reconciler, isolating every failure mode into the evidence record."""
    name = _reconciler_name(reconciler)
    try:
        result = reconciler.reconcile(action)
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not break dispatch
        return ReconcilerEvidence(name=name, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(result, Reconciliation):
        return ReconcilerEvidence(
            name=name,
            error=f"returned {type(result).__name__}, expected Reconciliation",
        )
    if result.occurred is not None and not isinstance(result.occurred, bool):
        return ReconcilerEvidence(
            name=name,
            error=f"occurred has type {type(result.occurred).__name__}, expected bool or None",
        )
    return ReconcilerEvidence(
        name=name,
        occurred=result.occurred,
        external_id=result.external_id,
        note=result.note,
    )


def _merge(action: Action, evidence: Sequence[ReconcilerEvidence]) -> ReconciliationAssessment:
    """Reduce the evidence list to one of the four outcomes, fail-closed.

    Only evidence that returned an explicit verdict counts towards confirmation.
    ``None`` (no evidence) simply declines to vote. An *error* is different: a
    reconciler that crashed or returned a malformed value could not say whether
    it agrees, so its silence is not neutrality. One errored source blocks
    confirmation entirely and the action escalates; a plugin that is broken is
    a reason for a human to look, never a reason to settle on the others. That
    is what makes a broken reconciler unable to make an action *more* settlable.
    """
    verdicts = [item for item in evidence if item.occurred is not None]
    errors = [item for item in evidence if item.error is not None]

    if errors:
        reason = "; ".join(f"{item.name} errored: {item.error}" for item in errors)
        return ReconciliationAssessment(
            action_id=action.action_id,
            action_type=action.action_type,
            outcome=ReconciliationOutcome.UNAVAILABLE,
            evidence=list(evidence),
            reason=f"evidence unusable while a reconciler is broken: {reason}",
        )

    if not verdicts:
        if evidence:
            reason = (
                f"{len(evidence)} reconciler(s) applied and none returned evidence: "
                + ", ".join(item.name for item in evidence)
            )
        else:
            reason = "no reconciler applied to this action"
        return ReconciliationAssessment(
            action_id=action.action_id,
            action_type=action.action_type,
            outcome=ReconciliationOutcome.UNAVAILABLE,
            evidence=list(evidence),
            reason=reason,
        )

    positions = {item.occurred for item in verdicts}
    if len(positions) > 1:
        occurred_by = ", ".join(f"{item.name}=occurred" for item in verdicts if item.occurred)
        not_occurred_by = ", ".join(
            f"{item.name}=not_occurred" for item in verdicts if not item.occurred
        )
        reason = f"reconcilers disagree: {occurred_by} | {not_occurred_by}"
        return ReconciliationAssessment(
            action_id=action.action_id,
            action_type=action.action_type,
            outcome=ReconciliationOutcome.CONFLICTING,
            evidence=list(evidence),
            reason=reason,
        )

    occurred = next(iter(positions))
    assert isinstance(occurred, bool)
    return ReconciliationAssessment(
        action_id=action.action_id,
        action_type=action.action_type,
        outcome=(
            ReconciliationOutcome.CONFIRMED_OCCURRED
            if occurred
            else ReconciliationOutcome.CONFIRMED_NOT_OCCURRED
        ),
        evidence=list(evidence),
        reason=f"confirmed by {', '.join(item.name for item in verdicts)}",
    )


def settle_with_reconcilers(
    storage: Storage,
    run_id: str,
    reconcilers: Registry | Collection[ActionReconciler] | None,
    *,
    dry_run: bool = False,
) -> SettlementReport:
    """Assess every pending action of ``run_id`` and settle the ones evidence confirms.

    Only ``CONFIRMED_OCCURRED`` and ``CONFIRMED_NOT_OCCURRED`` settle, and they
    settle through :meth:`ActionLedger.reconcile` exactly as the probe registry's
    verdicts do, so plugin evidence can never bypass the ledger's settlement rules.
    ``UNAVAILABLE`` and ``CONFLICTING`` escalate to ``flag_for_review`` instead:
    the human queue grows, never shrinks, on anything less than confirmation.
    """
    from continuum.actions import ActionLedger  # local import: avoids a cycle at module load

    report = SettlementReport()
    if reconcilers is None:
        return report

    resolved = resolve_reconcilers(reconcilers)
    if not resolved:
        return report

    ledger = ActionLedger(storage, run_id)
    keys = _key_map(storage, run_id)
    for action in ledger.pending():
        assessment = assess_action(action, resolved)
        report.assessments.append(assessment)
        external_ids = [item.external_id for item in assessment.evidence if item.external_id]
        ext_id = external_ids[0] if external_ids else action.external_id
        label = f"{action.action_type}:{ext_id or action.action_id[:12]}"
        key = keys.get(action.action_id)
        if key is None:
            # A pending action that vanished from the fold between the pending()
            # call and this lookup. Report it rather than settle on a key we
            # cannot prove, fail-closed.
            report.escalated.append((action.action_id, assessment.outcome.value))
            continue
        if assessment.outcome is ReconciliationOutcome.CONFIRMED_OCCURRED:
            if dry_run:
                report.settled_true.append(label)
                continue
            ledger.reconcile(
                key,
                occurred=True,
                external_id=external_ids[0] if external_ids else None,
                note=assessment.reason,
            )
            report.settled_true.append(label)
        elif assessment.outcome is ReconciliationOutcome.CONFIRMED_NOT_OCCURRED:
            if dry_run:
                report.settled_false.append(label)
                continue
            ledger.reconcile(key, occurred=False, note=assessment.reason)
            report.settled_false.append(label)
        else:
            if dry_run:
                report.escalated.append((action.action_id, assessment.outcome.value))
                continue
            ledger.flag_for_review(key, f"[{assessment.outcome.value}] {assessment.reason}")
            report.escalated.append((action.action_id, assessment.outcome.value))
    return report


def _key_map(storage: Storage, run_id: str) -> dict[str, str]:
    """Map each pending action's ``action_id`` to the ledger key that folds to it.

    The fold is keyed by derived idempotency key while the ``Action`` record does
    not carry it, so the mapping is recovered by matching ``action_id`` against
    the run's folded ledger.

    Folds full history, not the live tail: pending actions claimed before a
    compaction live on as archived events (issue #647), and a live-only fold would
    make every one of them "vanish mid-reconcile" and escalate spuriously.
    """
    from continuum.actions.ledger import fold_action_events

    folded = fold_action_events(storage.read_all_events(run_id))
    return {candidate.action_id: key for key, candidate in folded.items()}
