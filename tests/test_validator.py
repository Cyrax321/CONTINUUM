from __future__ import annotations

from datetime import datetime, timedelta

from continuum.environment import CallableProvider, StaticProvider, capture
from continuum.models import (
    Approval,
    ApprovalStatus,
    Component,
    Decision,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    Progress,
    SemanticState,
    StateStatus,
    utcnow,
)
from continuum.state.validator import validate_state


def state(**overrides: object) -> SemanticState:
    base: dict[str, object] = {
        "run_id": "run_4821",
        "goal": Goal(description="Analyze 10,000 documents", version=3),
        "progress": Progress(total=10_000, completed=3421, pending=6576, failed=3),
        "source_sequence": 4000,
    }
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


def status_for(outcome: object, component: Component, cid: str | None = None) -> StateStatus:
    entry = next(
        e
        for e in outcome.report.statuses  # type: ignore[attr-defined]
        if e.component is component and (cid is None or e.component_id == cid)
    )
    return entry.status


# --- the happy path -------------------------------------------------------- #


def test_an_unchanged_environment_is_safe_to_resume() -> None:
    env = capture("run_4821", StaticProvider(dataset="v3"))
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")]),
        checkpoint_environment=env,
        current_environment=capture("run_4821", StaticProvider(dataset="v3")),
    )
    assert outcome.safe
    assert not outcome.downgraded
    assert "verified" in outcome.report.reason


def test_the_goal_and_progress_are_always_reported() -> None:
    outcome = validate_state(state())
    assert status_for(outcome, Component.GOAL) is StateStatus.VALID
    assert status_for(outcome, Component.PROGRESS) is StateStatus.VALID


# --- the headline case: a dataset moved ------------------------------------ #


def test_omitting_the_current_environment_names_the_missing_side() -> None:
    """The diagnostic pointed at the wrong side of the comparison (issue #307).

    "no environment snapshot to compare against" says the stored snapshot is
    missing. It is not: the checkpoint recorded one, and supplying `env` proves
    it. What is absent is the caller's current observation, and a caller told the
    wrong thing is missing cannot act on the message.

    The UNKNOWN status is deliberate and stays: never validated is not the same
    as validated clean.
    """
    pinned = state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")])
    outcome = validate_state(
        pinned,
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
    )
    detail = next(
        e.detail
        for e in outcome.report.statuses
        if e.component is Component.EXTERNAL_DEPENDENCY and e.component_id == "dataset"
    )
    assert "no current version supplied" in detail
    assert 'env={"dataset": "<version>"}' in detail
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "dataset") is StateStatus.UNKNOWN


def test_a_dependency_absent_from_both_snapshots_blames_the_snapshot() -> None:
    """The caller did observe; this resource was in neither side of the diff.

    A resource the environment provider never reported is unverifiable, and the
    message must not claim the caller supplied nothing when it plainly did.
    """
    outcome = validate_state(
        state(
            external_dependencies=[
                ExternalDependency(resource="dataset", version="v3"),
                ExternalDependency(resource="registry", version="r1"),
            ]
        ),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "registry") is StateStatus.UNKNOWN
    detail = next(
        e.detail
        for e in outcome.report.statuses
        if e.component is Component.EXTERNAL_DEPENDENCY and e.component_id == "registry"
    )
    assert "not present in the current environment snapshot" in detail


def test_a_changed_dependency_conflicts_and_blocks_resume() -> None:
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")]),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    assert not outcome.safe
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "dataset") is StateStatus.CONFLICTED
    assert "v3 -> v4" in outcome.render()


def test_staleness_propagates_from_dependency_to_decision() -> None:
    """The point of the module: a moved dataset invalidates the reasoning built on it."""
    original = state(
        external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
        evidence=[Evidence(evidence_id="paper_128", source="dataset")],
        findings=[Finding(finding_id="finding_17", claim="X holds", evidence=["paper_128"])],
        decisions=[Decision(decision_id="d_12", decision="Publish X", evidence=["finding_17"])],
    )
    outcome = validate_state(
        original,
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )

    assert not outcome.safe
    revised = outcome.state
    assert revised.evidence[0].status is StateStatus.STALE
    assert revised.findings[0].status is StateStatus.STALE
    assert revised.decisions[0].status is StateStatus.STALE
    assert "finding_17" in (revised.decisions[0].invalidated_reason or "")

    # the original state object is untouched
    assert original.decisions[0].status is StateStatus.VALID


