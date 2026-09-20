"""Edge cases in projection: malformed logs, partial payloads, unusual orders.

These are the paths a crashed or half-written log actually takes, so they get
the same attention as the happy path.
"""

from __future__ import annotations

import pytest

from continuum.events import Event, EventLog, EventType
from continuum.models import Decision, Evidence, Finding, Goal, SemanticState, StateStatus
from continuum.state.extractor import DeterministicExtractor, ExtractionContext, LLMExtractor
from continuum.state.semantic import ProjectionError, project, project_incremental
from continuum.state.versioning import VersionChain


def started(log: EventLog, **payload: object) -> EventLog:
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g", **payload})
    return log


# --- malformed or hostile logs --------------------------------------------- #


def test_task_updated_before_run_started_is_rejected() -> None:
    log = EventLog()
    log.append("run_1", EventType.TASK_UPDATED, {"goal": "orphan"})
    with pytest.raises(ProjectionError, match="before the run was started"):
        project("run_1", log.events("run_1"))


def test_invalidating_an_unknown_finding_is_rejected() -> None:
    log = started(EventLog())
    log.append("run_1", EventType.FINDING_INVALIDATED, {"finding_id": "ghost"})
    with pytest.raises(ProjectionError, match="unknown finding"):
        project("run_1", log.events("run_1"))


def test_an_empty_event_stream_has_nothing_to_project() -> None:
    with pytest.raises(ProjectionError, match="never recorded RUN_STARTED"):
        project("run_1", [])


def test_unknown_event_types_are_counted_not_fatal() -> None:
    """A newer writer's vocabulary must not make a run unrecoverable."""
    log = started(EventLog())
    known = log.events("run_1")[0]
    future = Event(
        run_id="run_1",
        sequence=2,
        type=EventType.TOOL_CALLED,
        payload={},
        prev_hash=known.hash,
    ).sealed()
    unknown = future.model_copy(update={"type": "QUANTUM_ENTANGLED"})

    state, report = project_incremental("run_1", [known, unknown])
    assert state.goal.description == "g"
    assert report.ignored_types == {"QUANTUM_ENTANGLED": 1}
    assert not report.complete


# --- partial and unusual payloads ------------------------------------------ #


def test_task_updated_can_carry_progress_without_a_goal_change() -> None:
    log = started(EventLog(), total=100)
    log.append("run_1", EventType.TASK_UPDATED, {"completed": 40, "pending": 60})
    state = project("run_1", log.events("run_1"))
    assert state.goal.description == "g"
    assert state.goal.version == 1
    assert state.progress.completed == 40


def test_task_updated_with_no_recognised_fields_is_a_no_op() -> None:
    log = started(EventLog(), total=10)
    before = project("run_1", log.events("run_1"))
    log.append("run_1", EventType.TASK_UPDATED, {"note": "just a comment"})
    after = project("run_1", log.events("run_1"))
    assert after.progress == before.progress
    assert after.goal == before.goal


def test_explicit_goal_version_overrides_the_automatic_bump() -> None:
    log = started(EventLog())
    log.append("run_1", EventType.TASK_UPDATED, {"goal": "revised", "goal_version": 9})
    assert project("run_1", log.events("run_1")).goal.version == 9


def test_constraints_survive_a_goal_revision_that_omits_them() -> None:
    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g", "constraints": ["peer-reviewed"]})
    log.append("run_1", EventType.TASK_UPDATED, {"goal": "g2"})
    assert project("run_1", log.events("run_1")).goal.constraints == ["peer-reviewed"]


def test_a_single_evidence_string_is_accepted_as_a_list() -> None:
    log = started(EventLog())
    log.append(
        "run_1",
        EventType.DECISION_CREATED,
        {"decision_id": "d1", "decision": "x", "evidence": "paper_1"},
    )
    decision = project("run_1", log.events("run_1")).decision("d1")
    assert decision is not None and decision.evidence == ["paper_1"]


def test_work_completed_can_advance_by_more_than_one() -> None:
    log = started(EventLog(), total=100)
    log.append("run_1", EventType.WORK_COMPLETED, {"count": 25})
    assert project("run_1", log.events("run_1")).progress.completed == 25


