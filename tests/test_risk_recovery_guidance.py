"""Regression tests for risk-informed recovery assessment and located repair guidance.

Issue #1423:
1. RecoveryEngine.assess() evaluates active RISK_OBSERVED events, consulting
   active RiskPolicy to determine highest-severity required mode, while preserving
   maximum-severity precedence (tamper or uncertain external effects always take
   precedence over soft risk signals).
2. plan_repairs() generates located repair steps:
   - For loop: locates repeated action signatures/steps and emits instructions to
     replan excluding repeating trajectory.
   - For meltdown: identifies last verified fact-gathering step prior to the anomalous
     cascade and targets rollback there.
   - Includes step_id, trigger, and episode_id in repair rationale.
3. Deterministic mode selection and located guidance across all trigger classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.events import EventType
from continuum.models import (
    Evidence,
    Origin,
    PlanStep,
    PlanStepStatus,
    RecoveryMode,
    RecoverySafety,
    Run,
    SemanticState,
    StateStatus,
)
from continuum.recovery import RecoveryEngine, RepairKind, plan_repairs
from continuum.recovery.risk import ingest_risk
from continuum.storage import SQLiteStorage


def _make_run(store: SQLiteStorage, run_id: str = "r1", goal: str = "test goal") -> None:
    store.create_run_started(Run(run_id=run_id, goal=goal))


# --- Deterministic mode selection across all trigger classes ---


@pytest.mark.parametrize(
    ("trigger", "expected_mode"),
    [
        ("loop", RecoveryMode.REPLAN),
        ("error_cascade", RecoveryMode.WAIT),
        ("latency_anomaly", RecoveryMode.RESUME),
        ("token_runaway", RecoveryMode.WAIT),
        ("silent_abort", RecoveryMode.REPAIR_AND_RESUME),
        ("meltdown", RecoveryMode.ROLLBACK),
        ("side_effect_duplicate", RecoveryMode.ABORT),
        ("governance_decay", RecoveryMode.REQUEST_HUMAN),
    ],
)
def test_deterministic_mode_selection_across_all_triggers(
    tmp_path: Path, trigger: str, expected_mode: RecoveryMode
) -> None:
    db = str(tmp_path / f"{trigger}.db")
    with SQLiteStorage(db) as store:
        run_id = f"run_{trigger}"
        _make_run(store, run_id=run_id)
        ingest_risk(store, run_id, {"trigger": trigger, "score": 0.95})

        decision = RecoveryEngine(store).assess(run_id, replay=False)
        assert decision.mode is expected_mode
        if trigger != "latency_anomaly":
            assert len(decision.contract.triggering_risks) == 1
        else:
            assert len(decision.contract.triggering_risks) == 0


# --- Located repair guidance for loop ---


def test_loop_generates_located_replan_step_in_plan_repairs() -> None:
    risk_events = [
        {
            "trigger": "loop",
            "step_id": "fetch_user_profile",
            "episode_id": "ep_101",
            "score": 0.9,
        }
    ]
    plan = plan_repairs(risk_events=risk_events)
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.REVIEW_DECISION
    assert step.target == "fetch_user_profile"
    assert step.requires_human is False
    assert step.blocking is True

    # Rationale includes step_id, trigger, and episode_id
    assert "replan excluding repeating trajectory" in step.reason
    assert "step_id: fetch_user_profile" in step.reason
    assert "trigger: loop" in step.reason
    assert "episode_id: ep_101" in step.reason


def test_loop_locates_repeated_action_signatures_from_detail() -> None:
    risk_events = [
        {
            "trigger": "loop",
            "detail": {
                "action_signature": "github.search_issues",
                "episode_id": "ep_42",
            },
        }
    ]
    plan = plan_repairs(risk_events=risk_events)
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.REVIEW_DECISION
    assert step.target == "github.search_issues"
    assert "trigger: loop" in step.reason
    assert "episode_id: ep_42" in step.reason


def test_loop_deduplicates_repeated_steps_in_engine_assessment(tmp_path: Path) -> None:
    db = str(tmp_path / "loop_dedup.db")
    with SQLiteStorage(db) as store:
        run_id = "run_loop"
        _make_run(store, run_id=run_id)

        # Ingest repeat observations for step A and step B
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "step_a", "episode_id": "ep_1"})
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "step_b", "episode_id": "ep_1"})
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "step_a", "episode_id": "ep_1"})

        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode is RecoveryMode.REPLAN
    loop_steps = decision.plan.of_kind(RepairKind.REVIEW_DECISION)
    targets = [s.target for s in loop_steps]
    assert targets == ["step_a", "step_b"]
    for s in loop_steps:
        assert "replan excluding repeating trajectory" in s.reason
        assert "trigger: loop" in s.reason
        assert "episode_id: ep_1" in s.reason


# --- Located repair guidance for meltdown ---


def test_meltdown_identifies_last_verified_fact_gathering_step_from_plan() -> None:
    state = SemanticState(
        run_id="run_m",
        goal={"description": "objective"},
        plan=[
            PlanStep(
                step_id="step_gather",
                description="gather input data from external api",
                status=PlanStepStatus.VERIFIED,
                evidence=["ev_1"],
            ),
            PlanStep(
                step_id="step_process",
                description="process records",
                status=PlanStepStatus.VERIFIED,
            ),
            PlanStep(
                step_id="step_cascade",
                description="uncontrolled cascade recursion",
                status=PlanStepStatus.IN_PROGRESS,
            ),
        ],
    )
    risk_events = [
        {
            "trigger": "meltdown",
            "step_id": "step_cascade",
            "episode_id": "ep_cascade_9",
            "score": 0.99,
        }
    ]
    plan = plan_repairs(risk_events=risk_events, state=state)
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.HUMAN_REVIEW
    assert step.target == "step_gather"
    assert step.requires_human is True
    assert step.blocking is True

    # Rationale includes step_id, trigger, and episode_id
    assert "target rollback to last verified fact-gathering step step_gather" in step.reason
    assert "step_id: step_cascade" in step.reason
    assert "trigger: meltdown" in step.reason
    assert "episode_id: ep_cascade_9" in step.reason


def test_meltdown_identifies_last_verified_fact_from_evidence_when_plan_absent() -> None:
    state = SemanticState(
        run_id="run_ev",
        goal={"description": "objective"},
        evidence=[
            Evidence(evidence_id="ev_fact_1", summary="fact 1", status=StateStatus.VALID),
            Evidence(evidence_id="ev_fact_2", summary="fact 2", status=StateStatus.VALID),
        ],
    )
    risk_events = [
        {
            "trigger": "meltdown",
            "step_id": "step_fail",
            "episode_id": "ep_12",
        }
    ]
    plan = plan_repairs(risk_events=risk_events, state=state)
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.HUMAN_REVIEW
    assert step.target == "ev_fact_2"
    assert step.requires_human is True
    assert "target rollback to last verified fact-gathering step ev_fact_2" in step.reason
    assert "step_id: step_fail" in step.reason
    assert "trigger: meltdown" in step.reason
    assert "episode_id: ep_12" in step.reason


def test_meltdown_engine_assessment_sets_blocked_safety(tmp_path: Path) -> None:
    db = str(tmp_path / "meltdown_engine.db")
    with SQLiteStorage(db) as store:
        run_id = "run_meltdown"
        _make_run(store, run_id=run_id)
        ingest_risk(
            store,
            run_id,
            {
                "trigger": "meltdown",
                "step_id": "step_loop_blowup",
                "episode_id": "ep_99",
                "detail": {"fact_gathering_step": "fetch_baseline_evidence"},
            },
        )
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode is RecoveryMode.ROLLBACK
    assert decision.contract.recovery_status is RecoverySafety.BLOCKED
    assert decision.contract.next_allowed_action is None

    human_steps = decision.plan.of_kind(RepairKind.HUMAN_REVIEW)
    assert len(human_steps) == 1
    step = human_steps[0]
    assert step.target == "fetch_baseline_evidence"
    assert "trigger: meltdown" in step.reason
    assert "episode_id: ep_99" in step.reason
    assert "step_id: step_loop_blowup" in step.reason


# --- Maximum-severity precedence ---


def test_uncertain_external_actions_precede_soft_risk_signal(tmp_path: Path) -> None:
    db = str(tmp_path / "precedence_uncertain.db")
    with SQLiteStorage(db) as store:
        run_id = "run_prec"
        _make_run(store, run_id=run_id)

        # Create an uncertain external action
        ledger = ActionLedger(store, run_id)
        claim = ledger.claim("external.webhook", {"payload": "data"}, key="k1")
        assert claim.fresh is True

        # Ingest a soft risk signal (loop -> REPLAN, severity 2)
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "step_repeat"})

        # Strict mode: uncertain action proposes REQUEST_HUMAN (severity 4)
        decision = RecoveryEngine(store).assess(run_id, replay=False)
        assert decision.mode is RecoveryMode.REQUEST_HUMAN
        assert any("external side effect" in r for r in decision.rationale)


def test_unprojectable_log_fold_precedes_soft_risk_signal(tmp_path: Path) -> None:
    db = str(tmp_path / "precedence_unprojectable.db")
    with SQLiteStorage(db) as store:
        run_id = "run_unproj"
        store.create_run(Run(run_id=run_id, goal="g"))
        store.append_event(
            run_id,
            EventType.RUN_STARTED,
            {"goal": "g", "total": 10},
            source=Origin.DETERMINISTIC,
        )
        # Append over-total task updated event to cause unprojectable fold
        store.append_event(
            run_id,
            EventType.TASK_UPDATED,
            {"completed": 999, "failed": 0},
            source=Origin.DETERMINISTIC,
        )
        ingest_risk(store, run_id, {"trigger": "silent_abort"})

        decision = RecoveryEngine(store).assess(run_id, replay=True)
        assert decision.mode is RecoveryMode.REQUEST_HUMAN
        assert any("event log stops folding" in r for r in decision.rationale)


def test_severe_risk_signal_precedes_lower_severity_risk(tmp_path: Path) -> None:
    db = str(tmp_path / "precedence_risks.db")
    with SQLiteStorage(db) as store:
        run_id = "run_severe"
        _make_run(store, run_id=run_id)

        # Ingest loop (REPLAN, severity 2) then meltdown (ROLLBACK, severity 5)
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "step_a"})
        ingest_risk(store, run_id, {"trigger": "meltdown", "step_id": "step_b"})

        decision = RecoveryEngine(store).assess(run_id, replay=False)
        assert decision.mode is RecoveryMode.ROLLBACK
        assert "risk meltdown triggers rollback" in decision.contract.reason
        # Losing trigger step does not pollute guidance reason
        assert "step_a" not in decision.contract.reason


def test_abort_risk_precedes_uncertain_actions(tmp_path: Path) -> None:
    db = str(tmp_path / "precedence_abort.db")
    with SQLiteStorage(db) as store:
        run_id = "run_abort"
        _make_run(store, run_id=run_id)

        ledger = ActionLedger(store, run_id)
        ledger.claim("payment.process", {}, key="pay_1")

        # side_effect_duplicate -> ABORT (severity 6)
        ingest_risk(store, run_id, {"trigger": "side_effect_duplicate"})

        decision = RecoveryEngine(store).assess(run_id, replay=False)
        assert decision.mode is RecoveryMode.ABORT
        assert decision.contract.recovery_status is RecoverySafety.UNSAFE
