from __future__ import annotations

from continuum.events import EventLog, EventType
from continuum.models import Origin, StateStatus
from continuum.state.extractor import (
    CompositeExtractor,
    DeterministicExtractor,
    ExtractionContext,
    LLMExtractor,
    LLMProposal,
    StateExtractor,
)
from continuum.state.semantic import project


def build_log(total: int = 10) -> EventLog:
    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "Analyze documents", "total": total})
    log.append("run_1", EventType.DECISION_CREATED, {"decision_id": "d1", "decision": "recorded"})
    log.append("run_1", EventType.WORK_COMPLETED, {})
    return log


def context(log: EventLog) -> ExtractionContext:
    return ExtractionContext(run_id="run_1", trajectory=log.events("run_1"))


# --- deterministic --------------------------------------------------------- #


def test_deterministic_extractor_matches_a_direct_projection() -> None:
    log = build_log()
    extractor = DeterministicExtractor()
    assert extractor.extract(context(log)) == project("run_1", log.events("run_1"))


def test_deterministic_extractor_reports_what_it_folded() -> None:
    log = build_log()
    extractor = DeterministicExtractor()
    extractor.extract(context(log))
    assert extractor.last_report is not None
    assert extractor.last_report.consumed == 3
    assert extractor.last_report.complete


def test_extractors_satisfy_the_protocol() -> None:
    assert isinstance(DeterministicExtractor(), StateExtractor)
    assert isinstance(LLMExtractor(lambda ctx, state: LLMProposal()), StateExtractor)


# --- the LLM layer is advisory, never authoritative ------------------------ #


def test_llm_additions_are_marked_for_review_and_tagged() -> None:
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            findings=[{"finding_id": "f_llm", "claim": "inferred", "confidence": 0.7}]
        )
    )
    state = extractor.extract(context(log))
    finding = state.finding("f_llm")
    assert finding is not None
    assert finding.status is StateStatus.REQUIRES_REVIEW
    assert finding.provenance.origin is Origin.LLM
    assert not finding.provenance.reproducible


def test_the_llm_cannot_overwrite_a_recorded_decision() -> None:
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(decisions=[{"decision_id": "d1", "decision": "hijacked"}])
    )
    state = extractor.extract(context(log))
    decision = state.decision("d1")
    assert decision is not None
    assert decision.decision == "recorded"
    assert decision.provenance.origin is Origin.DETERMINISTIC


def test_the_llm_cannot_delete_recorded_state() -> None:
    log = build_log()
    baseline = DeterministicExtractor().extract(context(log))
    state = LLMExtractor(lambda ctx, s: LLMProposal()).extract(context(log))
    assert state == baseline


def test_a_failing_llm_degrades_to_the_deterministic_state() -> None:
    log = build_log()

    def broken(ctx: ExtractionContext, state: object) -> LLMProposal:
        raise RuntimeError("provider unavailable")

    extractor = LLMExtractor(broken)
    state = extractor.extract(context(log))

    assert state == project("run_1", log.events("run_1"))
    assert isinstance(extractor.last_error, RuntimeError)


def test_a_disabled_llm_extractor_never_calls_out() -> None:
    calls: list[int] = []

    def counting(ctx: ExtractionContext, state: object) -> LLMProposal:
        calls.append(1)
        return LLMProposal()

    extractor = LLMExtractor(counting, enabled=False)
    extractor.extract(context(build_log()))
    assert calls == []


def test_llm_confidence_is_clamped_to_the_unit_interval() -> None:
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            findings=[{"finding_id": "f_hi", "claim": "c", "confidence": 4.2}]
        )
    )
    finding = extractor.extract(context(log)).finding("f_hi")
    assert finding is not None and finding.confidence == 1.0


def test_proposals_without_ids_are_dropped() -> None:
    log = build_log()
    extractor = LLMExtractor(lambda ctx, state: LLMProposal(findings=[{"claim": "anonymous"}]))
    assert (
        extractor.extract(context(log)).findings
        == project("run_1", build_log().events("run_1")).findings
    )


# --- composition ----------------------------------------------------------- #


def test_a_repeated_decision_id_within_one_proposal_is_collapsed() -> None:
    """The known-id set is seeded from the base state; it must also grow as we append."""
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            decisions=[
                {"decision_id": "X", "decision": "first"},
                {"decision_id": "X", "decision": "second"},
            ]
        )
    )
    state = extractor.extract(context(log))
    assert [d.decision_id for d in state.decisions].count("X") == 1


def test_a_repeated_finding_id_within_one_proposal_is_collapsed() -> None:
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            findings=[
                {"finding_id": "Y", "claim": "a"},
                {"finding_id": "Y", "claim": "b"},
            ]
        )
    )
    state = extractor.extract(context(log))
    assert [f.finding_id for f in state.findings].count("Y") == 1


def test_a_repeated_task_id_within_one_proposal_is_collapsed() -> None:
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            pending_work=[
                {"task_id": "T", "description": "a"},
                {"task_id": "T", "description": "b"},
            ]
        )
    )
    state = extractor.extract(context(log))
    assert [w.task_id for w in state.pending_work].count("T") == 1


def test_the_first_occurrence_wins_within_a_proposal() -> None:
    """Consistent with the base state winning over a proposal: earlier wins."""
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            decisions=[
                {"decision_id": "X", "decision": "first"},
                {"decision_id": "X", "decision": "second"},
            ]
        )
    )
    decision = extractor.extract(context(log)).decision("X")
    assert decision is not None
    assert decision.decision == "first"


def test_distinct_ids_in_one_proposal_are_all_kept() -> None:
    """The de-dup guard must not collapse genuinely different components."""
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            decisions=[
                {"decision_id": "X", "decision": "one"},
                {"decision_id": "Z", "decision": "two"},
            ],
            findings=[
                {"finding_id": "Y", "claim": "a"},
                {"finding_id": "W", "claim": "b"},
            ],
        )
    )
    state = extractor.extract(context(log))
    assert {"X", "Z"} <= {d.decision_id for d in state.decisions}
    assert {"Y", "W"} <= {f.finding_id for f in state.findings}


def test_a_proposal_repeating_a_recorded_id_still_cannot_overwrite_it() -> None:
    """d1 is recorded; repeating it twice must neither overwrite nor duplicate."""
    log = build_log()
    extractor = LLMExtractor(
        lambda ctx, state: LLMProposal(
            decisions=[
                {"decision_id": "d1", "decision": "hijacked"},
                {"decision_id": "d1", "decision": "hijacked again"},
            ]
        )
    )
    state = extractor.extract(context(log))
    assert [d.decision_id for d in state.decisions].count("d1") == 1
    decision = state.decision("d1")
    assert decision is not None
    assert decision.decision == "recorded"


def test_composite_chains_without_double_applying_events() -> None:
    log = build_log()
    composite = CompositeExtractor(
        [
            DeterministicExtractor(),
            LLMExtractor(
                lambda ctx, state: LLMProposal(
                    pending_work=[{"task_id": "t_llm", "description": "verify"}]
                )
            ),
        ]
    )
    state = composite.extract(context(log))

    assert state.progress.completed == 1  # not 2 — events folded exactly once
    assert {w.task_id for w in state.pending_work} == {"t_llm"}
    assert state.decision("d1") is not None
