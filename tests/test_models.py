from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from continuum.models import (
    Action,
    ActionRecordPayload,
    ActionStatus,
    Approval,
    ApprovalStatus,
    AttemptLesson,
    AuthorityConsumed,
    AuthorityReconciled,
    ConstraintPin,
    ConstraintPinDropped,
    ConstraintPinned,
    ConstraintRetracted,
    ConsumedInputs,
    Decision,
    DecisionPayload,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    ObservedRisk,
    Origin,
    PendingWork,
    Progress,
    Provenance,
    RiskObservedPayload,
    Run,
    SemanticState,
    StateCheckpoint,
    StateStatus,
    TrajectoryReport,
    UnknownSideEffect,
    _risk_detail,
    _risk_optional_id,
    _risk_score,
    _risk_trigger,
    _risk_ts,
    _validated_constraint_id,
    validate_caused_by,
    validate_correlation_id,
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


def test_action_origin_digest_must_be_sha256_hex() -> None:
    """The origin_digest validator used to be accidentally nested inside
    validate_caused_by (after its return), so it never registered on Action
    and any string was accepted despite the documented 64-hex invariant.
    """
    good = "a" * 64
    assert Action(run_id="run_1", action_type="t", origin_digest=good).origin_digest == good
    assert Action(run_id="run_1", action_type="t").origin_digest is None

    with pytest.raises(ValidationError, match="64 lowercase hex"):
        Action(run_id="run_1", action_type="t", origin_digest="NOT-HEX-AT-ALL")
    with pytest.raises(ValidationError, match="64 lowercase hex"):
        Action(run_id="run_1", action_type="t", origin_digest="A" * 64)  # uppercase


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
def test_self_certified_truth_table(origin: Origin, expected: bool) -> None:
    assert origin.self_certified is expected


# --- bounded-collection validators and truncation helpers ------------------- #


def test_consumed_inputs_bounded_collection_validators() -> None:
    # 1. event_positions boundary (128 passes) and rejection arms
    assert len(ConsumedInputs(checkpoint_seq=0, event_positions=[0] * 128).event_positions) == 128

    with pytest.raises(ValidationError, match="event_positions must contain at most 128 entries"):
        ConsumedInputs(checkpoint_seq=0, event_positions=[0] * 129)

    with pytest.raises(ValueError, match="event_positions must contain at most 128 entries"):
        ConsumedInputs._positions_valid([0] * 129)

    with pytest.raises(ValueError, match="event_positions must be integers"):
        ConsumedInputs._positions_valid(["not_an_int"])  # type: ignore[list-item]

    with pytest.raises(ValueError, match="event_positions must be integers"):
        ConsumedInputs._positions_valid([1.5])  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="event_positions must be >= 0"):
        ConsumedInputs(checkpoint_seq=0, event_positions=[-1])

    with pytest.raises(ValueError, match="event_positions must be >= 0"):
        ConsumedInputs._positions_valid([-1])

    # 2. component_ids boundary (32 entries of 128 chars pass) and rejection arms
    assert len(ConsumedInputs(checkpoint_seq=0, component_ids=["c" * 128] * 32).component_ids) == 32

    with pytest.raises(ValidationError, match="component_ids must contain at most 32 entries"):
        ConsumedInputs(checkpoint_seq=0, component_ids=["c"] * 33)

    with pytest.raises(ValueError, match="component_ids must contain at most 32 entries"):
        ConsumedInputs._component_ids_valid(["c"] * 33)

    with pytest.raises(ValueError, match="component_ids must be strings"):
        ConsumedInputs._component_ids_valid([123])  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="component_id must be 1-128 characters"):
        ConsumedInputs(checkpoint_seq=0, component_ids=[""])

    with pytest.raises(ValueError, match="component_id must be 1-128 characters"):
        ConsumedInputs._component_ids_valid([""])

    with pytest.raises(ValueError, match="component_id must be 1-128 characters"):
        ConsumedInputs._component_ids_valid(["c" * 129])

    # 3. action_ids boundary (32 entries of 128 chars pass) and rejection arms
    assert len(ConsumedInputs(checkpoint_seq=0, action_ids=["a" * 128] * 32).action_ids) == 32

    with pytest.raises(ValidationError, match="action_ids must contain at most 32 entries"):
        ConsumedInputs(checkpoint_seq=0, action_ids=["a"] * 33)

    with pytest.raises(ValueError, match="action_ids must contain at most 32 entries"):
        ConsumedInputs._action_ids_valid(["a"] * 33)

    with pytest.raises(ValueError, match="action_ids must be strings"):
        ConsumedInputs._action_ids_valid([123])  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="action_id must be 1-128 characters"):
        ConsumedInputs(checkpoint_seq=0, action_ids=[""])

    with pytest.raises(ValueError, match="action_id must be 1-128 characters"):
        ConsumedInputs._action_ids_valid([""])

    with pytest.raises(ValueError, match="action_id must be 1-128 characters"):
        ConsumedInputs._action_ids_valid(["a" * 129])


