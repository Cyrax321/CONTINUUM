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


class RecoveryOutcome(StrEnum):
    """Possible correctness outcomes for a recovery scenario."""

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
        """Return total, pass/fail, and per-outcome counts for the report."""
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
