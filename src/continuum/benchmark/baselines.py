"""Comparison baselines for CONTINUUM-Bench (issue 11).

Five drivers the harness can run. The four naive baselines deliberately do
not use the recovery machinery, so the comparison isolates what semantic
checkpointing adds. The CONTINUUM driver runs through the existing adapters and
checkpoint manager. No results are fabricated here, these are the drivers only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.storage import SQLiteStorage


class Baseline(Protocol):
    """Protocol defining the interface for benchmark baseline drivers."""

    name: str

    def run(self, storage: SQLiteStorage, run_id: str) -> dict[str, str]:
        """Execute the baseline strategy for a run and return result metrics."""
        ...


@dataclass(frozen=True, slots=True)
class FullTranscriptReplay:
    """Baseline strategy that replays the full event log without compression."""

    name: str = "full_transcript_replay"

    def run(self, storage: SQLiteStorage, run_id: str) -> dict[str, str]:
        """Read and replay all events for the given run."""
        events = storage.read_events(run_id)
        return {"replayed": str(len(events)), "mode": "replay_all"}


@dataclass(frozen=True, slots=True)
class SimpleConversationSummarization:
    """Baseline strategy that condenses the event stream into a text summary."""

    name: str = "simple_conversation_summarization"

    def run(self, storage: SQLiteStorage, run_id: str) -> dict[str, str]:
        """Summarize all recorded events for the given run."""
        events = storage.read_events(run_id)
        summary = f"summarized {len(events)} events"
        return {"summary": summary, "mode": "summarize"}


@dataclass(frozen=True, slots=True)
class NaiveCheckpointing:
    """Baseline strategy that takes unguided checkpoints without recovery logic."""

    name: str = "naive_checkpointing"

    def run(self, storage: SQLiteStorage, run_id: str) -> dict[str, str]:
        """Attempt to checkpoint the run, catching and recording any failure."""
        manager = CheckpointManager(storage)
        try:
            checkpoint = manager.checkpoint(run_id)
            return {"checkpoint_id": checkpoint.checkpoint_id, "mode": "naive"}
        except Exception as exc:
            return {"error": str(exc), "mode": "naive_failed"}


@dataclass(frozen=True, slots=True)
class StructuredTaskSummary:
    """Baseline strategy that extracts structured summaries of completed work."""

    name: str = "structured_task_summary"

    def run(self, storage: SQLiteStorage, run_id: str) -> dict[str, str]:
        """Extract and count completed work events for the given run."""
        events = storage.read_events(run_id)
        tasks = [e for e in events if e.type == EventType.WORK_COMPLETED]
        return {"tasks": str(len(tasks)), "mode": "structured_summary"}


@dataclass(frozen=True, slots=True)
class ContinuumSemanticCheckpoint:
    """Baseline driver evaluating CONTINUUM semantic checkpointing."""

    name: str = "continuum_semantic_checkpoint"

    def run(self, storage: SQLiteStorage, run_id: str) -> dict[str, str]:
        """Create a semantic checkpoint for the run using CheckpointManager."""
        manager = CheckpointManager(storage)
        checkpoint = manager.checkpoint(run_id)
        return {"checkpoint_id": checkpoint.checkpoint_id, "mode": "continuum"}


BASELINES: tuple[Baseline, ...] = (  # type: ignore[assignment]
    FullTranscriptReplay(),
    SimpleConversationSummarization(),
    NaiveCheckpointing(),
    StructuredTaskSummary(),
    ContinuumSemanticCheckpoint(),
)


def baseline_by_name(name: str) -> Baseline:
    """Look up a registered baseline driver by its name string."""
    for baseline in BASELINES:
        if baseline.name == name:
            return baseline
    raise KeyError(f"unknown baseline {name!r}")
