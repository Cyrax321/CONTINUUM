"""Tests for the observability module (Phase 14 recovery dashboard).

The process-wide metrics collector that used to be tested here was removed
(issue #1032): it had no caller outside the tests, and the recovery ledger is
the durable record of what actually happened.
"""

from __future__ import annotations

from continuum.checkpoint.manager import RestoredRun
from continuum.environment.diff import EnvironmentDiff
from continuum.models import (
    Action,
    Component,
    ComponentValidationEntry,
    Goal,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    SemanticState,
    StateStatus,
    StateValidationResult,
)
from continuum.observability import render_dashboard
from continuum.recovery.engine import RecoveryDecision
from continuum.recovery.planner import RepairKind, RepairPlan, RepairStep
from continuum.state.validator import ValidationOutcome


def _decision(can_resume: bool, uncertain: tuple[Action, ...] = ()) -> RecoveryDecision:
    """Build a minimal RecoveryDecision for dashboard tests."""
    state = SemanticState(run_id="run_x", goal=Goal(description="recover"))
    contract = RecoveryContract(
        run_id="run_x",
        checkpoint_version=3,
        recovery_status=RecoverySafety.SAFE_TO_RESUME if can_resume else RecoverySafety.BLOCKED,
        verified=["goal"],
        invalidated=["external_dependency dataset (CONFLICTED)"],
        required_actions=[],
        next_allowed_action=None,
    )
    plan = RepairPlan(
        steps=[
            RepairStep(
                kind=RepairKind.REVALIDATE_DEPENDENCY,
                target="dataset",
                reason="version drift",
            )
        ]
    )
    validation = ValidationOutcome(
        state=state,
        report=StateValidationResult(
            run_id="run_x",
            checkpoint_version=3,
            statuses=[
                ComponentValidationEntry(component=Component.GOAL, status=StateStatus.VALID),
                ComponentValidationEntry(
                    component=Component.EXTERNAL_DEPENDENCY,
                    component_id="dataset",
                    status=StateStatus.CONFLICTED,
                    detail="v3 vs v4",
                ),
            ],
            safe_to_resume=can_resume,
        ),
        environment_diff=EnvironmentDiff(),
    )
    restored = RestoredRun(
        run_id="run_x", state=state, checkpoint=None, pending_events=0, replayed=True
    )
    return RecoveryDecision(
        run_id="run_x",
        mode=RecoveryMode.RESUME if can_resume else RecoveryMode.WAIT,
        contract=contract,
        plan=plan,
        validation=validation,
        restored=restored,
        uncertain_actions=uncertain,
        rationale=("dataset version drift",),
    )


def test_render_dashboard_contains_state_components_and_plan() -> None:
    out = render_dashboard(_decision(can_resume=False))
    assert "CONTINUUM RECOVERY DASHBOARD" in out
    assert "run_x" in out
    assert "external dependency dataset: conflicted" in out
    assert "REPAIRS REQUIRED" in out
    assert "revalidate_dependency dataset" in out
    assert "RATIONALE" in out


def test_render_dashboard_marks_resume() -> None:
    out = render_dashboard(_decision(can_resume=True))
    assert "safe to resume:     yes" in out