def test_optional_evidence_and_dependency_fields_stay_none() -> None:
    log = started(EventLog())
    log.append("run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "e1"})
    log.append("run_1", EventType.DEPENDENCY_DECLARED, {"resource": "api"})
    state = project("run_1", log.events("run_1"))
    assert state.evidence[0].source is None
    assert state.evidence[0].checksum is None
    dependency = state.dependency("api")
    assert dependency is not None
    assert dependency.version is None and dependency.kind == "resource"


def test_granting_an_approval_that_was_never_requested_still_records_it() -> None:
    log = started(EventLog())
    log.append(
        "run_1",
        EventType.APPROVAL_GRANTED,
        {
            "approval_id": "ap_9",
            "subject": "deploy",
            "expires_at": "2026-01-01T00:00:00+00:00",
            "reason": "out of band",
        },
    )
    approval = project("run_1", log.events("run_1")).approvals[0]
    assert approval.approval_id == "ap_9"
    assert approval.expires_at is not None
    assert approval.reason == "out of band"


def test_naive_expires_at_is_folded_as_utc() -> None:
    """Issue #704: an expires_at payload without a UTC offset must not fold to
    a naive datetime, because everything downstream compares against the
    tz-aware utcnow(), and a naive value would TypeError and brick validation."""
    log = started(EventLog())
    log.append(
        "run_1",
        EventType.APPROVAL_GRANTED,
        {"approval_id": "ap_10", "subject": "deploy", "expires_at": "2027-01-01"},
    )
    approval = project("run_1", log.events("run_1")).approvals[0]
    assert approval.expires_at is not None
    assert approval.expires_at.tzinfo is not None, "naive expires_at must be normalized to UTC"
    assert approval.expires_at.utcoffset().total_seconds() == 0


def test_model_assumptions_can_be_recorded_before_any_model_is_known() -> None:
    log = started(EventLog())
    log.append(
        "run_1",
        EventType.MODEL_ASSUMPTION_RECORDED,
        {"item_id": "a1", "description": "assumes JSON tool calls"},
    )
    state = project("run_1", log.events("run_1"))
    assert state.model is not None
    assert state.model.model is None
    assert len(state.model.model_specific_state) == 1


def test_a_repeated_assumption_id_replaces_the_previous_one() -> None:
    log = started(EventLog())
    for text in ("first", "second"):
        log.append(
            "run_1", EventType.MODEL_ASSUMPTION_RECORDED, {"item_id": "a1", "description": text}
        )
    state = project("run_1", log.events("run_1"))
    assert state.model is not None
    assert [a.description for a in state.model.model_specific_state] == ["second"]


def test_declaring_the_same_dependency_twice_updates_its_version() -> None:
    log = started(EventLog())
    log.append("run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"})
    log.append("run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v4"})
    state = project("run_1", log.events("run_1"))
    assert len(state.external_dependencies) == 1
    dependency = state.dependency("dataset")
    assert dependency is not None and dependency.version == "v4"


# --- state accessors ------------------------------------------------------- #


def test_lookups_return_none_for_absent_components() -> None:
    state = SemanticState(run_id="run_1", goal=Goal(description="g"))
    assert state.decision("nope") is None
    assert state.finding("nope") is None
    assert state.dependency("nope") is None
    assert state.dangling_evidence() == frozenset()


def test_open_work_excludes_only_invalidated_tasks() -> None:
    from continuum.models import PendingWork

    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="g"),
        pending_work=[
            PendingWork(task_id="t1", description="do"),
            PendingWork(task_id="t2", description="skip", status=StateStatus.INVALID),
            PendingWork(task_id="t3", description="check", status=StateStatus.REQUIRES_REVIEW),
        ],
    )
    assert {w.task_id for w in state.open_work()} == {"t1", "t3"}


def test_a_decision_citing_a_finding_is_not_a_dangling_reference() -> None:
    """Decisions legitimately cite findings, not only raw evidence. Flagging
    that would fire a false alarm on every well-formed reasoning chain."""
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="g"),
        evidence=[Evidence(evidence_id="paper_128")],
        findings=[Finding(finding_id="finding_17", claim="c", evidence=["paper_128"])],
        decisions=[Decision(decision_id="d1", decision="x", evidence=["finding_17"])],
    )
    assert state.dangling_evidence() == frozenset()


def test_evidence_cited_by_a_decision_is_tracked_as_dangling() -> None:
    state = SemanticState(
        run_id="run_1",
        goal=Goal(description="g"),
        decisions=[Decision(decision_id="d1", decision="x", evidence=["missing", "present"])],
        findings=[Finding(finding_id="f1", claim="c", evidence=["present"])],
        evidence=[Evidence(evidence_id="present")],
    )
    assert state.dangling_evidence() == {"missing"}