def test_caused_by_model_validators() -> None:
    # 1. DecisionPayload caused_by boundary (32 entries of 128 chars pass) and rejection arms
    assert len(DecisionPayload(decision="d", caused_by=["c" * 128] * 32).caused_by) == 32

    with pytest.raises(ValidationError, match="caused_by must contain at most 32 ids"):
        DecisionPayload(decision="d", caused_by=["c"] * 33)

    with pytest.raises(ValueError, match="caused_by must contain at most 32 ids"):
        DecisionPayload._validate_caused_by(["c"] * 33)

    with pytest.raises(ValueError, match="caused_by entries must be strings"):
        DecisionPayload._validate_caused_by([123])  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="caused_by entries must be 1-128 chars"):
        DecisionPayload(decision="d", caused_by=[""])

    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        DecisionPayload._validate_caused_by([""])

    with pytest.raises(ValidationError, match="caused_by entries must be 1-128 chars"):
        DecisionPayload(decision="d", caused_by=["c" * 129])

    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        DecisionPayload._validate_caused_by(["c" * 129])

    # 2. ActionRecordPayload caused_by boundary (32 entries of 128 chars pass) and rejection arms
    assert len(ActionRecordPayload(action_type="t", caused_by=["c" * 128] * 32).caused_by) == 32

    with pytest.raises(ValidationError, match="caused_by must contain at most 32 ids"):
        ActionRecordPayload(action_type="t", caused_by=["c"] * 33)

    with pytest.raises(ValueError, match="caused_by must contain at most 32 ids"):
        ActionRecordPayload._validate_caused_by(["c"] * 33)

    with pytest.raises(ValueError, match="caused_by entries must be strings"):
        ActionRecordPayload._validate_caused_by([123])  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="caused_by entries must be 1-128 chars"):
        ActionRecordPayload(action_type="t", caused_by=[""])

    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        ActionRecordPayload._validate_caused_by([""])

    with pytest.raises(ValidationError, match="caused_by entries must be 1-128 chars"):
        ActionRecordPayload(action_type="t", caused_by=["c" * 129])

    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        ActionRecordPayload._validate_caused_by(["c" * 129])


def test_validate_caused_by_helper() -> None:
    # 1. Empty or None returns []
    assert validate_caused_by(None) == []
    assert validate_caused_by([]) == []

    # 2. Boundary of 32 entries passes
    assert len(validate_caused_by(["c" * 128] * 32)) == 32

    # 3. Over 32 entries raises ValueError
    with pytest.raises(ValueError, match="caused_by must contain at most 32 ids"):
        validate_caused_by(["c"] * 33)

    # 4. Non-string entry raises ValueError
    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        validate_caused_by([123])  # type: ignore[list-item]

    # 5. Empty or over-length strings raise ValueError
    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        validate_caused_by([""])

    with pytest.raises(ValueError, match="caused_by entries must be 1-128 chars"):
        validate_caused_by(["c" * 129])

    # 6. known_ids verification
    with pytest.raises(ValueError, match="unknown caused_by id 'c_unknown'"):
        validate_caused_by(["c1", "c_unknown"], known_ids={"c1"})

    assert validate_caused_by(["c1", "c2"], known_ids={"c1", "c2", "c3"}) == ["c1", "c2"]


