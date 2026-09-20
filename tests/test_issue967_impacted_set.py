"""Direct contracts for the dependency impact set (issue #967)."""

from __future__ import annotations

from continuum.recovery.impact import ImpactedSet


def test_impacted_set_is_empty_without_impacted_items() -> None:
    impacted = ImpactedSet(resources=frozenset())

    assert impacted.empty is True
    assert impacted.all_items == frozenset()


def test_impacted_set_is_nonempty_for_each_item_kind() -> None:
    for field in ("evidence", "findings", "decisions"):
        impacted = ImpactedSet(resources=frozenset(), **{field: frozenset({field})})

        assert impacted.empty is False
        assert impacted.all_items == frozenset({field})


def test_impacted_set_all_items_unites_every_kind() -> None:
    impacted = ImpactedSet(
        resources=frozenset({"dataset"}),
        evidence=frozenset({"ev-1"}),
        findings=frozenset({"finding-1"}),
        decisions=frozenset({"decision-1"}),
    )

    assert impacted.all_items == frozenset({"ev-1", "finding-1", "decision-1"})
