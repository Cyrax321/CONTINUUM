from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from continuum.actions import ActionLedger, ProbeReconciler, Resolution, reconcile_pending
from continuum.checkpoint import CheckpointManager
from continuum.environment import CallableProvider, StaticProvider, capture
from continuum.events import EventType
from continuum.models import (
    Origin,
    RecoveryMode,
    RecoverySafety,
    Run,
    StateStatus,
    utcnow,
)
from continuum.recovery import (
    SEVERITY,
    RecoveryEngine,
    RepairKind,
    render_contract,
    verify_contract,
)
from continuum.recovery.guidance import human_steps_for
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
    storage.append_event(
        "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
    )
    yield storage
    storage.close()


def env(dataset: str = "v3"):  # type: ignore[no-untyped-def]
    return capture("run_1", StaticProvider(dataset=dataset))


def seed(store: SQLiteStorage, *, docs: int = 20, dataset: str = "v3") -> None:
    store.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": dataset}
    )
    store.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "paper_1", "summary": "study", "source": "dataset"},
    )
    store.append_event(
        "run_1",
        EventType.FINDING_ADDED,
        {"finding_id": "finding_1", "claim": "X", "evidence": ["paper_1"]},
    )
    for i in range(docs):
        store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})
    CheckpointManager(store).checkpoint("run_1", environment=env(dataset))


# --- the clean path -------------------------------------------------------- #


def test_an_intact_run_resumes(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))

    assert decision.mode is RecoveryMode.RESUME
    assert decision.safe
    assert not decision.plan
    assert decision.contract.recovery_status is RecoverySafety.SAFE_TO_RESUME
    assert decision.contract.next_allowed_action is None
    assert decision.state.progress.completed == 20


def test_resume_permits_any_action(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    assert decision.permits("anything_at_all")


# --- environment drift ----------------------------------------------------- #


def test_a_changed_dependency_requires_repair(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))

    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_REPAIR
    assert decision.plan.of_kind(RepairKind.REVALIDATE_DEPENDENCY)
    assert "dataset" in str(decision.contract.invalidated)


def test_repairs_are_ordered_prerequisites_first(store: SQLiteStorage) -> None:
    """Re-pin the dependency before re-deriving what rests on it."""
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
    kinds = [s.kind for s in decision.plan.steps]

    assert kinds.index(RepairKind.REVALIDATE_DEPENDENCY) < kinds.index(RepairKind.REDERIVE_EVIDENCE)
    assert kinds.index(RepairKind.REDERIVE_EVIDENCE) < kinds.index(RepairKind.REDERIVE_FINDING)


def test_progress_survives_environment_drift(store: SQLiteStorage) -> None:
    """Repair must not discard verified work."""
    seed(store, docs=60)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
    assert decision.state.progress.completed == 60


# --- uncertain side effects dominate --------------------------------------- #


def test_an_uncertain_side_effect_requests_a_human(store: SQLiteStorage) -> None:
    seed(store)
    ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_HUMAN
    assert decision.uncertain_actions


def test_caution_dominates_when_signals_disagree(store: SQLiteStorage) -> None:
    """A stale dataset says REPAIR; an unknown side effect says REQUEST_HUMAN.

    The more cautious answer must win regardless of evaluation order.
    """
    seed(store)
    ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert SEVERITY[decision.mode] > SEVERITY[RecoveryMode.REPAIR_AND_RESUME]


def test_reconciling_the_effect_downgrades_the_decision(store: SQLiteStorage) -> None:
    """Once the world is known, the run drops back to ordinary repair."""
    seed(store)
    ledger = ActionLedger(store, "run_1")
    ledger.claim("github.create_issue", {"title": "Bug"})

    engine = RecoveryEngine(store)
    assert engine.assess("run_1", current_environment=env("v4")).mode is RecoveryMode.REQUEST_HUMAN

    reconcile_pending(
        ledger, ProbeReconciler(lambda a: Resolution(occurred=True, external_id="481"))
    )
    after = engine.assess("run_1", current_environment=env("v4"))
    assert after.mode is RecoveryMode.REPAIR_AND_RESUME
    assert not after.uncertain_actions


