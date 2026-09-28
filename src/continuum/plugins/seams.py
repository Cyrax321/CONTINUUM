"""The four capability seams a plugin can implement.

Each seam is a ``Protocol`` (interface), with one or more providers
(implementations) and consumers inside CONTINUUM. Swapping a provider changes
behavior without changing call sites. See references/integration-architecture.md
section 3.2.

The ``EnvironmentProvider`` seam already ships in ``continuum.environment``;
it is re-exported here so all four seams live behind one import. The
``StateExtractor`` seam is defined in ``continuum.state.extractor`` and
re-exported here so all four seams share one import surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from continuum.environment import EnvironmentProvider
from continuum.models import (
    Action,
    ComponentValidationEntry,
    EnvironmentSnapshot,
    SemanticState,
)
from continuum.state.extractor import StateExtractor


@dataclass
class Reconciliation:
    """The outcome an ``ActionReconciler`` reports for an uncertain side effect.

    ``occurred`` is three-valued, and the third value is not a hedge:

    - ``True``: confirmed occurrence: evidence the effect exists was found.
    - ``False``: confirmed non-occurrence: evidence of absence was found.
    - ``None``: the reconciler looked and could not obtain evidence. This is
      *not* evidence of absence, and dispatch must never read it as one: an
      unreachable API says nothing about whether the earlier request landed.

    The fourth outcome, conflicting evidence, is not something one reconciler
    reports. It emerges when several reconcilers disagree, so it belongs to the
    merge layer (``continuum.plugins.reconcile``) rather than to this type.
    """

    occurred: bool | None = None
    external_id: str | None = None
    note: str = ""


@runtime_checkable
class ActionReconciler(Protocol):
    """Resolves an uncertain side effect using external evidence."""

    name: str

    def reconcile(self, action: Action) -> Reconciliation: ...


@runtime_checkable
class ValidationRule(Protocol):
    """Domain-specific staleness beyond environment changes."""

    name: str

    def evaluate(
        self, state: SemanticState, environment: EnvironmentSnapshot | None = None
    ) -> list[ComponentValidationEntry]: ...


__all__ = [
    "EnvironmentProvider",
    "StateExtractor",
    "ActionReconciler",
    "ValidationRule",
    "Reconciliation",
]
