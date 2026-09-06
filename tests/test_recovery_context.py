from __future__ import annotations

from continuum.checkpoint.context import (
    build_recovery_context,
    estimate_tokens,
)
from continuum.models import (
    Approval,
    ApprovalStatus,
    Decision,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    PendingWork,
    Progress,
    SemanticState,
    StateStatus,
)


def state(**overrides: object) -> SemanticState:
    base: dict[str, object] = {
        "run_id": "run_4821",
        "goal": Goal(description="Analyze 10,000 documents", version=3),
        "progress": Progress(total=10_000, completed=3421, pending=6576, failed=3),
    }
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


# --- what must always be present ------------------------------------------- #


def test_the_goal_and_verified_progress_lead() -> None:
    rendered = build_recovery_context(state()).render()
    assert "CURRENT GOAL" in rendered
    assert "Analyze 10,000 documents" in rendered
    assert "goal v3" in rendered
    assert "3421 completed" in rendered


def test_progress_cites_the_events_it_came_from() -> None:
    rendered = build_recovery_context(state(source_sequence=482)).render()
    assert "events 1..482" in rendered


def test_stale_state_is_surfaced_not_buried() -> None:
    """An agent not told what to distrust will confidently act on bad state."""
    context = build_recovery_context(
        state(
            decisions=[
                Decision(
                    decision_id="decision_12",
                    decision="Only peer-reviewed studies",
                    status=StateStatus.STALE,
                    invalidated_reason="dataset changed",
                )
            ],
            external_dependencies=[
                ExternalDependency(resource="dataset", version="v3", status=StateStatus.CONFLICTED)
            ],
        )
    )
    rendered = context.render()
    assert "STALE STATE: DO NOT RELY ON" in rendered
    assert "decision_12" in rendered
    assert "dataset changed" in rendered
    assert "dependency dataset" in rendered


def test_an_invalidated_finding_is_listed_as_stale() -> None:
    rendered = build_recovery_context(
        state(
            findings=[
                Finding(finding_id="f_bad", claim="retracted claim", status=StateStatus.INVALID)
            ]
        )
    ).render()
    assert "finding f_bad" in rendered
    assert "retracted claim" in rendered


def test_expired_approvals_count_as_stale() -> None:
    rendered = build_recovery_context(
        state(
            approvals=[
                Approval(approval_id="ap_1", subject="publish", status=ApprovalStatus.EXPIRED)
            ]
        )
    ).render()
    assert "approval ap_1" in rendered
    assert "publish" in rendered


def test_unavailable_evidence_is_flagged() -> None:
    rendered = build_recovery_context(
        state(findings=[Finding(finding_id="f1", claim="c", evidence=["paper_404"])])
    ).render()
    assert "evidence cited but unavailable: paper_404" in rendered


def test_model_specific_assumptions_require_review() -> None:
    rendered = build_recovery_context(
        state(
            model=ModelState(
                model="model-a",
                model_specific_state=[ModelSpecificState(description="assumes JSON tools")],
            )
        )
    ).render()
    assert "REQUIRES REVIEW" in rendered
    assert "assumes JSON tools" in rendered


def test_inferred_state_is_marked_for_review() -> None:
    rendered = build_recovery_context(
        state(
            findings=[
                Finding(finding_id="f_llm", claim="inferred", status=StateStatus.REQUIRES_REVIEW)
            ]
        )
    ).render()
    assert "REQUIRES REVIEW" in rendered
    assert "f_llm" in rendered


# --- what it deliberately excludes ----------------------------------------- #


def test_invalidated_decisions_are_excluded_from_the_valid_list() -> None:
    context = build_recovery_context(
        state(
            decisions=[
                Decision(decision_id="good", decision="keep this"),
                Decision(decision_id="bad", decision="drop this", status=StateStatus.INVALID),
            ]
        )
    )
    valid = next(s for s in context.sections if s.title == "VALID DECISIONS")
    assert any("good" in line for line in valid.lines)
    assert not any("bad" in line for line in valid.lines)


def test_empty_sections_are_omitted_entirely() -> None:
    rendered = build_recovery_context(state()).render()
    assert "STALE STATE" not in rendered
    assert "PENDING TASKS" not in rendered


def test_the_context_is_far_smaller_than_the_state_it_summarises() -> None:
    big = state(
        findings=[Finding(finding_id=f"f{i}", claim="x" * 200) for i in range(200)],
        evidence=[Evidence(evidence_id=f"e{i}", summary="y" * 200) for i in range(200)],
    )
    full = len(big.model_dump_json())
    context = len(build_recovery_context(big, max_items=10).render())
    assert context < full / 10, f"context {context} vs state {full}"


# --- budgeting ------------------------------------------------------------- #


def test_low_priority_sections_are_dropped_under_a_budget() -> None:
    rich = state(
        decisions=[Decision(decision_id=f"d{i}", decision="x" * 100) for i in range(20)],
        findings=[Finding(finding_id=f"f{i}", claim="y" * 100) for i in range(20)],
        pending_work=[PendingWork(task_id=f"t{i}", description="z" * 100) for i in range(20)],
        external_dependencies=[ExternalDependency(resource="dataset", version="v3")],
    )
    context = build_recovery_context(rich, token_budget=120)

    assert context.truncated
    assert context.dropped_sections
    assert "[context truncated to fit budget" in context.render()