def test_reconciliation_is_the_first_repair_step(store: SQLiteStorage) -> None:
    """Nothing else is safe while the world may have been modified."""
    seed(store)
    ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
    first = decision.plan.first
    assert first is not None
    assert first.kind is RepairKind.RECONCILE_ACTION


def test_an_escalated_action_still_requests_a_human(store: SQLiteStorage) -> None:
    seed(store)
    ledger = ActionLedger(store, "run_1")
    outcome = ledger.claim("payment.charge", {"amount": 100})
    ledger.flag_for_review(outcome.key, "provider unreachable")

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.plan.requires_human


# --- other escalations ----------------------------------------------------- #


def test_an_expired_approval_needs_a_person(store: SQLiteStorage) -> None:
    store.append_event(
        "run_1", EventType.APPROVAL_REQUESTED, {"approval_id": "ap_1", "subject": "publish"}
    )
    store.append_event(
        "run_1",
        EventType.APPROVAL_GRANTED,
        {
            "approval_id": "ap_1",
            "expires_at": (utcnow() - timedelta(hours=1)).isoformat(),
        },
    )
    CheckpointManager(store).checkpoint("run_1", environment=env())

    decision = RecoveryEngine(store).assess("run_1", current_environment=env())
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.plan.of_kind(RepairKind.RENEW_APPROVAL)


def test_an_unverifiable_resource_needs_a_person(store: SQLiteStorage) -> None:
    """UNKNOWN is not repairable by machine: nobody knows what is true."""
    seed(store)

    def down() -> str:
        raise ConnectionError("dataset service unreachable")

    decision = RecoveryEngine(store).assess(
        "run_1", current_environment=capture("run_1", CallableProvider({"dataset": down}))
    )
    assert decision.mode is RecoveryMode.REQUEST_HUMAN


def test_a_model_switch_requires_revalidation(store: SQLiteStorage) -> None:
    store.append_event("run_1", EventType.MODEL_CHANGED, {"model": "model-a"})
    store.append_event(
        "run_1",
        EventType.MODEL_ASSUMPTION_RECORDED,
        {"item_id": "a1", "description": "assumes JSON tool syntax"},
    )
    CheckpointManager(store).checkpoint("run_1", environment=env())

    decision = RecoveryEngine(store).assess(
        "run_1", current_environment=env(), expected_model="model-b"
    )
    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert decision.plan.of_kind(RepairKind.REVALIDATE_MODEL_STATE)


def test_a_run_with_no_history_aborts(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="run_empty", goal="g"))
    from continuum.checkpoint import CheckpointError

    with pytest.raises(CheckpointError):
        RecoveryEngine(store).assess("run_empty")


def test_an_invalid_goal_forces_a_replan(store: SQLiteStorage) -> None:
    """Work cannot be repaired toward a goal that no longer holds."""
    from continuum.models import Component, ComponentValidationEntry, StateStatus
    from continuum.state.validator import StateValidator

    class GoalDoubting(StateValidator):
        def validate(self, state, **kw):  # type: ignore[no-untyped-def]
            outcome = super().validate(state, **kw)
            statuses = [e for e in outcome.report.statuses if e.component is not Component.GOAL] + [
                ComponentValidationEntry(
                    component=Component.GOAL,
                    status=StateStatus.CONFLICTED,
                    detail="the requester withdrew the objective",
                )
            ]
            report = outcome.report.model_copy(
                update={"statuses": statuses, "safe_to_resume": False}
            )
            return type(outcome)(
                state=outcome.state,
                report=report,
                environment_diff=outcome.environment_diff,
            )

    seed(store)
    decision = RecoveryEngine(store, validator=GoalDoubting()).assess(
        "run_1", current_environment=env("v3")
    )
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert SEVERITY[decision.mode] > SEVERITY[RecoveryMode.REPLAN]
    assert any("goal" in r for r in decision.rationale) or decision.plan.requires_human


