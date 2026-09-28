"""Recovery-correctness metrics schema for Phase 6.

A benchmark run is a list of scenario results. Each result records whether the
recovery system behaved correctly under a stress condition (a corrupted
dependency, a tampered ledger, an exhausted lease, and so on) and the cheap
observations worth keeping (attempts, elapsed time, free-form metrics).

The schema is intentionally small and pydantic-based so a report can be dumped
to JSON for external tooling, or rendered to Markdown for humans.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "BenchmarkReport",
    "RecoveryOutcome",
    "ScenarioResult",
]


class RecoveryOutcome(StrEnum):
    """How a scenario's recovery behaved.

    PASS and FAIL are the only outcomes the harness produces
    (``run_scenario`` never raises). ESCALATED marks a recovery that
    correctly handed off to a human instead of finishing, DEGRADED one that
    recovered only partially; both exist for results a scenario constructs
    by hand.
    """

    PASS = "pass"
    FAIL = "fail"
    ESCALATED = "escalated"
    DEGRADED = "degraded"


class ScenarioResult(BaseModel):
    """One scenario's observed outcome."""

    scenario: str
    outcome: RecoveryOutcome
    passed: bool
    attempts: int = 0
    elapsed_ms: float = 0.0
    notes: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class BenchmarkReport(BaseModel):
    """The aggregate of every scenario in a benchmark run."""

    generated_at: datetime
    results: list[ScenarioResult] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Count the run two ways: pass/fail totals and per-outcome counts.

        ``failed`` is everything whose ``passed`` flag is False (so an
        ESCALATED or DEGRADED result counts as failed unless it was
        constructed with ``passed=True``), while ``by_outcome`` counts each
        outcome value separately, preserving the fuller picture.
        """
        passed = sum(1 for r in self.results if r.passed)
        by_outcome: dict[str, int] = {}
        for r in self.results:
            key = r.outcome.value
            by_outcome[key] = by_outcome.get(key, 0) + 1
        return {
            "total": len(self.results),
            "passed": passed,
            "failed": len(self.results) - passed,
            "by_outcome": by_outcome,
        }
