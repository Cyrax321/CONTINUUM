from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from continuum.models import (
    Action,
    ActionStatus,
    Approval,
    ApprovalStatus,
    Decision,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    Origin,
    PendingWork,
    Progress,
    SemanticState,
    StateCheckpoint,
    StateStatus,
)
from continuum.security.hashing import stable_hash


def make_state(**overrides: object) -> SemanticState:
    base: dict[str, object] = {
        "run_id": "run_4821",
        "goal": Goal(description="Analyze 10,000 documents for evidence supporting X", version=3),
        "progress": Progress(total=10_000, completed=3421, pending=6576, failed=3),
    }
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


# --- structural invariants ------------------------------------------------- #


def test_state_is_frozen() -> None:
    state = make_state()
    with pytest.raises(ValidationError):
        state.version = 5  # type: ignore[misc]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Goal(description="x", surprise="field")  # type: ignore[call-arg]


def test_next_version_increments_and_does_not_mutate_original() -> None:
    state = make_state()
    updated = state.next_version(
        progress=Progress(total=10_000, completed=3422, pending=6575, failed=3)
    )
    assert state.version == 0
    assert updated.version == 1
    assert state.progress.completed == 3421
    assert updated.progress.completed == 3422
    assert updated.updated_at >= state.updated_at


def test_progress_counters_cannot_exceed_total() -> None:
    with pytest.raises(ValidationError):
        Progress(total=10, completed=8, pending=5, failed=0)


def test_progress_counters_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        Progress(completed=-1)


def test_progress_without_total_is_unbounded() -> None:
    assert Progress(completed=99, pending=1).total is None


def test_confidence_must_be_within_unit_interval() -> None:
    Finding(claim="ok", confidence=0.0)
    Finding(claim="ok", confidence=1.0)
    for bad in (-0.01, 1.01):
        with pytest.raises(ValidationError):
            Finding(claim="bad", confidence=bad)


def test_goal_version_starts_at_one() -> None:
    assert Goal(description="x").version == 1
    with pytest.raises(ValidationError):
        Goal(description="x", version=0)


def test_state_version_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        make_state(version=-1)


# --- defaults reflect the safety posture ----------------------------------- #


def test_actions_start_planned_and_side_effect_is_certain_by_default() -> None:
    action = Action(run_id="run_1", action_type="github.create_issue")
    assert action.status is ActionStatus.PLANNED
    assert action.side_effect_uncertain is False
    assert action.external_id is None


def test_approvals_start_pending() -> None:
    assert Approval(subject="publish results").status is ApprovalStatus.PENDING


def test_state_components_default_to_valid() -> None:
    assert Decision(decision="Only peer-reviewed studies").status is StateStatus.VALID
    assert Evidence(summary="paper_128").status is StateStatus.VALID
    assert PendingWork(description="Search 2019-2022 literature").status is StateStatus.VALID
    assert ExternalDependency(resource="dataset", version="v3").status is StateStatus.VALID


def test_generated_ids_are_prefixed_by_kind() -> None:
    assert Decision(decision="d").decision_id.startswith("decision_")
    assert Finding(claim="c").finding_id.startswith("finding_")
    assert Evidence().evidence_id.startswith("evidence_")
    assert PendingWork(description="p").task_id.startswith("task_")
    assert Action(run_id="r", action_type="t").action_id.startswith("action_")


# --- serialization + determinism ------------------------------------------- #


def test_checkpoint_round_trips_through_json() -> None:
    checkpoint = StateCheckpoint(
        run_id="run_4821",
        version=17,
        trigger="milestone",
        state=make_state(
            decisions=[
                Decision(
                    decision_id="decision_12",
                    decision="Only include peer-reviewed studies",
                    reason="User requirement",
                    evidence=["user_instruction_001"],
                )
            ],
            findings=[
                Finding(
                    finding_id="finding_17", claim="...", evidence=["paper_128"], confidence=0.91
                )
            ],
            pending_work=[PendingWork(task_id="task_1", description="Search 2019-2022 literature")],
            external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
        ),
    )
    restored = StateCheckpoint.model_validate_json(checkpoint.model_dump_json())
    assert restored == checkpoint
    assert stable_hash(restored) == stable_hash(checkpoint)


def test_semantically_identical_states_hash_identically() -> None:
    a = make_state()
    b = make_state(created_at=a.created_at, updated_at=a.updated_at)
    assert stable_hash(a) == stable_hash(b)


def test_changing_any_field_changes_the_hash() -> None:
    a = make_state()
    b = a.model_copy(
        update={"progress": Progress(total=10_000, completed=3422, pending=6575, failed=3)}
    )
    assert stable_hash(a) != stable_hash(b)


def test_model_specific_state_is_carried_explicitly() -> None:
    state = make_state(
        model=ModelState(
            model="model-a",
            provider="local",
            model_specific_state=[ModelSpecificState(description="Relies on model-a tool syntax")],
        )
    )
    assert state.model is not None
    assert state.model.model_specific_state[0].required_validation


# --- property-based -------------------------------------------------------- #


@given(
    completed=st.integers(min_value=0, max_value=10_000),
    pending=st.integers(min_value=0, max_value=10_000),
    failed=st.integers(min_value=0, max_value=10_000),
)
def test_progress_accepts_any_non_negative_counts_without_total(
    completed: int, pending: int, failed: int
) -> None:
    progress = Progress(completed=completed, pending=pending, failed=failed)
    assert progress.completed + progress.pending + progress.failed >= 0


@given(steps=st.integers(min_value=1, max_value=50))
def test_versions_increase_monotonically(steps: int) -> None:
    state = make_state()
    versions = []
    for _ in range(steps):
        state = state.next_version()
        versions.append(state.version)
    assert versions == sorted(versions)
    assert versions[-1] == steps


# --- provenance ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "origin,expected",
    [
        (Origin.DETERMINISTIC, False),
        (Origin.HUMAN, False),
        (Origin.LLM, True),
        (Origin.EXTERNAL_AGENT, True),
        (Origin.IMPORTED, True),
    ],
)
def test_self_certified_truth_table(origin, expected) -> None:
    assert origin.self_certified is expected
    