def test_a_run_with_events_but_no_checkpoint_still_recovers(
    store: SQLiteStorage,
) -> None:
    """No checkpoint is not the same as no history."""
    for i in range(5):
        store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env())
    assert decision.mode is not RecoveryMode.ABORT
    assert decision.state.progress.completed == 5
    assert decision.restored.checkpoint is None


def test_the_environment_diff_is_exposed_on_the_decision(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
    assert decision.environment_diff.changed
    assert decision.environment_diff.for_resource("dataset") is not None


def test_no_environment_supplied_blocks_a_clean_resume(store: SQLiteStorage) -> None:
    """Never validated is not the same as validated clean."""
    seed(store)
    decision = RecoveryEngine(store).assess("run_1")
    assert decision.mode is not RecoveryMode.RESUME


# --- the contract gates behaviour ------------------------------------------ #


def test_the_contract_names_exactly_one_next_action(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))

    assert decision.contract.next_allowed_action is not None
    assert len(decision.contract.required_actions) > 1  # more work exists
    assert decision.contract.next_allowed_action == decision.contract.required_actions[0]


def test_the_contract_refuses_out_of_order_work(store: SQLiteStorage) -> None:
    seed(store)
    ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))

    assert not decision.permits("rederive_finding:finding_1")
    assert decision.permits(decision.contract.next_allowed_action or "")


def test_contracts_are_deterministic(store: SQLiteStorage) -> None:
    seed(store)
    engine = RecoveryEngine(store)
    first = engine.assess("run_1", current_environment=env("v4")).contract
    second = engine.assess("run_1", current_environment=env("v4")).contract

    assert first.integrity_hash == second.integrity_hash
    assert first.verified == second.verified
    assert first.required_actions == second.required_actions


def test_a_contract_is_sealed_and_tamper_evident(store: SQLiteStorage) -> None:
    seed(store)
    contract = RecoveryEngine(store).assess("run_1", current_environment=env("v4")).contract

    assert verify_contract(contract)
    forged = contract.model_copy(update={"next_allowed_action": "do_whatever_i_want"})
    assert not verify_contract(forged)


def test_an_unsealed_contract_does_not_verify() -> None:
    from continuum.models import RecoveryContract

    assert not verify_contract(
        RecoveryContract(run_id="r", recovery_status=RecoverySafety.SAFE_TO_RESUME)
    )


def test_contracts_render_readably(store: SQLiteStorage) -> None:
    seed(store)
    contract = RecoveryEngine(store).assess("run_1", current_environment=env("v4")).contract
    rendered = render_contract(contract)

    assert "run_id:            run_1" in rendered
    assert "recovery_status:   requires_repair" in rendered
    assert "next_allowed:" in rendered


# --- reporting -------------------------------------------------------------- #


def test_the_decision_renders_a_full_report(store: SQLiteStorage) -> None:
    seed(store, docs=60)
    ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})
    rendered = RecoveryEngine(store).assess("run_1", current_environment=env("v4")).render()

    assert "CONTINUUM RECOVERY" in rendered
    assert "Run: run_1" in rendered
    assert "State validation:" in rendered
    assert "[!!] external dependency dataset" in rendered
    assert "Action ledger:" in rendered
    assert "Recovery decision: REQUEST_HUMAN" in rendered
    assert "Repairs required:" in rendered
    assert "Next permitted action:" in rendered


def test_a_clean_report_says_the_ledger_is_clear(store: SQLiteStorage) -> None:
    seed(store)
    rendered = RecoveryEngine(store).assess("run_1", current_environment=env("v3")).render()
    assert "no uncertain side effects" in rendered
    assert "Recovery decision: RESUME" in rendered