def test_the_goal_and_stale_state_survive_even_a_tiny_budget() -> None:
    """Truncation must never remove the reason recovery is unsafe."""
    rich = state(
        decisions=[
            Decision(
                decision_id="d_stale",
                decision="x" * 300,
                status=StateStatus.STALE,
                invalidated_reason="dataset changed",
            )
        ],
        findings=[Finding(finding_id=f"f{i}", claim="y" * 300) for i in range(30)],
    )
    context = build_recovery_context(rich, token_budget=1)
    rendered = context.render()

    assert "CURRENT GOAL" in rendered
    assert "STALE STATE" in rendered
    assert "dataset changed" in rendered


def test_stale_state_survives_a_tight_budget_even_with_a_next_action() -> None:
    """Regression test for issue #16.

    When a NEXT SAFE ACTION section (priority 0) is present it sorts ahead of
    STALE STATE (priority 2). Protection used to be by sorted position, so the
    injected section pushed STALE STATE out of the protected prefix and it was
    dropped. Protection is now by section identity, so the agent is never handed
    its next action without also being told what it must distrust.
    """
    stale = state(
        decisions=[
            Decision(
                decision_id=f"d{i}",
                decision="analysis",
                status=StateStatus.INVALID,
                invalidated_reason="dataset v3 -> v4",
            )
            for i in range(3)
        ],
    )
    context = build_recovery_context(stale, token_budget=60, next_action="reconcile_action:x")
    assert "STALE STATE" in context.render()
    assert "STALE STATE: DO NOT RELY ON" not in context.dropped_sections
    titles = [section.title for section in context.sections]
    assert "STALE STATE: DO NOT RELY ON" in titles
    assert "CURRENT GOAL" in titles
    assert "VERIFIED PROGRESS" in titles


def test_the_protected_sections_are_never_dropped_across_budgets() -> None:
    """The goal, verified progress and stale state must survive truncation
    regardless of whether NEXT SAFE ACTION or ENVIRONMENT CHANGES are present."""
    stale = state(
        decisions=[
            Decision(
                decision_id=f"d{i}",
                decision="analysis",
                status=StateStatus.INVALID,
                invalidated_reason="dataset v3 -> v4",
            )
            for i in range(3)
        ],
    )
    for budget in range(1, 200):
        for next_action in (None, "reconcile_action:x"):
            context = build_recovery_context(
                stale,
                token_budget=budget,
                next_action=next_action,
                environment_changes=("data pipeline redeployed",),
            )
            assert "STALE STATE: DO NOT RELY ON" not in context.dropped_sections
            titles = [section.title for section in context.sections]
            assert "STALE STATE: DO NOT RELY ON" in titles
            assert "CURRENT GOAL" in titles
            assert "VERIFIED PROGRESS" in titles


def test_an_unbounded_context_is_never_marked_truncated() -> None:
    context = build_recovery_context(
        state(findings=[Finding(finding_id=f"f{i}", claim="c") for i in range(50)])
    )
    assert not context.truncated
    assert context.dropped_sections == ()


def test_long_lists_are_summarised_rather_than_dumped() -> None:
    context = build_recovery_context(
        state(pending_work=[PendingWork(task_id=f"t{i}", description="task") for i in range(50)]),
        max_items=5,
    )
    pending = next(s for s in context.sections if s.title == "PENDING TASKS")
    assert len(pending.lines) == 6
    assert "and 45 more pending tasks" in pending.lines[-1]


def test_findings_are_ranked_by_confidence() -> None:
    context = build_recovery_context(
        state(
            findings=[
                Finding(finding_id="low", claim="weak", confidence=0.2),
                Finding(finding_id="high", claim="strong", confidence=0.95),
            ]
        ),
        max_items=1,
    )
    findings = next(s for s in context.sections if s.title == "RELEVANT FINDINGS")
    assert "high" in findings.lines[0]


# --- extras ---------------------------------------------------------------- #


def test_environment_changes_and_next_action_are_included() -> None:
    rendered = build_recovery_context(
        state(),
        environment_changes=["dataset: v3 -> v4"],
        next_action="dataset_revalidation",
    ).render()
    assert "ENVIRONMENT CHANGES" in rendered
    assert "dataset: v3 -> v4" in rendered
    assert "NEXT SAFE ACTION" in rendered
    assert "dataset_revalidation" in rendered


def test_the_next_safe_action_ranks_with_the_goal_ahead_of_detail() -> None:
    """Goal first for orientation, permitted action immediately after."""
    context = build_recovery_context(
        state(pending_work=[PendingWork(task_id="t1", description="later")]),
        next_action="revalidate",
    )
    titles = [s.title for s in context.sections]
    assert titles[:2] == ["CURRENT GOAL", "NEXT SAFE ACTION"]
    assert titles.index("NEXT SAFE ACTION") < titles.index("PENDING TASKS")


def test_token_estimation_is_a_documented_heuristic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100
    assert build_recovery_context(state()).estimated_tokens > 0


def test_the_context_stringifies_to_its_rendering() -> None:
    context = build_recovery_context(state())
    assert str(context) == context.render()


def test_an_empty_section_renders_as_nothing() -> None:
    from continuum.checkpoint.context import ContextSection

    assert ContextSection("EMPTY", (), priority=0).render() == ""
    assert ContextSection("EMPTY", (), priority=0).estimated_tokens == 0
