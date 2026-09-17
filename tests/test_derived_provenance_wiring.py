"""Wiring tests for the non-amplification invariant's helpers (issue #1098).

``continuum/recovery/derived.py`` declared ``stamp_derived``, ``derived_label``
and ``is_derived_unverified`` but no producer or surface used them: both
producers inlined their own origin lookup and both renderers inlined their own
label, so the invariant the module exists to enforce (#392) was neither
displayed nor observable anywhere. These tests pin the wiring so the module
cannot silently go unused again.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from continuum.analysis import trajectory_report as trajectory_module
from continuum.analysis.trajectory_report import build_trajectory_report, render_trajectory_report
from continuum.events import Event, EventType
from continuum.models import (
    Goal,
    Origin,
    Progress,
    Provenance,
    Run,
    SemanticState,
    TrajectoryReport,
)
from continuum.recovery import summary as summary_module
from continuum.recovery.briefing_curation import curate_briefing
from continuum.recovery.derived import derived_label, is_derived_unverified
from continuum.recovery.summary import build_informed_retry
from continuum.state.semantic import project
from continuum.state.validator import validate_state
from continuum.storage import SQLiteStorage


def _run_with_agent_history() -> SQLiteStorage:
    """A run whose every event was asserted by an external agent."""
    storage = SQLiteStorage(":memory:")
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
    return storage


def test_informed_retry_block_is_stamped_by_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The block reaches its origin through stamp_derived, not an inline lookup."""
    storage = _run_with_agent_history()
    try:
        events = list(storage.read_events("r"))
        report = validate_state(project("r", events)).report

        calls: list[tuple[dict[str, Any], list[Event]]] = []
        original = summary_module.stamp_derived

        def spy(payload: dict[str, Any], source_events: list[Event]) -> dict[str, Any]:
            calls.append((payload, source_events))
            return original(payload, source_events)

        monkeypatch.setattr(summary_module, "stamp_derived", spy)
        block = build_informed_retry(storage, "r", validation_report=report)
        assert block is not None
        assert len(calls) == 1, "build_informed_retry must stamp through the shared helper"
        payload, source_events = calls[0]
        # The producer hands over an unstamped payload: the origin is added by
        # the helper, so it can never diverge from the one it computes.
        assert "derived_origin" not in payload
        assert source_events == events
        assert block["derived_origin"] == Origin.EXTERNAL_AGENT.value
    finally:
        storage.close()


def test_trajectory_report_is_stamped_by_the_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report reaches its origin through stamp_derived, not an inline lookup."""
    storage = _run_with_agent_history()
    try:
        calls: list[tuple[dict[str, Any], list[Event]]] = []
        original = trajectory_module.stamp_derived

        def spy(payload: dict[str, Any], source_events: list[Event]) -> dict[str, Any]:
            calls.append((payload, source_events))
            return original(payload, source_events)

        monkeypatch.setattr(trajectory_module, "stamp_derived", spy)
        end = storage.last_sequence("r")
        report = build_trajectory_report(storage, "r", 0, end)
        assert len(calls) == 1, "build_trajectory_report must stamp through the shared helper"
        payload, _ = calls[0]
        # The model is built unstamped (its field default) and the helper
        # supplies the origin, so it can never diverge from the events.
        assert payload["derived_origin"] == ""
        assert report.derived_origin == Origin.EXTERNAL_AGENT.value
    finally:
        storage.close()


def test_trajectory_report_render_uses_the_shared_label() -> None:
    """Rendering goes through derived_label, including the unstamped fallback."""
    base = TrajectoryReport(
        report_id="t1",
        window_start=0,
        window_end=1,
        compaction_seq=1,
        attempts=1,
        scar_rate=0.0,
        derived_origin=Origin.EXTERNAL_AGENT.value,
    )
    agent_text = "\n".join(render_trajectory_report(base))
    assert f"[{derived_label(base.model_dump(mode='json'))}]" in agent_text
    assert "unverified (derived from external_agent)" in agent_text

    local_text = "\n".join(
        render_trajectory_report(base.model_copy(update={"derived_origin": Origin.HUMAN.value}))
    )
    assert "derived from human" in local_text
    assert "unverified" not in local_text


def test_unstamped_trajectory_report_reads_as_unverified_not_mislabelled() -> None:
    """A report with no origin at all is unverified, not 'derived from ' (#1098).

    The old inline renderer printed ``derived from`` followed by an empty
    string for a report that was never stamped; the shared helper says
    'unverified' instead.
    """
    blank = TrajectoryReport(
        report_id="t1",
        window_start=0,
        window_end=1,
        compaction_seq=1,
        attempts=1,
        scar_rate=0.0,
        derived_origin="",
    )
    text = "\n".join(render_trajectory_report(blank))
    assert "unverified (derived from unverified sources)" in text
    assert is_derived_unverified(blank.model_dump(mode="json"))


def _state_with_report(report: TrajectoryReport) -> SemanticState:
    return SemanticState(
        run_id="r",
        goal=Goal(description="g", provenance=Provenance()),
        progress=Progress(total=1, completed=0, provenance=Provenance()),
        trajectory_reports=[report],
        source_sequence=1,
    )


def _decision(state: SemanticState, informed_retry: dict[str, Any] | None) -> SimpleNamespace:
    """A minimal stand-in: curation only reads these attributes."""
    return SimpleNamespace(
        state=state,
        contract=SimpleNamespace(reason=None, next_allowed_action=None),
        mode=SimpleNamespace(value="request_human"),
        safe=False,
        informed_retry=informed_retry,
        validation=None,
    )


def test_briefing_surfaces_unverified_derived_sections_as_agent_material() -> None:
    """A derived artifact of agent events is tiered as unverified, not system (#392)."""
    storage = _run_with_agent_history()
    try:
        report = TrajectoryReport(
            report_id="t1",
            window_start=0,
            window_end=1,
            compaction_seq=1,
            attempts=1,
            scar_rate=0.0,
            derived_origin=Origin.EXTERNAL_AGENT.value,
        )
        block = {"derived_origin": Origin.EXTERNAL_AGENT.value, "attempts": 1}
        curated = curate_briefing(storage, "r", _decision(_state_with_report(report), block))

        trajectory = next(s for s in curated["sections"] if "trajectory reports" in s["title"])
        assert trajectory["provenance"] == "agent"
        assert "unverified provenance" in trajectory["title"]

        retry = next(s for s in curated["sections"] if "previous attempts" in s["title"])
        assert retry["provenance"] == "agent"
        assert "unverified provenance" in retry["title"]
    finally:
        storage.close()


def test_briefing_keeps_verified_derived_sections_as_system() -> None:
    """Derived artifacts of trusted sources keep their system tier."""
    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="r", goal="g"))
        storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
        report = TrajectoryReport(
            report_id="t1",
            window_start=0,
            window_end=1,
            compaction_seq=1,
            attempts=1,
            scar_rate=0.0,
            derived_origin=Origin.DETERMINISTIC.value,
        )
        block = {"derived_origin": Origin.DETERMINISTIC.value, "attempts": 1}
        curated = curate_briefing(storage, "r", _decision(_state_with_report(report), block))

        trajectory = next(s for s in curated["sections"] if "trajectory reports" in s["title"])
        assert trajectory["provenance"] == "system"
        assert "unverified" not in trajectory["title"]

        retry = next(s for s in curated["sections"] if "previous attempts" in s["title"])
        assert retry["provenance"] == "system"
    finally:
        storage.close()