def test_the_decision_explains_itself(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
    assert decision.rationale
    assert any("repair" in r for r in decision.rationale)


def test_the_repair_count_excludes_verified_components(store: SQLiteStorage) -> None:
    """ "N component(s) need repair" must count only the damaged ones.

    The seeded run reports both VALID components (goal, progress) and
    downgraded ones (dataset, evidence, finding). Counting every status would
    overstate the damage to whoever reads the rationale.
    """
    from continuum.models import StateStatus

    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))

    statuses = decision.validation.report.statuses
    downgraded = [e for e in statuses if e.status is not StateStatus.VALID]
    verified = [e for e in statuses if e.status is StateStatus.VALID]

    assert verified, "this fixture must contain VALID components for the test to mean anything"
    assert len(downgraded) < len(statuses)

    rationale = next(r for r in decision.rationale if "need repair" in r)
    assert rationale.startswith(f"{len(downgraded)} component(s)")
    assert not rationale.startswith(f"{len(statuses)} component(s)")


def test_assessment_changes_nothing(store: SQLiteStorage) -> None:
    """Recovery decisions must be safe to compute against a live database."""
    seed(store)
    before = store.last_sequence("run_1")
    versions = list(store.list_versions("run_1"))

    RecoveryEngine(store).assess("run_1", current_environment=env("v4"))

    assert store.last_sequence("run_1") == before
    assert list(store.list_versions("run_1")) == versions


def test_lenient_mode_still_never_resumes_over_an_uncertain_side_effect(
    store: SQLiteStorage,
) -> None:
    """Tolerating uncertainty downgrades REQUEST_HUMAN to WAIT — not to RESUME.

    Opting out of strictness may change who resolves the doubt; it must never
    make an unresolved side effect look settled.
    """
    seed(store)
    ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})

    decision = RecoveryEngine(store, strict_unknown=False).assess(
        "run_1", current_environment=env("v3")
    )

    assert decision.mode is RecoveryMode.WAIT
    assert not decision.safe
    assert SEVERITY[decision.mode] > SEVERITY[RecoveryMode.REPAIR_AND_RESUME]
    assert not decision.permits("do_anything_else")
    assert decision.next_allowed_action is not None
    assert decision.next_allowed_action.startswith("reconcile_action:")


def test_strict_unknown_can_be_relaxed(store: SQLiteStorage) -> None:
    seed(store)

    def down() -> str:
        raise ConnectionError("unreachable")

    lenient = RecoveryEngine(store, strict_unknown=False)
    decision = lenient.assess(
        "run_1", current_environment=capture("run_1", CallableProvider({"dataset": down}))
    )
    assert SEVERITY[decision.mode] < SEVERITY[RecoveryMode.REQUEST_HUMAN]


# --- durability ------------------------------------------------------------ #


def test_a_decision_survives_a_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 100})
        seed(store, docs=40)
        ActionLedger(store, "run_1").claim("github.create_issue", {"title": "Bug"})

    with SQLiteStorage(db) as store:
        decision = RecoveryEngine(store).assess("run_1", current_environment=env("v4"))
        assert decision.mode is RecoveryMode.REQUEST_HUMAN
        assert decision.state.progress.completed == 40
        assert verify_contract(decision.contract)


