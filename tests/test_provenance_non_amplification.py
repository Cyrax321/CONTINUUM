"""Non-amplification invariant for derived artifacts (issue #392)."""

from __future__ import annotations

from continuum.events import Event, EventType
from continuum.models import Finding, Goal, Origin, Progress, Provenance, SemanticState, StateStatus
from continuum.provenance_map import derived_origin, derived_provenance_for_events, min_canonical
from continuum.recovery.derived import derived_label, is_derived_unverified, stamp_derived
from continuum.recovery.summary import render_informed_retry
from continuum.state.semantic import project
from continuum.state.validator import validate_state
from continuum.storage import SQLiteStorage


def test_lesson_sourced_only_from_external_agent_is_unverified() -> None:
    events = [
        Event(
            run_id="r",
            sequence=1,
            type=EventType.RUN_STARTED,
            payload={},
            source=Origin.EXTERNAL_AGENT,
        ),
        Event(
            run_id="r",
            sequence=2,
            type=EventType.TASK_UPDATED,
            payload={},
            source=Origin.EXTERNAL_AGENT,
        ),
    ]
    payload = {"falsified": "test", "attempt_id": "a1"}
    stamped = stamp_derived(payload, events)
    assert stamped["derived_origin"] == Origin.EXTERNAL_AGENT.value
    assert is_derived_unverified(stamped)
    assert "unverified" in derived_label(stamped)
    finding = Finding(
        finding_id="lesson_1",
        claim="lesson",
        provenance=Provenance(origin=Origin(stamped["derived_origin"])),
    )
    state2 = SemanticState(
        run_id="r",
        goal=Goal(description="g", provenance=Provenance(origin=Origin.DETERMINISTIC)),
        progress=Progress(
            total=10, completed=1, provenance=Provenance(origin=Origin.DETERMINISTIC)
        ),
        findings=[finding],
        source_sequence=1,
    )
    outcome = validate_state(state2)
    assert any(
        e.component.value == "finding"
        and e.component_id == "lesson_1"
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )
    assert not outcome.safe


def test_mixing_in_one_trusted_source_does_not_upgrade() -> None:
    events = [
        Event(
            run_id="r",
            sequence=1,
            type=EventType.RUN_STARTED,
            payload={},
            source=Origin.DETERMINISTIC,
        ),
        Event(
            run_id="r",
            sequence=2,
            type=EventType.WORK_COMPLETED,
            payload={},
            source=Origin.DETERMINISTIC,
        ),
        Event(
            run_id="r",
            sequence=3,
            type=EventType.TASK_UPDATED,
            payload={},
            source=Origin.EXTERNAL_AGENT,
        ),
    ]
    origin = derived_origin([e.source for e in events])  # type: ignore[arg-type]
    assert origin is Origin.EXTERNAL_AGENT
    payload = stamp_derived({}, events)
    assert payload["derived_origin"] == Origin.EXTERNAL_AGENT.value
    assert is_derived_unverified(payload)
    events2 = [
        Event(run_id="r", sequence=1, type=EventType.RUN_STARTED, payload={}, source=Origin.HUMAN),
        Event(
            run_id="r",
            sequence=2,
            type=EventType.TASK_UPDATED,
            payload={},
            source=Origin.EXTERNAL_AGENT,
        ),
    ]
    assert derived_provenance_for_events(events2) is Origin.EXTERNAL_AGENT


def test_existing_artifact_without_new_field_degrades_to_unverified() -> None:
    old_block: dict[str, object] = {"attempts": 1, "avoid": []}
    assert is_derived_unverified(old_block)  # type: ignore[arg-type]
    label = derived_label(old_block)  # type: ignore[arg-type]
    assert "unverified" in label
    assert min_canonical([]).value == "agent_asserted"
    assert derived_origin([]) is Origin.EXTERNAL_AGENT
    assert derived_provenance_for_events([]) is Origin.EXTERNAL_AGENT


def test_falsifiable_mcp_progress_lesson_stays_request_human(tmp_path=None) -> None:
    from continuum.models import Run
    from continuum.recovery import RecoveryEngine

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event(
        "r", EventType.RUN_STARTED, {"goal": "g", "total": 10}, source=Origin.EXTERNAL_AGENT
    )
    storage.append_event(
        "r", EventType.TASK_UPDATED, {"completed": 9, "total": 10}, source=Origin.EXTERNAL_AGENT
    )
    events = storage.read_events("r")
    lesson_payload = {"attempt_id": "a1", "falsified": "progress 9/10 was fabricated"}
    stamped = stamp_derived(lesson_payload, events)
    assert stamped["derived_origin"] == Origin.EXTERNAL_AGENT.value
    lesson_finding = Finding(
        finding_id="lesson_falsified",
        claim=stamped["falsified"],
        provenance=Provenance(origin=Origin(stamped["derived_origin"])),
    )
    state = project("r", events)
    state_with_lesson = state.model_copy(update={"findings": [*state.findings, lesson_finding]})
    outcome = validate_state(state_with_lesson)
    assert any(
        e.component_id == "lesson_falsified" and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )
    engine = RecoveryEngine(storage)
    decision = engine.assess("r")
    assert decision.mode.value == "request_human"
    assert not decision.safe
    storage.close()


def test_informed_retry_block_is_stamped_and_labelled() -> None:
    storage = SQLiteStorage(":memory:")
    from continuum.models import Run

    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event(
        "r", EventType.RUN_STARTED, {"goal": "g", "total": 10}, source=Origin.EXTERNAL_AGENT
    )
    storage.append_event(
        "r",
        EventType.RECOVERY_STARTED,
        {"mode": "request_human", "plan": []},
        source=Origin.DETERMINISTIC,
    )
    from continuum.recovery import RecoveryEngine

    engine = RecoveryEngine(storage)
    decision = engine.assess("r")
    assert decision.informed_retry is not None
    assert "derived_origin" in decision.informed_retry
    assert decision.informed_retry["derived_origin"] == Origin.EXTERNAL_AGENT.value
    rendered = render_informed_retry(decision.informed_retry)
    assert any("provenance" in line and "unverified" in line for line in rendered)
    storage.close()


def test_trusted_only_sources_yield_verified_derived() -> None:
    events = [
        Event(
            run_id="r",
            sequence=1,
            type=EventType.RUN_STARTED,
            payload={},
            source=Origin.DETERMINISTIC,
        ),
        Event(
            run_id="r", sequence=2, type=EventType.WORK_COMPLETED, payload={}, source=Origin.HUMAN
        ),
    ]
    origin = derived_provenance_for_events(events)
    assert origin is Origin.DETERMINISTIC
    payload = stamp_derived({}, events)
    assert not is_derived_unverified(payload)
    assert "derived from deterministic" in derived_label(payload)
