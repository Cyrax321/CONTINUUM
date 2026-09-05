"""Phase 1 tests: explainable RecoveryContract + provenance unification.

These cover the directive's required proofs:

1. existing serialized contracts still deserialize
2. new contracts carry evidence when evidence exists
3. new contracts carry reason when a rationale exists
4. missing evidence/reason does not break existing callers
5. recovery behaviour is unchanged
6. a real checkpoint -> validation -> recovery -> contract scenario carries
   the real WHY and WHAT-evidence
7. the canonical provenance mapping preserves all three source axes
8. the self-certification guarantee (agent claims != trusted state) holds
"""

from __future__ import annotations

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.environment import EnvironmentDiff, StaticProvider, capture
from continuum.events import EventType
from continuum.models import (
    Component,
    ComponentValidationEntry,
    ExternalDependency,
    Goal,
    Origin,
    Progress,
    Provenance,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    Run,
    SemanticState,
    StateStatus,
    StateValidationResult,
)
from continuum.provenance_map import (
    CanonicalProvenance,
    canonical_origin,
    canonical_state_status,
    canonical_trust,
    summarize,
)
from continuum.recovery import (
    RecoveryEngine,
    build_contract,
    render_contract,
    seal_contract,
    verify_contract,
)
from continuum.recovery.planner import RepairKind, RepairPlan, RepairStep
from continuum.security.hashing import stable_hash
from continuum.security.provenance import TrustLevel
from continuum.state.validator import ValidationOutcome, validate_state
from continuum.storage import SQLiteStorage

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _state() -> SemanticState:
    return SemanticState(run_id="r", goal=Goal(description="g"))


def _outcome_with_dependency_change() -> ValidationOutcome:
    """A validation outcome where the ``dataset`` dependency changed v3 -> v4."""
    state = SemanticState(
        run_id="r",
        goal=Goal(description="g"),
        external_dependencies=[
            ExternalDependency(resource="dataset", status=StateStatus.CONFLICTED)
        ],
    )
    report = StateValidationResult(
        run_id="r",
        checkpoint_version=1,
        statuses=[
            ComponentValidationEntry(
                component=Component.EXTERNAL_DEPENDENCY,
                component_id="dataset",
                status=StateStatus.CONFLICTED,
                detail="v3 -> v4",
            ),
            ComponentValidationEntry(
                component=Component.GOAL, status=StateStatus.VALID, detail="v1"
            ),
        ],
        safe_to_resume=False,
        reason="external_dependency dataset is conflicted",
    )
    return ValidationOutcome(state=state, report=report, environment_diff=EnvironmentDiff())


def _repair_plan() -> RepairPlan:
    return RepairPlan(
        steps=[RepairStep(kind=RepairKind.REVALIDATE_DEPENDENCY, target="dataset", reason="drift")]
    )


# --------------------------------------------------------------------------- #
# 1. Existing serialized contracts still deserialize (backward compatible)
# --------------------------------------------------------------------------- #


def test_existing_serialized_contract_still_deserializes() -> None:
    payload = {
        "run_id": "r",
        "checkpoint_version": 0,
        "recovery_status": "safe_to_resume",
        "verified": ["goal"],
        "invalidated": [],
        "required_actions": [],
        "next_allowed_action": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    contract = RecoveryContract.model_validate(payload)
    # The additive fields default rather than raising.
    assert contract.evidence == []
    assert contract.reason == ""
    assert contract.run_id == "r"
    assert contract.recovery_status is RecoverySafety.SAFE_TO_RESUME


def test_legacy_sealed_contract_verifies() -> None:
    """A contract sealed before evidence/reason existed must still verify."""
    contract = RecoveryContract(
        run_id="r",
        checkpoint_version=2,
        recovery_status=RecoverySafety.REQUIRES_REPAIR,
        verified=["goal"],
        invalidated=["external_dependency dataset (CONFLICTED)"],
        required_actions=["revalidate_dependency:dataset"],
        next_allowed_action="revalidate_dependency:dataset",
    )
    legacy_hash = stable_hash(
        contract.model_dump(
            mode="json", exclude={"integrity_hash", "created_at", "evidence", "reason"}
        )
    )
    legacy = contract.model_copy(update={"integrity_hash": legacy_hash})
    assert verify_contract(legacy)


# --------------------------------------------------------------------------- #
# 2. New contracts contain evidence when evidence exists
# --------------------------------------------------------------------------- #


def test_new_contract_contains_evidence_when_evidence_exists() -> None:
    outcome = _outcome_with_dependency_change()
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=outcome,
        plan=_repair_plan(),
    )
    assert contract.evidence
    assert any("dataset" in e and "v3 -> v4" in e for e in contract.evidence)