def test_staleness_cascades_along_finding_to_finding_citations() -> None:
    """A finding citing another finding is supported by it (issue #739).

    Taint used to stop at the first finding: a finding resting entirely on a
    now-stale finding, and the decision built on it, kept VALID in the revised
    state, so a repair plan driven by that state skipped the downstream work.
    """
    outcome = validate_state(
        state(
            external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
            evidence=[Evidence(evidence_id="paper_128", source="dataset")],
            findings=[
                Finding(finding_id="f_raw", claim="rows shifted", evidence=["paper_128"]),
                Finding(finding_id="f_derived", claim="X holds", evidence=["f_raw"]),
            ],
            decisions=[Decision(decision_id="d_1", decision="Publish X", evidence=["f_derived"])],
        ),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    revised = outcome.state
    assert revised.evidence[0].status is StateStatus.STALE
    assert revised.findings[0].status is StateStatus.STALE
    assert revised.findings[1].status is StateStatus.STALE
    assert revised.decisions[0].status is StateStatus.STALE
    assert not outcome.safe


def test_finding_citation_cascade_is_independent_of_list_order() -> None:
    """A forward citation (a finding citing one listed after it) taints the
    same set, so the cascade must not depend on list order (issue #739)."""
    outcome = validate_state(
        state(
            external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
            evidence=[Evidence(evidence_id="paper_128", source="dataset")],
            findings=[
                # Listed before the raw finding it rests on.
                Finding(finding_id="f_derived", claim="X holds", evidence=["f_raw"]),
                Finding(finding_id="f_raw", claim="rows shifted", evidence=["paper_128"]),
            ],
        ),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    revised = {f.finding_id: f.status for f in outcome.state.findings}
    assert revised["f_raw"] is StateStatus.STALE
    assert revised["f_derived"] is StateStatus.STALE


def test_propagation_spares_state_that_did_not_depend_on_the_change() -> None:
    outcome = validate_state(
        state(
            external_dependencies=[
                ExternalDependency(resource="dataset", version="v3"),
                ExternalDependency(resource="registry", version="r1"),
            ],
            evidence=[
                Evidence(evidence_id="tainted", source="dataset"),
                Evidence(evidence_id="clean", source="registry"),
            ],
            findings=[
                Finding(finding_id="f_bad", claim="from dataset", evidence=["tainted"]),
                Finding(finding_id="f_ok", claim="from registry", evidence=["clean"]),
            ],
        ),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3", registry="r1")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4", registry="r1")),
    )
    revised = {f.finding_id: f.status for f in outcome.state.findings}
    assert revised["f_bad"] is StateStatus.STALE
    assert revised["f_ok"] is StateStatus.VALID
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "registry") is StateStatus.VALID


def test_a_decision_resting_on_untouched_support_stays_valid() -> None:
    """Propagation must not sweep up reasoning that never depended on the change."""
    outcome = validate_state(
        state(
            external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
            evidence=[
                Evidence(evidence_id="tainted", source="dataset"),
                Evidence(evidence_id="independent", source="handbook"),
            ],
            decisions=[
                Decision(decision_id="d_hit", decision="from dataset", evidence=["tainted"]),
                Decision(decision_id="d_safe", decision="from handbook", evidence=["independent"]),
            ],
        ),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    revised = {d.decision_id: d.status for d in outcome.state.decisions}
    assert revised["d_hit"] is StateStatus.STALE
    assert revised["d_safe"] is StateStatus.VALID


def test_a_removed_resource_invalidates_rather_than_conflicts() -> None:
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")]),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider()),
    )
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "dataset") is StateStatus.INVALID


def test_already_invalid_state_is_not_relabelled_by_propagation() -> None:
    outcome = validate_state(
        state(
            external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
            evidence=[Evidence(evidence_id="e1", source="dataset")],
            findings=[
                Finding(
                    finding_id="f1",
                    claim="c",
                    evidence=["e1"],
                    status=StateStatus.INVALID,
                )
            ],
        ),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    assert outcome.state.findings[0].status is StateStatus.INVALID


# --- uncertainty degrades, it does not resolve ----------------------------- #


def test_an_unverifiable_resource_becomes_unknown_and_blocks() -> None:
    def down() -> str:
        raise ConnectionError("api unreachable")

    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="api", version="live")]),
        checkpoint_environment=capture("run_4821", StaticProvider(api="live")),
        current_environment=capture("run_4821", CallableProvider({"api": down})),
    )
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "api") is StateStatus.UNKNOWN
    assert not outcome.safe


def test_unknown_can_be_tolerated_explicitly() -> None:
    def down() -> str:
        raise ConnectionError("api unreachable")

    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="api", version="live")]),
        checkpoint_environment=capture("run_4821", StaticProvider(api="live")),
        current_environment=capture("run_4821", CallableProvider({"api": down})),
        strict_unknown=False,
    )
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "api") is StateStatus.UNKNOWN
    assert outcome.safe  # opted in, and it is visible in the report


