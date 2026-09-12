"""Dependency impact analysis for localized recovery.

The semantic state forms a derivation graph:

    external_dependency -> evidence          (via ``Evidence.source``)
    evidence            -> finding           (via ``Finding.evidence``)
    evidence | finding  -> decision          (via ``Decision.evidence``)

When an external dependency moves (v3 -> v4) or disappears, only the subgraph
that transitively references it is suspect. This module makes that subgraph
explicit and queryable so recovery can stay local: re-derive the impacted
evidence/findings/decisions and leave everything else untouched. That is the
core of dependency-localized repair, and it is what lets a run keep the clean
parts of its state instead of discarding the whole bundle.

The propagation here mirrors ``StateValidator._propagate`` exactly, so the two
never disagree about what a broken resource invalidates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from continuum.models import SemanticState


@dataclass(frozen=True, slots=True)
class ImpactedSet:
    """The precise subgraph invalidated by a set of broken resources."""

    resources: frozenset[str]
    evidence: frozenset[str] = frozenset()
    findings: frozenset[str] = frozenset()
    decisions: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        """Return whether no evidence, finding, or decision is impacted."""
        return not (self.evidence or self.findings or self.decisions)

    @property
    def all_items(self) -> frozenset[str]:
        """Every impacted evidence/finding/decision id, for plan scoping."""
        return self.evidence | self.findings | self.decisions


class DependencyGraph:
    """The derivation graph over a ``SemanticState``.

    Build it once per state; ``impacted_by`` is then cheap to call for any set
    of broken dependency resources.
    """

    def __init__(self, state: SemanticState) -> None:
        self._state = state

        # resource name -> evidence ids that source from it
        self._resource_to_evidence: dict[str, list[str]] = {}
        for item in state.evidence:
            if item.source is not None:
                self._resource_to_evidence.setdefault(item.source, []).append(item.evidence_id)

        # finding id -> evidence ids it rests on
        self._finding_evidence: dict[str, frozenset[str]] = {
            f.finding_id: frozenset(f.evidence) for f in state.findings
        }

        # decision id -> evidence/finding ids it rests on
        self._decision_support: dict[str, frozenset[str]] = {
            d.decision_id: frozenset(d.evidence) for d in state.decisions
        }

    def impacted_by(self, resources: Iterable[str]) -> ImpactedSet:
        """Return exactly the subgraph invalidated by ``resources``.

        A resource invalidates the evidence sourced from it, every finding that
        cites any of that evidence, and every decision that cites any of that
        evidence or any of those findings.
        """
        scoped = frozenset(resources)
        evidence = frozenset(
            eid for res in scoped for eid in self._resource_to_evidence.get(res, ())
        )
        findings = frozenset(fid for fid, evs in self._finding_evidence.items() if evs & evidence)
        support = evidence | findings
        decisions = frozenset(did for did, sup in self._decision_support.items() if sup & support)
        return ImpactedSet(
            resources=scoped,
            evidence=evidence,
            findings=findings,
            decisions=decisions,
        )