def test_risk_detail_clamp_and_risk_ts() -> None:
    # 1. _risk_detail mapping depth cap (depth >= 4 returns {})
    nested_map = {"l1": {"l2": {"l3": {"l4": {"l5": "deep"}}}}}
    assert _risk_detail(nested_map) == {"l1": {"l2": {"l3": {"l4": {}}}}}

    # 2. _risk_detail list depth cap and key slicing
    nested_list = {"l1": {"l2": {"l3": {"l4": [1, 2, 3]}}}}
    assert _risk_detail(nested_list) == {"l1": {"l2": {"l3": {"l4": []}}}}

    long_list = list(range(40))
    assert _risk_detail({"items": long_list}) == {"items": list(range(32))}
    assert _risk_detail({"items": (1, 2)}) == {"items": [1, 2]}

    # 3. _risk_detail mapping key count slicing (max 32 keys)
    large_map = {f"k_{i}": i for i in range(40)}
    clamped_map = _risk_detail(large_map)
    assert len(clamped_map) == 32

    # 4. _risk_detail key length capping (max 128 chars) and string length capping (max 512 chars)
    res_key = _risk_detail({"k" * 200: "val"})
    assert res_key == {"k" * 128: "val"}

    long_str = "x" * 600
    res_str = _risk_detail({"msg": long_str})
    assert len(res_str["msg"]) == 512
    assert res_str["msg"] == "x" * 512

    # 5. _risk_detail arbitrary unknown node fallback
    custom_obj = object()
    res_obj = _risk_detail({"obj": custom_obj})
    assert res_obj["obj"] == str(custom_obj)[:512]
    custom_set = {1, 2, 3}
    res_set = _risk_detail({"set": custom_set})
    assert res_set["set"] == str(custom_set)[:512]

    # 6. _risk_detail scalar values
    res_scalars = _risk_detail({"none": None, "int": 42, "float": 3.14, "bool": True})
    assert res_scalars == {"none": None, "int": 42, "float": 3.14, "bool": True}

    # 7. _risk_detail non-mapping wrapping and edge cases
    assert _risk_detail(None) == {}
    assert _risk_detail("") == {}
    assert _risk_detail("simple error message") == {"message": "simple error message"}
    assert _risk_detail(404) == {"message": "404"}
    assert _risk_detail({"status": "failed"}) == {"status": "failed"}

    # 8. _risk_ts parsing and None fallback arms
    now_dt = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert _risk_ts(now_dt) == now_dt
    assert _risk_ts(True) is None
    assert _risk_ts(False) is None
    assert _risk_ts(1700000000) == datetime.fromtimestamp(1700000000, tz=UTC)
    assert _risk_ts(1700000000.5) == datetime.fromtimestamp(1700000000.5, tz=UTC)
    assert _risk_ts(None) is None
    assert _risk_ts("") is None
    assert _risk_ts("   ") is None
    assert _risk_ts("1700000000") == datetime.fromtimestamp(1700000000, tz=UTC)
    assert _risk_ts("2026-01-01T12:00:00+00:00") == datetime.fromisoformat(
        "2026-01-01T12:00:00+00:00"
    )
    assert _risk_ts("not-a-timestamp") is None