def test_self_certified_runs_are_confirmable(store: SQLiteStorage) -> None:
    """Issue #35: an MCP/agent-reported run must be resumable after a human
    confirms its self-reported goal and progress."""
    store.create_run(Run(run_id="r1", goal="do X"))
    store.append_event("r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT)
    store.append_event(
        "r1", EventType.TASK_UPDATED, {"completed": 1, "failed": 0}, source=Origin.EXTERNAL_AGENT
    )

    blocked = RecoveryEngine(store).assess("r1")
    assert blocked.mode is RecoveryMode.REQUEST_HUMAN
    assert blocked.next_allowed_action == "human_review:goal"

    store.append_event(
        "r1",
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )

    confirmed = RecoveryEngine(store).assess("r1")
    assert confirmed.mode is RecoveryMode.RESUME
    assert confirmed.safe
    assert all(
        e.status is not StateStatus.REQUIRES_REVIEW for e in confirmed.validation.report.statuses
    )


# --- unprojectable logs (issue #383) ---------------------------------------- #


def test_an_unprojectable_log_requests_a_human_and_names_the_break(store: SQLiteStorage) -> None:
    """A poisoned log must produce a verdict, not a pydantic traceback.

    The action tools fold only ACTION_* events, so a run whose projection is
    dead can still authorise real side effects. Recovery has to be able to say
    what is known, where the log stops folding, and that continuing is not a
    decision software may take.
    """
    seed(store)
    store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 999, "failed": 0})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))

    assert decision.mode is not RecoveryMode.RESUME
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert not decision.safe
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_HUMAN
    assert "stops folding at sequence" in " ".join(decision.rationale)
    # The verdict covers the last-good prefix only, and says so.
    state = decision.state
    assert state.status is StateStatus.INVALID
    assert state.unprojectable_at_sequence == 26
    assert state.source_sequence == 24


def test_the_contract_names_the_break_as_a_required_action(store: SQLiteStorage) -> None:
    """The structured artifact must carry the break, not just the prose.

    With an empty plan the contract read required_actions=[] and fell through
    to "continue" over a requires_human verdict (#385 review): prose and
    structure disagreeing, with a machine reader most likely to act on the
    structure.
    """
    seed(store)
    store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 999, "failed": 0})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))

    steps = decision.plan.of_kind(RepairKind.REPAIR_LOG)
    assert len(steps) == 1
    assert steps[0].target == "sequence_26"
    assert steps[0].requires_human
    assert decision.next_allowed_action == "repair_log:sequence_26"
    assert decision.plan.steps[0] is steps[0], "the unreadable log sorts first"
    assert decision.contract.required_actions[0] == "repair_log:sequence_26"
    assert decision.contract.next_allowed_action == "repair_log:sequence_26"


def test_the_degraded_contract_does_not_claim_unqualified_verification(
    store: SQLiteStorage,
) -> None:
    """verified entries were checked against the last-good prefix only."""
    seed(store)
    store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 999, "failed": 0})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    contract = decision.contract

    assert all(e.startswith("goal (through sequence 24)") for e in contract.verified if e == "goal")
    assert any("projection (invalid" in e for e in contract.invalidated)
    assert any("projection stopped at sequence 26" in e for e in contract.evidence)
    assert verify_contract(contract)


def test_rendered_output_never_offers_continue_on_a_broken_log(store: SQLiteStorage) -> None:
    """'continue' over requires_human reads as permission the gate never gave."""
    seed(store)
    store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 999, "failed": 0})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    rendered = render_contract(decision.contract)

    assert "continue" not in rendered
    assert "next_allowed:      repair_log:sequence_26" in rendered
    assert "Next permitted action: continue" not in decision.render()
    assert "Next permitted action: repair_log:sequence_26" in decision.render()


def test_guidance_names_the_break_instead_of_the_generic_fallback(store: SQLiteStorage) -> None:
    seed(store)
    store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 999, "failed": 0})

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    steps = human_steps_for(decision, run_id="run_1")

    assert any("stops folding at sequence_26" in s for s in steps)
    assert not any("nothing further is automatable" in s for s in steps)


def test_a_healthy_log_still_resumes_with_degrade_wired_in(store: SQLiteStorage) -> None:
    """The engine now folds with degrade enabled everywhere; a sound log must
    be untouched by that."""
    seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))

    assert decision.mode is RecoveryMode.RESUME
    assert decision.safe
    assert decision.state.status is StateStatus.VALID
    assert decision.state.unprojectable_at_sequence is None
    # Healthy contracts keep their exact shape: bare verified names, no
    # projection entry, and "continue" remains correct under SAFE_TO_RESUME.
    assert decision.contract.verified == ["external_dependency:dataset", "goal", "progress"]
    assert decision.contract.invalidated == []
    assert "next_allowed:      continue" in render_contract(decision.contract)
