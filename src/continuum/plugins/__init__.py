"""Plugin registry and capability seams for CONTINUUM.

A plugin is any object registered in a :class:`Registry` under a name, and
conforming to one of the four capability seams:

* :class:`~continuum.environment.EnvironmentProvider` (discover the world)
* :class:`StateExtractor` (map a framework's state onto CONTINUUM)
* :class:`ActionReconciler` (settle an uncertain side effect)
* :class:`ValidationRule` (domain-specific staleness)

``ActionReconciler`` has a consumer: :mod:`continuum.plugins.reconcile` dispatches
registered reconcilers over a run's uncertain actions and merges their evidence
(issue #765).
"""

from continuum.plugins.reconcile import (
    ReconcilerEvidence,
    ReconciliationAssessment,
    ReconciliationOutcome,
    SettlementReport,
    assess_action,
    resolve_reconcilers,
    select_reconcilers,
    settle_with_reconcilers,
)
from continuum.plugins.registry import Registration, Registry
from continuum.plugins.seams import (
    ActionReconciler,
    EnvironmentProvider,
    Reconciliation,
    StateExtractor,
    ValidationRule,
)

__all__ = [
    "Registry",
    "Registration",
    "EnvironmentProvider",
    "StateExtractor",
    "ActionReconciler",
    "ValidationRule",
    "Reconciliation",
    "ReconciliationOutcome",
    "ReconcilerEvidence",
    "ReconciliationAssessment",
    "SettlementReport",
    "resolve_reconcilers",
    "select_reconcilers",
    "assess_action",
    "settle_with_reconcilers",
]