def test_truncating_and_small_field_validators() -> None:
    # 1. AuthorityReconciled authority_id and reason bounds
    with pytest.raises(ValidationError, match="authority_id must be non-empty"):
        AuthorityReconciled(authority_id="   ")

    with pytest.raises(ValueError, match="authority_id must be 1-128 characters"):
        AuthorityReconciled._authority_id_valid("a" * 129)

    probe_stripped = AuthorityReconciled(authority_id="  auth_123  ")
    assert probe_stripped.authority_id == "auth_123"

    probe_long_reason = AuthorityReconciled(authority_id="auth", reason="r" * 600)
    assert len(probe_long_reason.reason) == 512
    assert probe_long_reason.reason == "r" * 512

    probe_short_reason = AuthorityReconciled(authority_id="auth", reason="ok")
    assert probe_short_reason.reason == "ok"

    # 2. TrajectoryReport stall_sites and top_failure_action_types list trimming
    report = TrajectoryReport(
        report_id="rep_1",
        window_start=0,
        window_end=10,
        compaction_seq=1,
        attempts=2,
        scar_rate=0.0,
        stall_sites=["site_1", "site_2", "site_3", "site_4", "site_5", "site_6"],
        top_failure_action_types=["f" * 200],
    )
    assert len(report.stall_sites) == 5
    assert report.stall_sites == ["site_1", "site_2", "site_3", "site_4", "site_5"]
    assert len(report.top_failure_action_types) == 1
    assert len(report.top_failure_action_types[0]) == 128
    assert report.top_failure_action_types[0] == "f" * 128

    report_fails = TrajectoryReport(
        report_id="rep_2",
        window_start=0,
        window_end=10,
        compaction_seq=1,
        attempts=2,
        scar_rate=0.0,
        top_failure_action_types=["f1", "f2", "f3", "f4", "f5", "f6"],
    )
    assert len(report_fails.top_failure_action_types) == 5

    # 3. ConstraintPin sha256 lowercase hex verification
    with pytest.raises(ValidationError, match="sha256 must be exactly 64 lowercase hex characters"):
        ConstraintPin(constraint_id="pin_1", sha256="a" * 63)

    with pytest.raises(ValidationError, match="sha256 must be exactly 64 lowercase hex characters"):
        ConstraintPin(constraint_id="pin_1", sha256="a" * 65)

    with pytest.raises(ValidationError, match="sha256 must be exactly 64 lowercase hex characters"):
        ConstraintPin(constraint_id="pin_1", sha256="A" * 64)

    with pytest.raises(ValidationError, match="sha256 must be exactly 64 lowercase hex characters"):
        ConstraintPin(constraint_id="pin_1", sha256="z" * 64)

    pin = ConstraintPin(constraint_id="pin_1", sha256="a" * 64)
    assert pin.sha256 == "a" * 64

    # 4. SemanticState active_pins() returning pinned tuple
    state = make_state(pins={"pin_1": pin})
    assert state.active_pins() == (pin,)


def test_provenance_reproducible() -> None:
    p_repro = Provenance(origin=Origin.DETERMINISTIC, source_sequence=1)
    assert p_repro.reproducible is True

    p_no_seq = Provenance(origin=Origin.DETERMINISTIC, source_sequence=None)
    assert p_no_seq.reproducible is False

    p_human = Provenance(origin=Origin.HUMAN, source_sequence=1)
    assert p_human.reproducible is False


def test_constraint_id_and_pin_dropped_validators() -> None:
    for bad_id in ("", "id with space", "id$special", "a" * 129):
        with pytest.raises(
            ValueError,
            match=r"constraint_id must be 1-128 chars from ASCII letters, digits and \. _ : -",
        ):
            _validated_constraint_id(bad_id)

    assert _validated_constraint_id("pin_1") == "pin_1"
    assert _validated_constraint_id("a.b:c-d") == "a.b:c-d"

    # ConstraintPinned validation
    with pytest.raises(
        ValidationError,
        match=r"constraint_id must be 1-128 chars from ASCII letters, digits and \. _ : -",
    ):
        ConstraintPinned(constraint_id="bad id", sha256="a" * 64)

    with pytest.raises(ValidationError, match="sha256 must be exactly 64 lowercase hex characters"):
        ConstraintPinned(constraint_id="pin_1", sha256="a" * 63)

    cp = ConstraintPinned(constraint_id="pin_1", sha256="a" * 64)
    assert cp.constraint_id == "pin_1"
    assert cp.sha256 == "a" * 64

    # ConstraintRetracted validation
    with pytest.raises(
        ValidationError,
        match=r"constraint_id must be 1-128 chars from ASCII letters, digits and \. _ : -",
    ):
        ConstraintRetracted(constraint_id="bad id")

    cr = ConstraintRetracted(constraint_id="pin_1")
    assert cr.constraint_id == "pin_1"

    # ConstraintPinDropped validation
    with pytest.raises(
        ValidationError,
        match=r"constraint_id must be 1-128 chars from ASCII letters, digits and \. _ : -",
    ):
        ConstraintPinDropped(constraint_id="bad id", sha256="a" * 64)

    cpd = ConstraintPinDropped(constraint_id="pin_1", sha256="a" * 64)
    assert cpd.constraint_id == "pin_1"


