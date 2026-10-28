"""Tests for the observe hook's payload extraction (``continuum.clienthooks``).

Covers the durable facts a PostToolUse hook extracts from the host's report:
tool name, file path, size and digest, and the optional correlation
identifier that links the observation to a ledger action attempt (#785).

The correlation branches exist on a trust boundary. The hook process runs
inside the host (Claude Code, Cursor, ...), so every value it forwards is
agent-controlled. The hook's own contract forbids it from failing the host
tool call, so a malformed identifier is dropped while the observation is
still recorded, and the strict check stays on the explicit CLI flag (review
of #1680, and the coverage request on #1875).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from continuum.clienthooks import observe_event_payload


def test_valid_correlation_id_is_carried_into_the_observation() -> None:
    """A valid top-level correlation ID is forwarded to the event payload."""
    payload = observe_event_payload(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "make"},
            "correlation_id": "attempt-1",
        }
    )
    assert payload["correlation_id"] == "attempt-1"
    assert payload["tool"] == "Bash"


def test_correlation_id_nested_in_tool_input_is_carried() -> None:
    """A correlation ID nested in ``tool_input`` is read when the top level has none.

    Hosts pass the agent's tool arguments as ``tool_input`` and do not reserve
    a top-level slot for a correlation ID, so the nested read is the path the
    feature actually takes in production.
    """
    payload = observe_event_payload(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "x.py",
                "correlation_id": "attempt-2",
            },
        }
    )
    assert payload["correlation_id"] == "attempt-2"


def test_top_level_correlation_id_wins_over_a_nested_one() -> None:
    """A top-level ID is preferred; the nested one is only a fallback."""
    payload = observe_event_payload(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "make", "correlation_id": "nested"},
            "correlation_id": "top-level",
        }
    )
    assert payload["correlation_id"] == "top-level"


@pytest.mark.parametrize("bad", [42, "", "   ", "x" * 129])
def test_malformed_correlation_id_is_dropped_not_fatal(bad: object) -> None:
    """A malformed agent-supplied ID is dropped while the observation survives.

    The hook must never fail the host tool call (its own contract), and the
    observation is durable evidence regardless of linkage quality.
    """
    payload = observe_event_payload(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "x.py", "correlation_id": bad},
        }
    )
    assert "correlation_id" not in payload
    assert payload["tool"] == "Write"


def test_observation_without_any_correlation_id_is_unchanged() -> None:
    """An observation with no identifier anywhere is recorded uncorrelated."""
    payload = observe_event_payload({"tool_name": "Write", "tool_input": {"file_path": "x.py"}})
    assert "correlation_id" not in payload
    assert payload["tool"] == "Write"


def test_non_mapping_tool_input_is_not_searched_for_a_correlation_id() -> None:
    """A ``tool_input`` that is not a mapping is skipped, not crashed on."""
    payload = observe_event_payload({"tool_name": "Bash", "tool_input": "make"})
    assert "correlation_id" not in payload
    assert payload["tool"] == "Bash"


def test_file_facts_are_still_recorded_alongside_a_correlation_id(
    tmp_path: Path,
) -> None:
    """Linking an observation does not displace the durable file facts.

    Size and digest describe what is on disk and are what recovery reads; the
    correlation ID is additive metadata on the same event.
    """
    written = tmp_path / "built.log"
    written.write_bytes(b"correlated build output" * 4)
    payload = observe_event_payload(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(written),
                "correlation_id": "attempt-3",
            },
        }
    )
    assert payload["correlation_id"] == "attempt-3"
    assert payload["path"] == str(written)
    assert payload["bytes"] == len(b"correlated build output" * 4)
    assert payload["sha256"] == hashlib.sha256(b"correlated build output" * 4).hexdigest()