def test_new_contract_contains_reason_when_rationale_exists() -> None:
    outcome = _outcome_with_dependency_change()
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=outcome,
        plan=_repair_plan(),
        reason="dataset changed; repair before resume",
    )
    assert contract.reason == "dataset changed; repair before resume"


def test_new_sealed_contract_verifies_via_current_hash() -> None:
    outcome = _outcome_with_dependency_change()
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=outcome,
        plan=_repair_plan(),
    )
    assert verify_contract(contract)


def test_round_trips_evidence_and_reason() -> None:
    outcome = _outcome_with_dependency_change()
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=outcome,
        plan=_repair_plan(),
        reason="dataset changed",
    )
    restored = RecoveryContract.model_validate(contract.model_dump(mode="json"))
    assert restored.evidence == contract.evidence
    assert restored.reason == contract.reason


# --------------------------------------------------------------------------- #
# 4. Missing evidence/reason does not break existing callers
# --------------------------------------------------------------------------- #


def test_missing_evidence_and_reason_do_not_break_callers() -> None:
    outcome = _outcome_with_dependency_change()

    # build_contract with no explicit reason/evidence falls back to the
    # validator's reason and derived validation evidence.
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=outcome,
        plan=_repair_plan(),
    )
    assert contract.reason == "external_dependency dataset is conflicted"
    assert contract.evidence  # derived from the validation report

    # A caller constructing RecoveryContract directly with neither field still
    # produces a valid, verifiable contract.
    direct = seal_contract(
        RecoveryContract(run_id="r", recovery_status=RecoverySafety.SAFE_TO_RESUME)
    )
    assert direct.evidence == []
    assert direct.reason == ""
    assert verify_contract(direct)


def test_render_contract_includes_reason_and_evidence() -> None:
    outcome = _outcome_with_dependency_change()
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=outcome,
        plan=_repair_plan(),
        reason="dataset changed",
    )
    rendered = render_contract(contract)
    assert "reason:" in rendered
    assert "evidence:" in rendered


# --------------------------------------------------------------------------- #
# 5. Recovery behaviour is unchanged
# --------------------------------------------------------------------------- #


@pytest.fixture
def store() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="Analyze documents"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "Analyze documents", "total": 20})
    yield storage
    storage.close()


def _env(dataset: str = "v3"):
    return capture("run_1", StaticProvider(dataset=dataset))


def _seed(store: SQLiteStorage, *, docs: int = 20, dataset: str = "v3") -> None:
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
    CheckpointManager(store).checkpoint("run_1", environment=_env(dataset))


def test_recovery_decision_unchanged_with_explainable_contract(store: SQLiteStorage) -> None:
    _seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=_env("v4"))

    # Behaviour is identical to before Phase 1.
    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_REPAIR
    # Exactly-one-next-action invariant preserved.
    assert decision.contract.next_allowed_action is not None
    assert decision.contract.next_allowed_action == decision.contract.required_actions[0]


# --------------------------------------------------------------------------- #
# 6. Integration: checkpoint -> validation -> recovery -> explainable contract
# --------------------------------------------------------------------------- #


def test_recovery_scenario_produces_explainable_contract(store: SQLiteStorage) -> None:
    _seed(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=_env("v4"))
    contract = decision.contract

    # The contract now answers WHY and WHAT-evidence using *real* validation
    # output, not invented strings.
    assert contract.reason
    assert any("repair" in token for token in contract.reason.split())
    assert contract.evidence
    # Real evidence: the validator observed the dataset drift v3 -> v4.
    assert any("dataset" in e for e in contract.evidence)
    assert any("v3" in e and "v4" in e for e in contract.evidence)


# --------------------------------------------------------------------------- #
# 7. Canonical provenance mapping preserves all three axes
# --------------------------------------------------------------------------- #


def test_origin_maps_to_canonical_who() -> None:
    assert canonical_origin(Origin.DETERMINISTIC) is CanonicalProvenance.OBSERVED
    assert canonical_origin(Origin.HUMAN) is CanonicalProvenance.VERIFIED
    assert canonical_origin(Origin.LLM) is CanonicalProvenance.AGENT_ASSERTED
    assert canonical_origin(Origin.EXTERNAL_AGENT) is CanonicalProvenance.AGENT_ASSERTED
    assert canonical_origin(Origin.IMPORTED) is CanonicalProvenance.INFERRED


