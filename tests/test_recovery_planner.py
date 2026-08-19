"""Repair planning in isolation, including branches the engine rarely reaches."""

from __future__ import annotations

from continuum.models import (
    Action,
    ActionStatus,
    Component,
    ComponentValidationEntry,
    RecoveryMode,
    StateStatus,
)
from continuum.recovery import SEVERITY, RepairKind, RepairPlan, plan_repairs
from continuum.recovery.planner import RepairStep


def finding(
    component: Component,
    status: StateStatus = StateStatus.STALE,
    component_id: str | None = None,
    detail: str = "",
) -> ComponentValidationEntry:
    return ComponentValidationEntry(
        component=component, component_id=component_id, status=status, detail=detail
    )


def uncertain(action_type: str = "github.create_issue", **kw: object) -> Action:
    return Action(
        run_id="run_1",
        action_type=action_type,
        status=ActionStatus.UNKNOWN,
        **kw,  # type: ignore[arg-type]
    )


# --- mapping findings to repairs ------------------------------------------- #


def test_valid_components_need_no_repair() -> None:
    plan = plan_repairs([finding(Component.EVIDENCE, StateStatus.VALID)])
    assert not plan
    assert plan.render() == "No repairs required."
    assert len(plan) == 0


def test_each_component_maps_to_its_own_repair_kind() -> None:
    plan = plan_repairs(
        [
            finding(Component.EXTERNAL_DEPENDENCY, component_id="dataset"),
            finding(Component.EVIDENCE, component_id="paper_1"),
            finding(Component.FINDING, component_id="f1"),
            finding(Component.DECISION, component_id="d1"),
            finding(Component.APPROVAL, StateStatus.EXPIRED, "ap_1"),
            finding(Component.MODEL, component_id="model-a"),
        ]
    )
    kinds = {s.kind for s in plan.steps}
    assert kinds == {
        RepairKind.REVALIDATE_DEPENDENCY,
        RepairKind.REDERIVE_EVIDENCE,
        RepairKind.REDERIVE_FINDING,
        RepairKind.REVIEW_DECISION,
        RepairKind.RENEW_APPROVAL,
        RepairKind.REVALIDATE_MODEL_STATE,
    }


def test_a_doubtful_goal_cannot_be_fixed_by_retrying() -> None:
    """Goal and progress problems need a person, not a re-run."""
    plan = plan_repairs(
        [
            finding(Component.GOAL, StateStatus.CONFLICTED),
            finding(Component.PROGRESS, StateStatus.UNKNOWN),
        ]
    )
    assert all(s.kind is RepairKind.HUMAN_REVIEW for s in plan.steps)
    assert plan.requires_human


def test_an_unmapped_component_escalates_rather_than_being_ignored() -> None:
    """A component nobody wrote a rule for must not silently pass."""
    plan = plan_repairs([finding(Component.PLAN, StateStatus.CONFLICTED, "step_3")])
    assert plan.steps[0].kind is RepairKind.HUMAN_REVIEW
    assert plan.steps[0].requires_human


def test_a_component_without_an_id_falls_back_to_its_name() -> None:
    plan = plan_repairs([finding(Component.EVIDENCE, StateStatus.UNKNOWN)])
    assert plan.steps[0].target == "evidence"


# --- uncertainty policy ----------------------------------------------------- #


def test_an_unverifiable_dependency_needs_a_person_by_default() -> None:
    plan = plan_repairs([finding(Component.EXTERNAL_DEPENDENCY, StateStatus.UNKNOWN, "dataset")])
    assert plan.requires_human


def test_tolerating_uncertainty_makes_it_automatic() -> None:
    plan = plan_repairs(
        [finding(Component.EXTERNAL_DEPENDENCY, StateStatus.UNKNOWN, "dataset")],
        strict_unknown=False,
    )
    assert not plan.requires_human


# --- action reconciliation -------------------------------------------------- #


def test_interrupted_actions_become_reconciliation_steps() -> None:
    plan = plan_repairs(uncertain_actions=[uncertain()])
    assert plan.steps[0].kind is RepairKind.RECONCILE_ACTION
    assert "may or may not have occurred" in plan.steps[0].reason


def test_strict_mode_makes_an_unknown_side_effect_a_human_step() -> None:
    """Issue #42: the engine escalates to REQUEST_HUMAN, so the step must agree.

    Otherwise ``plan.requires_human`` is False and ``next_allowed_action`` is an
    automatic reconcile the contract permits, contradicting the mode.
    """
    plan = plan_repairs(uncertain_actions=[uncertain()], strict_unknown=True)
    assert plan.steps[0].requires_human
    assert plan.requires_human
    # The reason states what happened, not who handles it: it was never escalated.
    assert "escalated" not in plan.steps[0].reason