def test_attempt_lesson_bounds() -> None:
    lesson = AttemptLesson(
        attempt_id="att_1",
        falsified="f" * 600,
        env_delta="e" * 600,
        next_avoid="n" * 600,
        source_evidence=["ev_1", "ev_2" * 300],
        scar_action_ids=["act_1", "act_2"],
    )
    assert len(lesson.falsified) == 512
    assert lesson.falsified == "f" * 512
    assert len(lesson.env_delta) == 512
    assert lesson.env_delta == "e" * 512
    assert len(lesson.next_avoid) == 512
    assert lesson.next_avoid == "n" * 512
    assert len(lesson.source_evidence[1]) == 512
    assert lesson.scar_action_ids == ["act_1", "act_2"]

    lesson_short = AttemptLesson(
        attempt_id="att_2",
        falsified="short",
        env_delta="delta",
        next_avoid="avoid",
    )
    assert lesson_short.falsified == "short"
    assert lesson_short.env_delta == "delta"
    assert lesson_short.next_avoid == "avoid"


def test_authority_consumed_bounds() -> None:
    with pytest.raises(ValidationError, match="authority_id must be non-empty"):
        AuthorityConsumed(authority_id="   ", consumer_run_id="run_1")

    with pytest.raises(ValueError, match="authority_id must be 1-128 characters"):
        AuthorityConsumed._authority_id_valid("a" * 129)

    ac = AuthorityConsumed(authority_id="  auth_123  ", consumer_run_id="  run_1  ")
    assert ac.authority_id == "auth_123"
    assert ac.consumer_run_id == "run_1"
    assert ac.via_action_id is None
    assert AuthorityConsumed._via_action_id_valid(None) is None

    with pytest.raises(ValidationError, match="consumer_run_id must be non-empty"):
        AuthorityConsumed(authority_id="auth_1", consumer_run_id="   ")

    with pytest.raises(ValidationError, match="via_action_id must be non-empty when provided"):
        AuthorityConsumed(authority_id="auth_1", consumer_run_id="run_1", via_action_id="   ")

    with pytest.raises(ValueError, match="via_action_id must be at most 256 characters"):
        AuthorityConsumed._via_action_id_valid("a" * 257)

    ac_via = AuthorityConsumed(
        authority_id="auth_1", consumer_run_id="run_1", via_action_id="  act_123  "
    )
    assert ac_via.via_action_id == "act_123"


def test_trajectory_report_digests() -> None:
    report = TrajectoryReport(
        report_id="rep_test",
        window_start=0,
        window_end=10,
        compaction_seq=1,
        attempts=2,
        scar_rate=0.0,
    )
    dig = report.digest()
    assert isinstance(dig, str) and len(dig) == 64
    assert report.digest_matches() is False

    matching_report = report.model_copy(update={"report_id": dig[: len(report.report_id)]})
    assert matching_report.digest_matches() is True

    leg_dig = report.legacy_digest("run_123")
    assert isinstance(leg_dig, str) and len(leg_dig) == 64
    assert report.legacy_digest_matches("run_123") is False

    matching_leg_report = report.model_copy(update={"report_id": leg_dig[: len(report.report_id)]})
    assert matching_leg_report.legacy_digest_matches("run_123") is True


