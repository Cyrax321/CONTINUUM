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

from continuum.models import Evidence, Goal, SemanticState
from continuum.recovery import DependencyGraph, ImpactedSet


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