# --- extractor and version chain edges ------------------------------------- #


def test_an_extractor_can_resume_from_a_base_state() -> None:
    log = started(EventLog(), total=10)
    log.append("run_1", EventType.WORK_COMPLETED, {})
    base = project("run_1", log.events("run_1"))
    log.append("run_1", EventType.WORK_COMPLETED, {})

    extractor = DeterministicExtractor()
    state = extractor.extract(
        ExtractionContext(
            run_id="run_1",
            trajectory=log.events("run_1", after_sequence=base.source_sequence),
            base=base,
        )
    )
    assert state.progress.completed == 2


def test_composite_requires_at_least_one_extractor() -> None:
    from continuum.state.extractor import CompositeExtractor

    with pytest.raises(ValueError, match="at least one extractor"):
        CompositeExtractor([])


def test_llm_proposals_for_existing_work_ids_are_ignored() -> None:
    from continuum.state.extractor import LLMProposal

    log = started(EventLog())
    log.append("run_1", EventType.WORK_ADDED, {"task_id": "t1", "description": "recorded"})
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            pending_work=[{"task_id": "t1", "description": "hijacked"}, {"description": "no id"}]
        )
    )
    state = extractor.extract(ExtractionContext(run_id="run_1", trajectory=log.events("run_1")))
    assert len(state.pending_work) == 1
    assert state.pending_work[0].description == "recorded"


def test_an_empty_version_chain_reports_no_head() -> None:
    chain = VersionChain("run_1")
    assert chain.head is None
    assert chain.current is None
    assert chain.verify()
    assert list(chain) == []


def test_verify_detects_a_renumbered_version() -> None:
    chain = VersionChain("run_1")
    chain.commit(SemanticState(run_id="run_1", goal=Goal(description="g")))
    chain._entries[0] = chain._entries[0].model_copy(update={"version": 7})
    assert not chain.verify()


def test_verify_detects_a_severed_link() -> None:
    chain = VersionChain("run_1")
    chain.commit(SemanticState(run_id="run_1", goal=Goal(description="g")))
    chain.commit(SemanticState(run_id="run_1", goal=Goal(description="g2")))
    chain._entries[1] = chain._entries[1].model_copy(update={"prev_fingerprint": "0" * 64})
    assert not chain.verify()


def test_a_findings_status_can_be_downgraded_without_full_invalidation() -> None:
    log = started(EventLog())
    log.append("run_1", EventType.FINDING_ADDED, {"finding_id": "f1", "claim": "c"})
    log.append(
        "run_1", EventType.FINDING_INVALIDATED, {"finding_id": "f1", "status": "requires_review"}
    )
    finding = project("run_1", log.events("run_1")).finding("f1")
    assert finding is not None and finding.status is StateStatus.REQUIRES_REVIEW


def test_goal_constraint_changes_are_diffed() -> None:
    from continuum.state.diff import diff_states

    before = SemanticState(run_id="run_1", goal=Goal(description="g"))
    after = SemanticState(run_id="run_1", goal=Goal(description="g", constraints=["peer-reviewed"]))
    details = [e.detail for e in diff_states(before, after).entries]
    assert any("constraints: 0 → 1" in d for d in details)


def test_llm_decisions_with_new_ids_are_added_for_review() -> None:
    from continuum.state.extractor import LLMProposal

    log = started(EventLog())
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            decisions=[
                {"decision_id": "d_llm", "decision": "inferred", "evidence": ["e1"]},
                {"decision": "no id"},
            ]
        )
    )
    state = extractor.extract(ExtractionContext(run_id="run_1", trajectory=log.events("run_1")))
    decision = state.decision("d_llm")
    assert decision is not None
    assert decision.status is StateStatus.REQUIRES_REVIEW
    assert len(state.decisions) == 1


def test_non_iterable_payload_values_are_coerced_to_a_single_entry() -> None:
    log = started(EventLog())
    log.append(
        "run_1", EventType.DECISION_CREATED, {"decision_id": "d1", "decision": "x", "evidence": 42}
    )
    decision = project("run_1", log.events("run_1")).decision("d1")
    assert decision is not None and decision.evidence == ["42"]
