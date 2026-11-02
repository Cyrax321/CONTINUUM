"""Regression tests for risk-informed recovery assessment and located guidance.

Issue #1423 covers two located-guidance behaviours, both driven through the
typed RiskSignal projection the planner consumes:

1. ``plan_repairs()`` emits located repair steps for exactly two trigger
   classes: a ``loop`` signal yields ``REPLAN_EXCLUDING`` steps naming the
   repeating steps or action signatures to avoid, and a ``meltdown`` signal
   yields a ``HUMAN_REVIEW`` step targeting the last verified fact-gathering
   step before the anomalous cascade, or an honest gap statement when none
   can be located. The other trigger classes drive the mode only and produce
   no steps; that is by design, and the parametrised mode test below asserts
   the mode without asserting a plan for them.
2. Mode selection across all eight trigger classes stays deterministic, and
   maximum-severity precedence keeps tamper and uncertain external effects
   ahead of soft risk signals.

The risk evaluation in ``assess()`` predates this work (issue #303); these
tests pin its located-guidance additions, not its invention.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.events import EventType
from continuum.models import (
    Evidence,
    Finding,
    Origin,
    PlanStep,
    PlanStepStatus,
    Provenance,
    RecoveryMode,
    RecoverySafety,
    Run,
    SemanticState,
    StateStatus,
)
from continuum.recovery import RecoveryEngine, RepairKind, plan_repairs
from continuum.recovery.guidance import human_steps_for
from continuum.recovery.risk import (
    RiskSignal,
    ingest_risk,
    locate_last_fact_gathering_step,
    signal_from_event,
    signals_from_events,
)
from continuum.storage import SQLiteStorage


def _make_run(store: SQLiteStorage, run_id: str = "r1", goal: str = "test goal") -> None:
    store.create_run_started(Run(run_id=run_id, goal=goal))


def _state(*steps: PlanStep, **kwargs: object) -> SemanticState:
    return SemanticState(
        run_id="run_t", goal={"description": "objective"}, plan=list(steps), **kwargs
    )


def _step(
    step_id: str,
    description: str,
    status: PlanStepStatus = PlanStepStatus.PENDING,
    evidence: list[str] | None = None,
    depends_on: list[str] | None = None,
    source_sequence: int | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=description,
        status=status,
        evidence=evidence if evidence is not None else [],
        depends_on=depends_on if depends_on is not None else [],
        provenance=Provenance(source_sequence=source_sequence),
    )


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


# --- RiskSignal extraction (the typed projection the planner consumes) ---


def test_signal_from_event_reads_plain_mapping() -> None:
    signal = signal_from_event(
        {"trigger": "loop", "step_id": "step_a", "episode_id": "ep1", "detail": {"n": 1}}
    )
    assert signal == RiskSignal("loop", "step_a", "ep1", {"n": 1})


def test_signal_from_event_reads_event_payload_attribute() -> None:
    event = types.SimpleNamespace(
        payload={"trigger": "meltdown", "step_id": "step_c", "episode_id": "ep7", "detail": {}}
    )
    assert signal_from_event(event) == RiskSignal("meltdown", "step_c", "ep7", {})


def test_signal_from_event_reads_plain_object_attributes() -> None:
    # Older probes and hand-rolled doubles expose fields as attributes with no
    # payload dict; the projection still has to read them (issue #1423 review).
    probe = types.SimpleNamespace(
        trigger="loop", step_id="step_x", episode_id=None, detail={"repeating_steps": ["s1"]}
    )
    assert signal_from_event(probe) == RiskSignal(
        "loop", "step_x", None, {"repeating_steps": ["s1"]}
    )


def test_signal_from_event_folds_detail_episode_id() -> None:
    signal = signal_from_event({"trigger": "loop", "detail": {"episode_id": "ep_42"}})
    assert signal is not None
    assert signal.episode_id == "ep_42"


def test_signal_from_event_drops_missing_or_blank_trigger() -> None:
    assert signal_from_event({}) is None
    assert signal_from_event({"trigger": "  "}) is None
    assert signal_from_event({"trigger": None, "step_id": "s"}) is None
    assert signal_from_event(types.SimpleNamespace(payload="not-a-mapping")) is None


def test_signals_from_events_preserves_order_and_drops_malformed() -> None:
    events = [
        types.SimpleNamespace(payload={"trigger": "loop", "step_id": "a"}),
        "garbage",
        types.SimpleNamespace(payload={"trigger": "meltdown"}),
    ]
    assert [s.trigger for s in signals_from_events(events)] == ["loop", "meltdown"]


def test_signals_from_events_normalises_detail_to_a_dict() -> None:
    signal = signal_from_event({"trigger": "loop", "detail": "not-a-dict"})
    assert signal is not None
    assert signal.detail == {}


# --- Located repair guidance for loop ---


def test_loop_generates_located_replan_step_in_plan_repairs() -> None:
    plan = plan_repairs(risk_events=[RiskSignal("loop", "fetch_user_profile", "ep_101")])
    assert len(plan.steps) == 1
    step = plan.steps[0]

    # The target is a step or signature, never a decision id: this is its own
    # kind, not REVIEW_DECISION (issue #1423 review).
    assert step.kind is RepairKind.REPLAN_EXCLUDING
    assert step.action_name == "replan_excluding:fetch_user_profile"
    assert step.target == "fetch_user_profile"
    assert step.requires_human is False
    assert step.blocking is True

    assert "replan excluding the repeating trajectory" in step.reason
    assert "trigger: loop" in step.reason
    assert "episode_id: ep_101" in step.reason
    assert "step_id:" not in step.reason


def test_loop_locates_repeated_action_signatures_from_detail() -> None:
    # The signature lives in detail while the episode rides along nested
    # beside it; the extraction folds the episode onto the signal.
    signal = signal_from_event(
        {
            "trigger": "loop",
            "detail": {"action_signature": "github.search_issues", "episode_id": "ep_42"},
        }
    )
    assert signal is not None
    plan = plan_repairs(risk_events=[signal])
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.REPLAN_EXCLUDING
    assert step.target == "github.search_issues"
    assert "trigger: loop" in step.reason
    assert "episode_id: ep_42" in step.reason


def test_loop_expands_list_of_repeating_steps_each_with_its_own_reason() -> None:
    plan = plan_repairs(
        risk_events=[
            RiskSignal(
                "loop",
                "step_api_call",
                "ep_3",
                detail={"repeating_steps": ["step_api_call", "step_retry"]},
            )
        ]
    )
    assert [s.target for s in plan.steps] == ["step_api_call", "step_retry"]
    retry_step = plan.steps[1]
    # No cross-wired ids: the second step's reason must not carry the event's
    # step_id as though it were its own (issue #1423 review).
    assert "step_id: step_api_call" not in retry_step.reason
    assert "step_retry" in retry_step.reason
    assert "episode_id: ep_3" in retry_step.reason


def test_loop_without_locatable_target_names_trigger_and_episode() -> None:
    plan = plan_repairs(risk_events=[RiskSignal("loop", None, "ep9")])
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.kind is RepairKind.REPLAN_EXCLUDING
    assert step.target == "ep9"
    assert "trigger: loop" in step.reason
    assert "episode_id: ep9" in step.reason


def test_loop_with_no_targets_or_episode_names_the_trigger() -> None:
    plan = plan_repairs(risk_events=[RiskSignal("loop")])
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.target == "trigger_loop"
    assert "trigger: loop" in step.reason


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
    loop_steps = decision.plan.of_kind(RepairKind.REPLAN_EXCLUDING)
    targets = [s.target for s in loop_steps]
    assert targets == ["step_a", "step_b"]
    for s in loop_steps:
        assert "replan excluding the repeating trajectory" in s.reason
        assert "trigger: loop" in s.reason
        assert "episode_id: ep_1" in s.reason


def test_loop_step_renders_its_own_guidance_line(tmp_path: Path) -> None:
    db = str(tmp_path / "loop_guidance.db")
    with SQLiteStorage(db) as store:
        run_id = "run_loop_guidance"
        _make_run(store, run_id=run_id)
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "step_a", "episode_id": "ep_1"})

        decision = RecoveryEngine(store).assess(run_id, replay=False)
        steps = human_steps_for(decision, run_id=run_id)

    assert any(s.startswith("replan excluding the repeating trajectory at step_a") for s in steps)
    assert not any("review decision step_a" in s for s in steps)


# --- Located repair guidance for meltdown ---


def test_meltdown_identifies_last_verified_fact_gathering_step_from_plan() -> None:
    state = _state(
        _step(
            "step_gather",
            "gather input data from external api",
            PlanStepStatus.VERIFIED,
            evidence=["ev_1"],
        ),
        _step("step_process", "process records", PlanStepStatus.VERIFIED),
        _step("step_cascade", "uncontrolled cascade recursion", PlanStepStatus.IN_PROGRESS),
    )
    plan = plan_repairs(
        risk_events=[RiskSignal("meltdown", "step_cascade", "ep_cascade_9")], state=state
    )
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.HUMAN_REVIEW
    assert step.target == "step_gather"
    assert step.requires_human is True
    assert step.blocking is True

    assert "rollback to the last verified fact-gathering step step_gather" in step.reason
    assert "step_id: step_cascade" in step.reason
    assert "trigger: meltdown" in step.reason
    assert "episode_id: ep_cascade_9" in step.reason


def test_meltdown_keyword_matching_uses_word_boundaries() -> None:
    # "already" contains "read" and "thread" contains "read"; under substring
    # matching the unrelated "already refactored" step beat the real
    # fact-gathering one (issue #1423 review).
    state = _state(
        _step("step_baseline", "collect baseline metrics", PlanStepStatus.VERIFIED),
        _step("step_cleanup", "already refactored the retry budget", PlanStepStatus.VERIFIED),
        _step("step_cascade", "uncontrolled cascade recursion", PlanStepStatus.IN_PROGRESS),
    )
    plan = plan_repairs(risk_events=[RiskSignal("meltdown", "step_cascade", "ep_9")], state=state)
    assert plan.steps[0].target == "step_baseline"


def test_meltdown_ranks_evidence_over_position() -> None:
    # Two fact-gathering steps: the earlier one carries evidence, the later
    # one does not. Evidence outranks position, so the earlier one wins.
    state = _state(
        _step("step_query_db", "query the database", PlanStepStatus.VERIFIED, evidence=["ev_9"]),
        _step("step_query_cache", "query the cache", PlanStepStatus.VERIFIED),
        _step("step_cascade", "cascade", PlanStepStatus.IN_PROGRESS),
    )
    fact = locate_last_fact_gathering_step(state, trigger_step_id="step_cascade")
    assert fact == "step_query_db"


def test_meltdown_prefers_later_fact_step_among_equals() -> None:
    state = _state(
        _step("step_fetch_a", "fetch records", PlanStepStatus.VERIFIED, evidence=["ev_1"]),
        _step("step_fetch_b", "fetch more records", PlanStepStatus.VERIFIED, evidence=["ev_2"]),
        _step("step_cascade", "cascade", PlanStepStatus.IN_PROGRESS),
    )
    fact = locate_last_fact_gathering_step(state, trigger_step_id="step_cascade")
    assert fact == "step_fetch_b"


def test_meltdown_honours_explicit_detail_anchors() -> None:
    state = _state(_step("step_cascade", "cascade", PlanStepStatus.IN_PROGRESS))
    for key in ("last_verified_step", "fact_gathering_step", "rollback_target"):
        detail = {key: "explicit_anchor"}
        assert locate_last_fact_gathering_step(state, detail=detail) == "explicit_anchor"


def test_meltdown_excludes_the_trigger_step_from_candidates() -> None:
    # The trigger step is the one melting down; it must never be its own
    # rollback anchor even when it reads as fact gathering.
    state = _state(_step("step_read_log", "read the log", PlanStepStatus.VERIFIED))
    fact = locate_last_fact_gathering_step(state, trigger_step_id="step_read_log")
    assert fact is None


def test_meltdown_locates_the_anchor_via_dependencies_despite_step_id_sort() -> None:
    # ``plan_upsert`` stores ``state.plan`` sorted by ``step_id``, so a
    # failing ``a_cascade`` sorts before the completed ``z_fetch`` that
    # produced it. A positional slice drops the only real anchor and leaves the
    # cascade itself as the target; the ``depends_on`` graph is the ordering
    # the plan records, so the lookup must use it (issue #1423 review).
    state = _state(
        _step("a_cascade", "cascade", PlanStepStatus.IN_PROGRESS, depends_on=["z_fetch"]),
        _step("z_fetch", "fetch the baseline records", PlanStepStatus.COMPLETED),
    )
    fact = locate_last_fact_gathering_step(state, trigger_step_id="a_cascade")
    assert fact == "z_fetch"


def test_meltdown_locates_the_anchor_via_recorded_sequence() -> None:
    # Same hazard, different signal: ``z_fetch`` sorts after ``a_cascade`` but
    # was recorded before it, which ``provenance.source_sequence`` carries and
    # a positional slice cannot see (issue #1423 review).
    state = _state(
        _step("a_cascade", "cascade", PlanStepStatus.IN_PROGRESS, source_sequence=100),
        _step("z_fetch", "fetch records", PlanStepStatus.VERIFIED, source_sequence=50),
    )
    fact = locate_last_fact_gathering_step(state, trigger_step_id="a_cascade")
    assert fact == "z_fetch"


def test_meltdown_never_admits_an_unverified_fact_step() -> None:
    # A fact-gathering step that is still in progress is not the last
    # *verified* one, even when it already carries evidence. Nothing honest
    # can be named, so the plan says so instead of labelling it.
    state = _state(
        _step("step_fetch_live", "fetch live data", PlanStepStatus.IN_PROGRESS, evidence=["ev"]),
        _step("step_cascade", "cascade", PlanStepStatus.IN_PROGRESS),
    )
    plan = plan_repairs(risk_events=[RiskSignal("meltdown", "step_cascade", "ep_1")], state=state)
    step = plan.steps[0]
    assert step.kind is RepairKind.HUMAN_REVIEW
    assert "no verified fact-gathering step could be located" in step.reason
    assert "last verified fact-gathering step step_fetch_live" not in step.reason


def test_meltdown_skips_explicit_anchor_that_names_the_trigger_step() -> None:
    # A monitor that points the rollback at the step that is melting down is
    # naming the failure, not an anchor before it: ignore that anchor and keep
    # looking instead of echoing it back as guidance.
    state = _state(_step("step_blowup", "cascade", PlanStepStatus.IN_PROGRESS))
    fact = locate_last_fact_gathering_step(
        state, trigger_step_id="step_blowup", detail={"fact_gathering_step": "step_blowup"}
    )
    assert fact is None


def test_meltdown_skips_non_valid_evidence_and_findings() -> None:
    # Evidence the projection marked STALE or INVALID is not a verified fact,
    # so it may not name the rollback anchor either; nothing is located rather
    # than the newest rejected entry.
    state = SemanticState(
        run_id="run_invalid",
        goal={"description": "objective"},
        evidence=[
            Evidence(evidence_id="ev_stale", summary="stale", status=StateStatus.STALE),
            Evidence(evidence_id="ev_bad", summary="invalid", status=StateStatus.INVALID),
        ],
    )
    assert locate_last_fact_gathering_step(state, trigger_step_id="step_x") is None


def test_meltdown_identifies_last_verified_fact_from_evidence_when_plan_absent() -> None:
    state = SemanticState(
        run_id="run_ev",
        goal={"description": "objective"},
        evidence=[
            Evidence(evidence_id="ev_fact_1", summary="fact 1", status=StateStatus.VALID),
            Evidence(evidence_id="ev_fact_2", summary="fact 2", status=StateStatus.VALID),
        ],
    )
    plan = plan_repairs(risk_events=[RiskSignal("meltdown", "step_fail", "ep_12")], state=state)
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.HUMAN_REVIEW
    assert step.target == "ev_fact_2"
    assert step.requires_human is True
    assert "rollback to the last verified fact-gathering step ev_fact_2" in step.reason
    assert "step_id: step_fail" in step.reason
    assert "trigger: meltdown" in step.reason
    assert "episode_id: ep_12" in step.reason


def test_meltdown_without_locatable_anchor_reports_the_gap_not_the_trigger_step() -> None:
    # Nothing honest to anchor on: empty plan, no evidence, no findings. The
    # step must say no fact-gathering step was located instead of presenting
    # the melting-down step as one (issue #1423 review).
    state = SemanticState(run_id="run_gap", goal={"description": "objective"})
    plan = plan_repairs(risk_events=[RiskSignal("meltdown", "step_blowup", "ep_99")], state=state)
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.HUMAN_REVIEW
    assert step.target == "step_blowup"
    assert "no verified fact-gathering step could be located" in step.reason
    assert "last verified fact-gathering step step_blowup" not in step.reason
    assert "trigger: meltdown" in step.reason
    assert "episode_id: ep_99" in step.reason


def test_meltdown_without_semantic_state_reports_the_gap() -> None:
    # A run with no semantic state at all has nothing to anchor on. There is
    # no plan to scan, so no fact-gathering step exists to name, and the step
    # says so rather than presenting the melting-down step as the anchor.
    plan = plan_repairs(risk_events=[RiskSignal("meltdown", "step_blowup", "ep_1")])
    assert len(plan.steps) == 1
    step = plan.steps[0]

    assert step.kind is RepairKind.HUMAN_REVIEW
    assert step.target == "step_blowup"
    assert "no verified fact-gathering step could be located" in step.reason
    assert "last verified fact-gathering step step_blowup" not in step.reason


def test_meltdown_scans_the_whole_plan_when_no_trigger_step_is_named() -> None:
    # Without a trigger step there is no cascade boundary, so every step is a
    # candidate; the unverified cascade step is still excluded by status, and
    # the last verified fact-gathering step still wins.
    state = _state(
        _step("step_fetch", "fetch the baseline", PlanStepStatus.VERIFIED, evidence=["ev"]),
        _step("step_cascade", "cascade", PlanStepStatus.IN_PROGRESS),
    )
    fact = locate_last_fact_gathering_step(state)
    assert fact == "step_fetch"


def test_meltdown_falls_back_to_findings_when_plan_and_evidence_are_absent() -> None:
    state = SemanticState(
        run_id="run_find",
        goal={"description": "objective"},
        findings=[
            Finding(finding_id="f_1", claim="earlier", status=StateStatus.INVALID),
            Finding(finding_id="f_2", claim="later valid", status=StateStatus.VALID),
        ],
    )
    fact = locate_last_fact_gathering_step(state, trigger_step_id="missing")
    assert fact == "f_2"


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