def test_trust_maps_to_canonical_how() -> None:
    assert canonical_trust("verified") is CanonicalProvenance.VERIFIED
    assert canonical_trust("unverified") is CanonicalProvenance.INFERRED
    assert canonical_trust("contested") is CanonicalProvenance.CONTRADICTED


def test_state_status_maps_to_canonical_what() -> None:
    assert canonical_state_status(StateStatus.VALID) is CanonicalProvenance.VERIFIED
    assert canonical_state_status(StateStatus.STALE) is CanonicalProvenance.STALE
    assert canonical_state_status(StateStatus.CONFLICTED) is CanonicalProvenance.CONTRADICTED
    assert canonical_state_status(StateStatus.UNKNOWN) is CanonicalProvenance.UNKNOWN
    assert canonical_state_status(StateStatus.INVALID) is CanonicalProvenance.CONTRADICTED
    assert (
        canonical_state_status(StateStatus.REQUIRES_REVIEW) is CanonicalProvenance.REQUIRES_REVIEW
    )
    # EXPIRED has no dedicated canonical member; it normalizes to STALE while the
    # original StateStatus.EXPIRED is preserved in the source enum.
    assert canonical_state_status(StateStatus.EXPIRED) is CanonicalProvenance.STALE


def test_provenance_view_preserves_all_three_axes() -> None:
    view = summarize(Origin.LLM, StateStatus.STALE, trust="unverified")
    # Source axes preserved on the view (no information collapse).
    assert view.origin is Origin.LLM
    assert view.state_status is StateStatus.STALE
    assert view.trust == "unverified"
    # Canonical projections are independent.
    assert view.who is CanonicalProvenance.AGENT_ASSERTED
    assert view.how_trusted is CanonicalProvenance.INFERRED
    assert view.what_state is CanonicalProvenance.STALE
    # Validity wins for the single primary label.
    assert view.primary is CanonicalProvenance.STALE


def test_provenance_view_primary_prefers_trust_when_valid() -> None:
    with_trust = summarize(Origin.LLM, StateStatus.VALID, trust="verified")
    assert with_trust.primary is CanonicalProvenance.VERIFIED
    # No trust info -> fall back to who.
    no_trust = summarize(Origin.HUMAN, StateStatus.VALID)
    assert no_trust.primary is CanonicalProvenance.VERIFIED


def test_source_vocabularies_remain_intact() -> None:
    # Phase 1 must not delete or alter the three source vocabularies.
    assert {o.value for o in Origin} == {
        "deterministic",
        "human",
        "llm",
        "external_agent",
        "external_monitor",
        "imported",
    }
    assert {s.value for s in StateStatus} == {
        "valid",
        "stale",
        "conflicted",
        "unknown",
        "invalid",
        "requires_review",
        "expired",
    }
    assert set(TrustLevel.__args__) == {"verified", "unverified", "contested"}


# --------------------------------------------------------------------------- #
# 8. Self-certification guarantee: agent claims != trusted state
# --------------------------------------------------------------------------- #


def test_self_certified_progress_stays_requires_review_until_confirmed() -> None:
    state = SemanticState(
        run_id="r",
        goal=Goal(description="g", provenance=Provenance(origin=Origin.LLM)),
        progress=Progress(completed=1, provenance=Provenance(origin=Origin.LLM)),
        source_sequence=1,
    )

    before = validate_state(state)
    progress_before = next(e for e in before.report.statuses if e.component is Component.PROGRESS)
    assert progress_before.status is StateStatus.REQUIRES_REVIEW

    after = validate_state(state, confirmed=True)
    progress_after = next(e for e in after.report.statuses if e.component is Component.PROGRESS)
    assert progress_after.status is StateStatus.VALID


def test_agent_claim_requires_human_then_confirms_to_resume(
    store: SQLiteStorage,
) -> None:
    """Agent reports completion -> stays review-required -> human confirmation
    -> trusted and resumable."""
    store.create_run(Run(run_id="r1", goal="do X"))
    store.append_event("r1", EventType.RUN_STARTED, {"goal": "do X"}, source=Origin.EXTERNAL_AGENT)
    store.append_event(
        "r1", EventType.TASK_UPDATED, {"completed": 1, "failed": 0}, source=Origin.EXTERNAL_AGENT
    )

    blocked = RecoveryEngine(store).assess("r1")
    assert blocked.mode is RecoveryMode.REQUEST_HUMAN
    progress = next(
        e for e in blocked.validation.report.statuses if e.component is Component.PROGRESS
    )
    assert progress.status is StateStatus.REQUIRES_REVIEW

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
