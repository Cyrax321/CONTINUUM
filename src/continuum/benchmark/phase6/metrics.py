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
    """What a scenario observed of the recovery system's behaviour.

    ``PASS`` and ``FAIL`` are the harness-assigned verdicts (normal return
    vs ``ctx.fail``/exception); only those two are ever assigned today.
    ``ESCALATED`` and ``DEGRADED`` are reserved for scenarios that assert a
    *correct-but-not-clean* result - recovery that correctly demanded a
    human, or correctly resumed with a visibly reduced guarantee - which
    ``PASS``/``FAIL`` cannot express; no current scenario reports them.
    ``passed`` tracks the harness verdict, so a future scenario reporting
    ``ESCALATED`` decides for itself whether its invariant held.
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
        """Count totals and per-outcome tallies over the report's results.

        Returns ``total``, ``passed`` (count of ``passed=True`` results),
        ``failed`` (the remainder - anything not passed, including
        escalated/degraded scenarios whose own invariant failed) and
        ``by_outcome``, the raw tally keyed by outcome value. The shape is
        what ``write_report`` renders and what a CI threshold reads.
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