def test_risk_helpers_and_models() -> None:
    # 1. _risk_trigger
    with pytest.raises(ValueError, match="trigger must name a risk class"):
        _risk_trigger(None)

    with pytest.raises(ValueError, match="trigger must name a risk class"):
        _risk_trigger("")

    with pytest.raises(ValueError, match="trigger must name a risk class"):
        _risk_trigger("   ")

    assert _risk_trigger("  LOOP  ") == "loop"
    assert _risk_trigger("x" * 200) == "x" * 128

    # 2. _risk_score
    assert _risk_score(None) == 0.0
    assert _risk_score(True) == 0.0
    assert _risk_score(False) == 0.0
    assert _risk_score("unparseable") == 0.0
    assert _risk_score(float("nan")) == 0.0
    assert _risk_score(float("inf")) == 0.0
    assert _risk_score(float("-inf")) == 0.0
    assert _risk_score(-0.5) == 0.0
    assert _risk_score(1.5) == 1.0
    assert _risk_score(0.75) == 0.75
    assert _risk_score("0.75") == 0.75

    # 3. _risk_optional_id
    assert _risk_optional_id(None) is None
    assert _risk_optional_id("   ") is None
    assert _risk_optional_id("  ep_123  ") == "ep_123"
    assert _risk_optional_id("y" * 200) == "y" * 128
    assert _risk_optional_id(12345) == "12345"

    # 4. RiskObservedPayload model
    p = RiskObservedPayload(
        trigger="  STALL  ",
        score=1.5,
        episode_id="  ep_1  ",
        step_id="  step_1  ",
        detail={"info": "test"},
        ts="2026-01-01T00:00:00Z",
    )
    assert p.trigger == "stall"
    assert p.score == 1.0
    assert p.episode_id == "ep_1"
    assert p.step_id == "step_1"
    assert p.detail == {"info": "test"}

    p_fallback = RiskObservedPayload(trigger="LOOP", score="invalid", ts="not_a_ts")
    assert p_fallback.trigger == "loop"
    assert p_fallback.score == 0.0
    assert isinstance(p_fallback.ts, datetime)

    # 5. ObservedRisk model
    o = ObservedRisk(
        trigger="  MELTDOWN  ",
        score=-0.5,
        episode_id="  ep_2  ",
        step_id="  step_2  ",
        detail={"info": "meltdown"},
        ts="2026-01-01T00:00:00Z",
    )
    assert o.trigger == "meltdown"
    assert o.score == 0.0
    assert o.episode_id == "ep_2"
    assert o.step_id == "step_2"

    o_fallback = ObservedRisk(trigger="LOOP", ts="not_a_ts")
    assert o_fallback.trigger == "loop"
    assert isinstance(o_fallback.ts, datetime)


def test_semantic_state_lookups() -> None:
    d1 = Decision(
        decision_id="dec_1",
        decision="dec_1 text",
        reason="r1",
        evidence=["ev_1", "find_1"],
    )
    f1 = Finding(finding_id="find_1", claim="c1", evidence=["ev_2"])
    dep1 = ExternalDependency(resource="postgres://db", kind="database")
    pin1 = ConstraintPin(constraint_id="pin_1", sha256="a" * 64)
    ev1 = Evidence(evidence_id="ev_1", summary="summary1")
    ev2 = Evidence(evidence_id="ev_2", summary="summary2")
    pw1 = PendingWork(description="work1", status=StateStatus.VALID)
    pw2 = PendingWork(description="work2", status=StateStatus.INVALID)

    state = make_state(
        decisions=[d1],
        findings=[f1],
        external_dependencies=[dep1],
        pins={"pin_1": pin1},
        evidence=[ev1, ev2],
        pending_work=[pw1, pw2],
    )

    assert state.decision("dec_1") == d1
    assert state.decision("dec_missing") is None
    assert state.finding("find_1") == f1
    assert state.finding("find_missing") is None
    assert state.dependency("postgres://db") == dep1
    assert state.dependency("missing") is None
    assert state.pin("pin_1") == pin1
    assert state.pin("missing") is None
    assert state.active_pins() == (pin1,)
    assert state.evidence_ids() == frozenset({"ev_1", "ev_2"})
    assert state.valid_decisions() == (d1,)
    assert state.open_work() == (pw1,)
    assert state.dangling_evidence() == frozenset()

    # Dangling evidence when references do not match known evidence or findings
    d2 = Decision(
        decision_id="dec_2",
        decision="dec_2 text",
        reason="r2",
        evidence=["dangling_ev"],
    )
    f2 = Finding(finding_id="find_2", claim="c2", evidence=["dangling_ev_2"])
    state_dangling = make_state(decisions=[d2], findings=[f2])
    assert state_dangling.dangling_evidence() == frozenset({"dangling_ev", "dangling_ev_2"})