def test_a_dependency_absent_from_the_snapshot_is_unknown() -> None:
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")]),
        checkpoint_environment=capture("run_4821", StaticProvider(other="x")),
        current_environment=capture("run_4821", StaticProvider(other="x")),
    )
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "dataset") is StateStatus.UNKNOWN


def test_no_environment_at_all_leaves_dependencies_unverified() -> None:
    """Never validated is not the same as validated clean."""
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")])
    )
    assert status_for(outcome, Component.EXTERNAL_DEPENDENCY, "dataset") is StateStatus.UNKNOWN
    assert not outcome.safe


def test_a_state_with_no_dependencies_needs_no_environment() -> None:
    assert validate_state(state()).safe


# --- approvals ------------------------------------------------------------- #


def test_an_expired_approval_is_caught_by_its_timestamp() -> None:
    outcome = validate_state(
        state(
            approvals=[
                Approval(
                    approval_id="ap_1",
                    subject="publish",
                    status=ApprovalStatus.GRANTED,
                    expires_at=utcnow() - timedelta(minutes=1),
                )
            ]
        )
    )
    assert status_for(outcome, Component.APPROVAL, "ap_1") is StateStatus.EXPIRED
    assert not outcome.safe


def test_a_naive_expires_at_grades_instead_of_raising() -> None:
    """Issue #704: a naive expires_at (persisted before the fold normalized
    offsets, or constructed directly) must grade as EXPIRED, not raise
    TypeError comparing naive against the tz-aware utcnow()."""
    outcome = validate_state(
        state(
            approvals=[
                Approval(
                    approval_id="ap_1",
                    subject="publish",
                    status=ApprovalStatus.GRANTED,
                    expires_at=datetime(2020, 1, 1),  # naive, and in the past
                )
            ]
        )
    )
    assert status_for(outcome, Component.APPROVAL, "ap_1") is StateStatus.EXPIRED


