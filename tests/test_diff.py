from __future__ import annotations

import pytest

from continuum.models import (
    Component,
    ConstraintPin,
    Decision,
    DiffKind,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelState,
    PendingWork,
    Progress,
    Provenance,
    SemanticState,
    StateStatus,
)
from continuum.state.diff import diff_states, render_diff


def make_state(**overrides: object) -> SemanticState:
    base: dict[str, object] = {
        "run_id": "run_4821",
        "goal": Goal(description="Analyze 100 documents"),
        "progress": Progress(total=100, completed=10, pending=90),
    }
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


def kinds(diff: object, component: Component) -> set[DiffKind]:
    return {e.kind for e in diff.entries if e.component is component}  # type: ignore[attr-defined]


def test_identical_states_produce_no_entries() -> None:
    state = make_state()
    diff = diff_states(state, state)
    assert diff.entries == []
    assert "No semantic change" in render_diff(diff)


def test_reordering_a_list_is_not_a_change() -> None:
    a = Decision(decision_id="d1", decision="first")
    b = Decision(decision_id="d2", decision="second")
    before = make_state(decisions=[a, b])
    after = make_state(decisions=[b, a])
    assert diff_states(before, after).entries == []


def test_added_and_removed_components_are_reported() -> None:
    before = make_state(findings=[Finding(finding_id="f1", claim="old")])
    after = make_state(findings=[Finding(finding_id="f81", claim="new")])
    diff = diff_states(before, after)
    assert kinds(diff, Component.FINDING) == {DiffKind.ADDED, DiffKind.REMOVED}


def test_pin_add_and_retract_are_reported() -> None:
    """Pins are the active constraint set a resuming agent must honour.

    The diff predates pins, so adding or retracting one used to yield an empty
    diff and `continuum diff` said "no semantic change" (issue #740).
    """
    pin = ConstraintPin(
        constraint_id="c1", sha256="a" * 64, status="active", provenance=Provenance()
    )
    added = diff_states(make_state(), make_state(pins={"c1": pin}))
    assert kinds(added, Component.PIN) == {DiffKind.ADDED}

    retracted = diff_states(make_state(pins={"c1": pin}), make_state())
    assert kinds(retracted, Component.PIN) == {DiffKind.REMOVED}


def test_a_pin_whose_digest_changes_is_reported() -> None:
    """Same label, different constraint text: the digest is the identity."""
    before = make_state(
        pins={"c1": ConstraintPin(constraint_id="c1", sha256="a" * 64, provenance=Provenance())}
    )
    after = make_state(
        pins={"c1": ConstraintPin(constraint_id="c1", sha256="b" * 64, provenance=Provenance())}
    )
    diff = diff_states(before, after)
    assert kinds(diff, Component.PIN) == {DiffKind.CHANGED}


def test_unchanged_pins_produce_no_entries() -> None:
    pin = ConstraintPin(
        constraint_id="c1", sha256="a" * 64, status="active", provenance=Provenance()
    )
    before = make_state(pins={"c1": pin})
    after = make_state(pins={"c1": pin})
    assert diff_states(before, after).entries == []


def test_invalidation_is_distinguished_from_an_ordinary_change() -> None:
    before = make_state(decisions=[Decision(decision_id="d7", decision="keep")])
    after = make_state(
        decisions=[Decision(decision_id="d7", decision="keep", status=StateStatus.INVALID)]
    )
    diff = diff_states(before, after)
    assert kinds(diff, Component.DECISION) == {DiffKind.INVALIDATED}

    softened = make_state(decisions=[Decision(decision_id="d7", decision="keep", reason="because")])
    assert kinds(diff_states(before, softened), Component.DECISION) == {DiffKind.CHANGED}


def test_dependency_version_change_is_visible() -> None:
    before = make_state(
        external_dependencies=[ExternalDependency(resource="dataset", version="v3")]
    )
    after = make_state(external_dependencies=[ExternalDependency(resource="dataset", version="v4")])
    diff = diff_states(before, after)
    entry = next(e for e in diff.entries if e.component is Component.EXTERNAL_DEPENDENCY)
    assert entry.before == "v3"
    assert entry.after == "v4"
    assert "v3 → v4" in entry.detail


def test_progress_changes_are_reported_per_counter() -> None:
    before = make_state()
    after = make_state(progress=Progress(total=100, completed=20, pending=80))
    diff = diff_states(before, after)
    changed = {e.component_id for e in diff.entries if e.component is Component.PROGRESS}
    assert changed == {"completed", "pending"}


def test_goal_revision_reports_description_and_version() -> None:
    before = make_state()
    after = make_state(goal=Goal(description="Analyze 200 documents", version=2))
    details = [e.detail for e in diff_states(before, after).entries]
    assert any("description:" in d for d in details)
    assert any("v1 → v2" in d for d in details)


def test_model_switch_is_reported() -> None:
    before = make_state(model=ModelState(model="model-a"))
    after = make_state(model=ModelState(model="model-b"))
    diff = diff_states(before, after)
    assert kinds(diff, Component.MODEL) == {DiffKind.CHANGED}


def test_evidence_and_work_are_diffed() -> None:
    before = make_state()
    after = make_state(
        evidence=[Evidence(evidence_id="paper_1", summary="study")],
        pending_work=[PendingWork(task_id="t1", description="re-run experiment")],
    )
    diff = diff_states(before, after)
    assert kinds(diff, Component.EVIDENCE) == {DiffKind.ADDED}
    assert kinds(diff, Component.PENDING_WORK) == {DiffKind.ADDED}


def test_diff_output_is_deterministic() -> None:
    before = make_state()
    after = make_state(
        findings=[Finding(finding_id=f"f{i}", claim=str(i)) for i in range(5)],
        evidence=[Evidence(evidence_id=f"e{i}") for i in range(5)],
    )
    first = render_diff(diff_states(before, after))
    second = render_diff(diff_states(before, after))
    assert first == second


def test_diff_across_runs_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot diff across runs"):
        diff_states(make_state(), make_state(run_id="other"))


def test_rendered_diff_uses_readable_sigils() -> None:
    before = make_state(decisions=[Decision(decision_id="d7", decision="keep")])
    after = make_state(
        decisions=[Decision(decision_id="d7", decision="keep", status=StateStatus.STALE)],
        findings=[Finding(finding_id="f81", claim="new finding")],
        external_dependencies=[ExternalDependency(resource="dataset", version="v4")],
    )
    rendered = render_diff(diff_states(before, after))
    assert "+ finding f81" in rendered
    assert "! decision d7" in rendered
    assert "+ external dependency dataset" in rendered