def test_tolerating_unknown_side_effects_allows_an_automatic_reconcile() -> None:
    plan = plan_repairs(uncertain_actions=[uncertain()], strict_unknown=False)
    assert not plan.steps[0].requires_human
    assert not plan.requires_human


def test_an_escalated_action_is_not_sent_back_through_automation() -> None:
    """It already defeated automatic reconciliation once; retrying would loop."""
    action = Action(
        run_id="run_1", action_type="payment.charge", status=ActionStatus.REQUIRES_REVIEW
    )
    plan = plan_repairs(uncertain_actions=[action])
    assert plan.steps[0].requires_human
    assert "escalated for review" in plan.steps[0].reason


def test_settled_actions_need_no_reconciliation() -> None:
    for status in (ActionStatus.COMPLETED, ActionStatus.FAILED, ActionStatus.COMPENSATED):
        action = Action(run_id="run_1", action_type="x.do", status=status)
        assert not plan_repairs(uncertain_actions=[action])


# --- ordering and determinism ---------------------------------------------- #


def test_reconciliation_precedes_every_other_repair() -> None:
    plan = plan_repairs(
        [
            finding(Component.DECISION, component_id="d1"),
            finding(Component.EXTERNAL_DEPENDENCY, component_id="dataset"),
        ],
        uncertain_actions=[uncertain()],
    )
    assert plan.steps[0].kind is RepairKind.RECONCILE_ACTION


def test_prerequisites_sort_before_dependents() -> None:
    plan = plan_repairs(
        [
            finding(Component.DECISION, component_id="d1"),
            finding(Component.FINDING, component_id="f1"),
            finding(Component.EVIDENCE, component_id="e1"),
            finding(Component.EXTERNAL_DEPENDENCY, component_id="dataset"),
        ]
    )
    assert [s.kind for s in plan.steps] == [
        RepairKind.REVALIDATE_DEPENDENCY,
        RepairKind.REDERIVE_EVIDENCE,
        RepairKind.REDERIVE_FINDING,
        RepairKind.REVIEW_DECISION,
    ]


def test_the_same_findings_always_produce_the_same_plan() -> None:
    findings = [
        finding(Component.FINDING, component_id="f2"),
        finding(Component.FINDING, component_id="f1"),
        finding(Component.EVIDENCE, component_id="e1"),
    ]
    first = plan_repairs(findings)
    second = plan_repairs(list(reversed(findings)))
    assert [s.action_name for s in first.steps] == [s.action_name for s in second.steps]


def test_duplicate_findings_collapse_to_one_step() -> None:
    plan = plan_repairs(
        [
            finding(Component.EXTERNAL_DEPENDENCY, component_id="dataset"),
            finding(Component.EXTERNAL_DEPENDENCY, StateStatus.CONFLICTED, "dataset"),
        ]
    )
    assert len(plan) == 1


# --- plan surface ----------------------------------------------------------- #


def test_a_plan_exposes_its_first_and_blocking_steps() -> None:
    plan = plan_repairs([finding(Component.EVIDENCE, component_id="e1")])
    assert plan.first is not None
    assert plan.first.action_name == "rederive_evidence:e1"
    assert len(plan.blocking) == 1
    assert bool(plan)


def test_an_empty_plan_has_no_first_step() -> None:
    assert RepairPlan().first is None
    assert not RepairPlan().blocking
    assert not RepairPlan().requires_human


def test_steps_render_with_their_operator() -> None:
    auto = RepairStep(kind=RepairKind.REDERIVE_EVIDENCE, target="e1", reason="source moved")
    human = RepairStep(kind=RepairKind.RENEW_APPROVAL, target="ap_1", requires_human=True)

    assert auto.render().startswith("[auto]")
    assert "source moved" in auto.render()
    assert human.render().startswith("[human]")


def test_a_plan_renders_as_a_numbered_list() -> None:
    plan = plan_repairs(
        [
            finding(Component.EXTERNAL_DEPENDENCY, component_id="dataset"),
            finding(Component.EVIDENCE, component_id="e1"),
        ]
    )
    rendered = plan.render()
    assert "1. " in rendered and "2. " in rendered


def test_severity_is_a_total_order_over_every_mode() -> None:
    """Every mode must be rankable, or max() could pick arbitrarily."""
    assert set(SEVERITY) == set(RecoveryMode)
    assert len(set(SEVERITY.values())) == len(RecoveryMode)
    assert SEVERITY[RecoveryMode.RESUME] < SEVERITY[RecoveryMode.REPAIR_AND_RESUME]
    assert SEVERITY[RecoveryMode.REPAIR_AND_RESUME] < SEVERITY[RecoveryMode.REQUEST_HUMAN]
    assert SEVERITY[RecoveryMode.REQUEST_HUMAN] < SEVERITY[RecoveryMode.ABORT]