def test_a_live_approval_passes() -> None:
    outcome = validate_state(
        state(
            approvals=[
                Approval(
                    approval_id="ap_1",
                    subject="publish",
                    status=ApprovalStatus.GRANTED,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            ]
        )
    )
    assert status_for(outcome, Component.APPROVAL, "ap_1") is StateStatus.VALID
    assert outcome.safe


def test_revoked_and_pending_approvals_are_distinguished() -> None:
    outcome = validate_state(
        state(
            approvals=[
                Approval(approval_id="revoked", subject="s", status=ApprovalStatus.REVOKED),
                Approval(approval_id="pending", subject="s", status=ApprovalStatus.PENDING),
                Approval(approval_id="expired", subject="s", status=ApprovalStatus.EXPIRED),
            ]
        )
    )
    assert status_for(outcome, Component.APPROVAL, "revoked") is StateStatus.INVALID
    assert status_for(outcome, Component.APPROVAL, "pending") is StateStatus.REQUIRES_REVIEW
    assert status_for(outcome, Component.APPROVAL, "expired") is StateStatus.EXPIRED


# --- model switching is never assumed safe --------------------------------- #


def test_switching_models_flags_model_specific_state() -> None:
    outcome = validate_state(
        state(
            model=ModelState(
                model="model-a",
                model_specific_state=[ModelSpecificState(description="assumes JSON tools")],
            )
        ),
        expected_model="model-b",
    )
    assert status_for(outcome, Component.MODEL) is StateStatus.STALE
    assert not outcome.safe
    assert "model-b" in outcome.render()


def test_switching_models_without_assumptions_only_asks_for_review() -> None:
    outcome = validate_state(state(model=ModelState(model="model-a")), expected_model="model-b")
    assert status_for(outcome, Component.MODEL) is StateStatus.REQUIRES_REVIEW


def test_the_same_model_is_not_flagged() -> None:
    outcome = validate_state(
        state(
            model=ModelState(
                model="model-a",
                model_specific_state=[ModelSpecificState(description="assumes JSON tools")],
            )
        ),
        expected_model="model-a",
    )
    assert status_for(outcome, Component.MODEL) is StateStatus.VALID
    assert outcome.safe


def test_no_expected_model_means_no_model_check() -> None:
    outcome = validate_state(state(model=ModelState(model="model-a")))
    assert not any(e.component is Component.MODEL for e in outcome.report.statuses)


def test_expected_model_against_an_unrecorded_model_reports_the_gap() -> None:
    """A safety check that silently does nothing is worse than none (issue #308).

    No MCP tool records a model, so `state.model` is None for every MCP-created
    run. Returning silently made the answer identical to a clean comparison, so
    a caller passing expected_model believed it had ruled out drift when nothing
    had been compared.
    """
    outcome = validate_state(state(), expected_model="claude-3-opus-20240229")
    assert status_for(outcome, Component.MODEL) is StateStatus.UNKNOWN
    detail = next(e.detail for e in outcome.report.statuses if e.component is Component.MODEL)
    assert "no model recorded" in detail
    assert "claude-3-opus-20240229" in detail
    # The caller explicitly asked for a check that cannot be performed, so under
    # the default strict_unknown this is not a clean resume.
    assert not outcome.safe


def test_no_expected_model_with_assumptions_is_unknown_not_valid() -> None:
    # A validate/resume run without --model has no idea which model will
    # actually resume, so it cannot verify recorded model-specific
    # assumptions. It must not report them VALID (issue #49).
    outcome = validate_state(
        state(
            model=ModelState(
                model="model-a",
                model_specific_state=[ModelSpecificState(description="assumes JSON tools")],
            )
        ),
        expected_model=None,
    )
    assert status_for(outcome, Component.MODEL) is StateStatus.UNKNOWN
    assert not outcome.safe


def test_unrecorded_model_with_assumptions_is_unknown_not_valid() -> None:
    # If the state itself doesn't record which model produced it, assumptions
    # can't be verified either, even when expected_model is known.
    outcome = validate_state(
        state(
            model=ModelState(
                model=None,
                model_specific_state=[ModelSpecificState(description="assumes JSON tools")],
            )
        ),
        expected_model="model-b",
    )
    assert status_for(outcome, Component.MODEL) is StateStatus.UNKNOWN
    assert not outcome.safe


# --- internal coherence ---------------------------------------------------- #


def test_incoherent_progress_cannot_reach_the_validator() -> None:
    """The counter invariant is enforced by the model, on construction and on
    deserialization, so no corrupted row can smuggle it past validation."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="exceeds total"):
        Progress(total=10, completed=9, pending=9)
    with pytest.raises(ValidationError, match="exceeds total"):
        Progress.model_validate_json('{"total":10,"completed":9,"pending":9,"failed":0}')


def test_progress_without_source_events_is_unknown() -> None:
    outcome = validate_state(state(source_sequence=0, progress=Progress(completed=100)))
    assert status_for(outcome, Component.PROGRESS) is StateStatus.UNKNOWN


def test_self_certified_progress_stays_requires_review_without_source() -> None:
    """Regression for issue #48: a self-certified progress with no source events
    must keep REQUIRES_REVIEW, not be downgraded to a tolerable UNKNOWN by the
    source_sequence == 0 branch. REQUIRES_REVIEW is never excepted by
    strict_unknown, so it must always block the resume."""
    from continuum.models import Origin, Provenance

    outcome = validate_state(
        state(
            source_sequence=0,
            progress=Progress(
                total=10_000,
                completed=3421,
                pending=6576,
                failed=3,
                provenance=Provenance(origin=Origin.EXTERNAL_AGENT),
            ),
        )
    )
    assert status_for(outcome, Component.PROGRESS) is StateStatus.REQUIRES_REVIEW
    # Even with --tolerate-unknown the self-report must still block.
    tolerated = validate_state(
        state(
            source_sequence=0,
            progress=Progress(
                total=10_000,
                completed=3421,
                pending=6576,
                failed=3,
                provenance=Provenance(origin=Origin.EXTERNAL_AGENT),
            ),
        ),
        strict_unknown=False,
    )
    assert status_for(tolerated, Component.PROGRESS) is StateStatus.REQUIRES_REVIEW
    assert not tolerated.safe


def test_evidence_cited_but_missing_is_reported() -> None:
    outcome = validate_state(
        state(findings=[Finding(finding_id="f1", claim="c", evidence=["paper_404"])])
    )
    assert status_for(outcome, Component.EVIDENCE) is StateStatus.UNKNOWN
    assert "paper_404" in outcome.render()


# --- reporting -------------------------------------------------------------- #


def test_the_report_names_what_blocked_the_resume() -> None:
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")]),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
    )
    assert "external_dependency dataset is conflicted" in outcome.report.reason


def test_the_rendering_is_human_readable() -> None:
    outcome = validate_state(
        state(external_dependencies=[ExternalDependency(resource="dataset", version="v3")]),
        checkpoint_environment=capture("run_4821", StaticProvider(dataset="v3")),
        current_environment=capture("run_4821", StaticProvider(dataset="v4")),
        checkpoint_version=17,
    )
    rendered = outcome.render()
    assert "Run: run_4821" in rendered
    assert "Checkpoint: v17" in rendered
    assert "[ok] goal" in rendered
    assert "[!!] external dependency dataset" in rendered
    assert "Safe to resume: no" in rendered


def test_validation_records_when_it_happened() -> None:
    outcome = validate_state(state())
    assert outcome.report.validated_at <= utcnow()
    assert outcome.report.run_id == "run_4821"
