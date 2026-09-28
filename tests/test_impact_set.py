"""Direct contracts for ``ImpactedSet`` (issue #967).

``empty`` and ``all_items`` are the two smallest contracts of the impact
subgraph, but they had no direct test: ``empty`` was never referenced in
``tests/`` at all, and ``all_items`` was only exercised indirectly through
``impacted_by`` in ``test_phase2.py``. A regression that flipped ``empty``
would surface only as a wrongly scoped repair plan downstream. These tests
pin both properties directly on the dataclass, plus the wiring that produces
an empty set when a graph is queried for resources it does not know.
"""

from __future__ import annotations

import pytest

from continuum.models import Decision, Evidence, Finding, Goal, SemanticState, StateStatus
from continuum.recovery import DependencyGraph, ImpactedSet
from continuum.state.validator import ResourceChange, StateValidator


def test_empty_is_true_on_a_fresh_set() -> None:
    # Broken resources alone impact no state item: nothing to re-derive.
    assert ImpactedSet(resources=frozenset({"dataset"})).empty is True


@pytest.mark.parametrize("field", ["evidence", "findings", "decisions"])
def test_empty_is_false_when_any_collection_has_items(field: str) -> None:
    impacted = ImpactedSet(resources=frozenset({"dataset"}), **{field: frozenset({"item_1"})})
    assert impacted.empty is False


def test_all_items_is_the_union_of_the_three_collections() -> None:
    impacted = ImpactedSet(
        resources=frozenset({"dataset"}),
        evidence=frozenset({"ev_a"}),
        findings=frozenset({"f_a"}),
        decisions=frozenset({"dec_a"}),
    )
    assert impacted.all_items == frozenset({"ev_a", "f_a", "dec_a"})


def test_all_items_is_empty_on_a_fresh_set() -> None:
    assert ImpactedSet(resources=frozenset({"dataset"})).all_items == frozenset()


# --- the seam: where the graph produces the set ------------------------------ #


def test_impacted_by_unknown_resource_yields_an_empty_set() -> None:
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="pin ImpactedSet contracts"),
        evidence=[Evidence(evidence_id="ev_a", summary="a", source="dataset")],
    )
    impacted = DependencyGraph(state).impacted_by({"unknown"})

    # The unknown resource is recorded, but nothing in the state derives
    # from it, so the set a scoped plan would read from is empty.
    assert impacted.resources == frozenset({"unknown"})
    assert impacted.empty is True
    assert impacted.all_items == frozenset()


def test_impacted_by_cascades_finding_to_finding_citations() -> None:
    """Finding-to-finding citation edges cascade taint to fixpoint (issues #739, #1475)."""
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="finding-to-finding taint cascade"),
        evidence=[
            Evidence(evidence_id="E1", summary="e1", source="dataset"),
            Evidence(evidence_id="E2", summary="e2", source="clean_source"),
        ],
        findings=[
            Finding(finding_id="F1", claim="f1", evidence=["E1"]),
            Finding(finding_id="F2", claim="f2", evidence=["F1"]),
            Finding(finding_id="F3", claim="f3", evidence=["F2"]),
            Finding(finding_id="F_clean", claim="clean", evidence=["E2"]),
        ],
        decisions=[
            Decision(decision_id="D1", decision="d1", evidence=["F3"]),
            Decision(decision_id="D_clean", decision="clean", evidence=["F_clean"]),
        ],
    )

    impacted = DependencyGraph(state).impacted_by({"dataset"})

    assert impacted.evidence == {"E1"}
    assert impacted.findings == {"F1", "F2", "F3"}
    assert impacted.decisions == {"D1"}
    assert "F_clean" not in impacted.all_items
    assert "D_clean" not in impacted.all_items


def test_impacted_by_cascades_forward_referencing_findings() -> None:
    """Findings citing one declared after them in state.findings are tainted (issue #1475)."""
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="forward reference finding cascade"),
        evidence=[Evidence(evidence_id="E1", summary="e1", source="dataset")],
        findings=[
            # F2 is declared before F1 but cites F1
            Finding(finding_id="F2", claim="f2", evidence=["F1"]),
            Finding(finding_id="F1", claim="f1", evidence=["E1"]),
        ],
        decisions=[Decision(decision_id="D1", decision="d1", evidence=["F2"])],
    )

    impacted = DependencyGraph(state).impacted_by({"dataset"})

    assert impacted.evidence == {"E1"}
    assert impacted.findings == {"F1", "F2"}
    assert impacted.decisions == {"D1"}


def test_impacted_by_parity_with_state_validator() -> None:
    """DependencyGraph.impacted_by matches StateValidator._propagate exactly (issue #1475)."""
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="validator parity"),
        evidence=[
            Evidence(evidence_id="E1", summary="e1", source="db"),
            Evidence(evidence_id="E2", summary="e2", source="clean"),
        ],
        findings=[
            Finding(finding_id="F1", claim="f1", evidence=["E1"]),
            Finding(finding_id="F2", claim="f2", evidence=["F1"]),
            Finding(finding_id="F3", claim="f3", evidence=["F2"]),
        ],
        decisions=[Decision(decision_id="D1", decision="d1", evidence=["F3"])],
    )

    impacted = DependencyGraph(state).impacted_by({"db"})

    validator = StateValidator()
    propagated = validator._propagate(state, {"db": ResourceChange.CHANGED}, [])

    stale_findings = {
        f.finding_id for f in propagated.findings if f.status is not StateStatus.VALID
    }
    stale_decisions = {
        d.decision_id for d in propagated.decisions if d.status is not StateStatus.VALID
    }

    assert impacted.findings == stale_findings
    assert impacted.decisions == stale_decisions


def test_impacted_by_handles_finding_cycles_safely() -> None:
    """Circular finding citations terminate without infinite loops."""
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="circular finding citations"),
        evidence=[Evidence(evidence_id="E1", summary="e1", source="dataset")],
        findings=[
            Finding(finding_id="F1", claim="f1", evidence=["E1", "F2"]),
            Finding(finding_id="F2", claim="f2", evidence=["F1"]),
        ],
    )

    impacted = DependencyGraph(state).impacted_by({"dataset"})
    assert impacted.findings == {"F1", "F2"}
