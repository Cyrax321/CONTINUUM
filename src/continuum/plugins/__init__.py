"""Plugin registry and capability seams for CONTINUUM.

A plugin is any object registered in a :class:`Registry` under a name, and
conforming to one of the four capability seams:

* :class:`~continuum.environment.EnvironmentProvider` (discover the world)
* :class:`StateExtractor` (map a framework's state onto CONTINUUM)
* :class:`ActionReconciler` (settle an uncertain side effect)
* :class:`ValidationRule` (domain-specific staleness; the built-in
  :class:`~continuum.plugins.validation_rules.RevokedApprovalRule` is a worked
  example, and :func:`~continuum.plugins.validation_rules.check_validation_rule`
  is the conformance check a rule author runs against their own rule)
"""

from continuum.plugins.registry import Registration, Registry
from continuum.plugins.seams import (
    ActionReconciler,
    EnvironmentProvider,
    Reconciliation,
    StateExtractor,
    ValidationRule,
)
from continuum.plugins.validation_rules import (
    ConformanceReport,
    RevokedApprovalRule,
    check_validation_rule,
    conformance_states,
)

__all__ = [
    "Registry",
    "Registration",
    "EnvironmentProvider",
    "StateExtractor",
    "ActionReconciler",
    "ValidationRule",
    "Reconciliation",
    "ConformanceReport",
    "RevokedApprovalRule",
    "check_validation_rule",
    "conformance_states",
]