def test_action_origin_digest_and_correlation_id() -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64

    # 1. origin_observation_digest synced to origin_digest
    a1 = Action(run_id="run_1", action_type="t", origin_observation_digest=digest_a)
    assert a1.origin_digest == digest_a
    assert a1.origin_observation_digest == digest_a

    # 2. origin_digest synced to origin_observation_digest
    a2 = Action(run_id="run_1", action_type="t", origin_digest=digest_a)
    assert a2.origin_digest == digest_a
    assert a2.origin_observation_digest == digest_a

    # 3. Disagreement raises ValueError
    with pytest.raises(
        ValidationError, match="origin_digest and origin_observation_digest disagree"
    ):
        Action(
            run_id="run_1",
            action_type="t",
            origin_digest=digest_a,
            origin_observation_digest=digest_b,
        )

    # 4. Invalid origin_digest format
    with pytest.raises(ValidationError, match="origin_digest must be 64 lowercase hex characters"):
        Action(run_id="run_1", action_type="t", origin_digest="not_a_sha256")

    assert Action._origin_digest_is_sha256(None) is None
    assert Action._sync_origin_observation_digest("not_a_dict") == "not_a_dict"

    # 5. validate_correlation_id helper
    assert validate_correlation_id(None) is None
    with pytest.raises(ValueError, match="correlation_id must be a string"):
        validate_correlation_id(12345)

    with pytest.raises(ValueError, match="correlation_id must be 1-128 characters and non-empty"):
        validate_correlation_id("")

    with pytest.raises(ValueError, match="correlation_id must be 1-128 characters and non-empty"):
        validate_correlation_id("   ")

    with pytest.raises(ValueError, match="correlation_id must be 1-128 characters and non-empty"):
        validate_correlation_id("c" * 129)

    assert validate_correlation_id("corr_123") == "corr_123"

    # 6. Action and ActionRecordPayload with correlation_id
    act = Action(run_id="run_1", action_type="t", correlation_id="corr_action")
    assert act.correlation_id == "corr_action"

    payload = ActionRecordPayload(action_type="t", correlation_id="corr_payload")
    assert payload.correlation_id == "corr_payload"

    with pytest.raises(
        ValidationError, match="correlation_id must be 1-128 characters and non-empty"
    ):
        ActionRecordPayload(action_type="t", correlation_id="   ")


def test_unknown_side_effect_and_run_checkpoint() -> None:
    # 1. UnknownSideEffect
    err = UnknownSideEffect("side effect occurred", action_key="key_1", action_id="id_1")
    assert str(err) == "side effect occurred"
    assert err.action_key == "key_1"
    assert err.action_id == "id_1"

    err_empty = UnknownSideEffect("unknown")
    assert err_empty.action_key is None
    assert err_empty.action_id is None

    # 2. Run touch
    run = Run(run_id="run_1", goal="goal")
    run_touched = run.touch()
    assert run_touched.updated_at >= run.updated_at

    # 3. StateCheckpoint methods
    state = make_state()
    ckpt = StateCheckpoint(run_id="run_1", state=state)
    content = ckpt.content()
    assert "state" in content
    assert "integrity_hash" not in content
    assert ckpt.canonical_json()
    assert ckpt.digest() == stable_hash(ckpt.content())
    assert ckpt.verify() is False

    sealed = ckpt.sealed()
    assert sealed.integrity_hash == ckpt.digest()
    assert sealed.verify() is True

    tampered = sealed.model_copy(update={"reason": "tampered"})
    assert tampered.verify() is False
