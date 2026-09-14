"""Tool-to-action correlation identifiers (issue #785)."""

from __future__ import annotations

import pytest

from continuum.events import Event, EventType
from continuum.provenance.correlation import correlate_events, normalize_correlation_id


def test_normalize_accepts_absent() -> None:
    assert normalize_correlation_id(None) is None
    assert normalize_correlation_id("") is None


def test_normalize_accepts_bounded() -> None:
    assert normalize_correlation_id("a1-_B2") == "a1-_B2"
    assert normalize_correlation_id("x" * 64) == "x" * 64


def test_normalize_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        normalize_correlation_id("has space")
    with pytest.raises(ValueError):
        normalize_correlation_id("x" * 65)
    with pytest.raises(ValueError):
        normalize_correlation_id(123)
    with pytest.raises(ValueError):
        normalize_correlation_id("semi;colon")


def _event(sequence: int, event_type: EventType, payload: dict) -> Event:
    return Event(
        run_id="run_1",
        sequence=sequence,
        event_id=f"e{sequence:04d}",
        type=event_type,
        payload=payload,
    )


def test_correlate_matched_pair() -> None:
    events = [
        _event(1, EventType.ACTION_RECORDED, {"correlation_id": "c1"}),
        _event(2, EventType.TOOL_COMPLETED, {"correlation_id": "c1", "tool": "Write"}),
    ]
    result = correlate_events(events)
    assert list(result["chains"]) == ["c1"]
    assert [r["sequence"] for r in result["chains"]["c1"]] == [1, 2]
    assert result["unmatched_tool"] == []
    assert result["ambiguous"] == []


def test_correlate_unmatched_tool() -> None:
    events = [
        _event(1, EventType.TOOL_COMPLETED, {"tool": "Write"}),
        _event(2, EventType.TOOL_COMPLETED, {"correlation_id": "orphan", "tool": "Bash"}),
    ]
    result = correlate_events(events)
    assert result["chains"] == {}
    assert [r["sequence"] for r in result["unmatched_tool"]] == [1, 2]


def test_correlate_ambiguous_duplicate_action() -> None:
    events = [
        _event(1, EventType.ACTION_RECORDED, {"correlation_id": "dup"}),
        _event(2, EventType.ACTION_RECORDED, {"correlation_id": "dup"}),
        _event(3, EventType.TOOL_COMPLETED, {"correlation_id": "dup"}),
    ]
    result = correlate_events(events)
    assert result["ambiguous"] == ["dup"]
    assert [r["sequence"] for r in result["chains"]["dup"]] == [1, 2, 3]
