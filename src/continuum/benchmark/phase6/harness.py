"""Benchmark harness for Phase 6 recovery-correctness scenarios.

A scenario is a callable that takes a :class:`ScenarioContext` and either
returns normally (asserting its own invariants) or signals failure via
``ctx.fail`` or by raising. The harness times the run, converts any failure into
a ``FAIL`` result, and never lets one broken scenario abort the whole suite.

``run_benchmark`` runs a list of ``(name, scenario)`` pairs and returns a
:class:`BenchmarkReport`. ``write_report`` persists it as JSON and Markdown so a
run is reproducible and diffable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from continuum.benchmark.phase6.metrics import (
    BenchmarkReport,
    RecoveryOutcome,
    ScenarioResult,
)

ScenarioFn = Callable[["ScenarioContext"], None]


@dataclass
class ScenarioContext:
    """Scratch space a scenario uses to report notes and observations."""

    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    _failed: bool = False

    def fail(self, message: str) -> None:
        """Mark the scenario failed and record the reason."""
        self.notes.append(f"FAIL: {message}")
        self._failed = True


def run_scenario(name: str, fn: ScenarioFn) -> ScenarioResult:
    """Run one scenario and return its timed result without propagating failures."""
    ctx = ScenarioContext()
    start = time.perf_counter()
    try:
        fn(ctx)
        outcome = RecoveryOutcome.FAIL if ctx._failed else RecoveryOutcome.PASS
    except Exception as exc:
        outcome = RecoveryOutcome.FAIL
        ctx.notes.append(f"exception: {type(exc).__name__}: {exc}")
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
    return ScenarioResult(
        scenario=name,
        outcome=outcome,
        passed=outcome is RecoveryOutcome.PASS,
        attempts=ctx.attempts,
        elapsed_ms=elapsed_ms,
        notes=ctx.notes,
        metrics=ctx.metrics,
    )


def run_benchmark(scenarios: list[tuple[str, ScenarioFn]]) -> BenchmarkReport:
    """Run named scenarios and return their aggregate benchmark report."""
    return BenchmarkReport(
        generated_at=datetime.now(),
        results=[run_scenario(name, fn) for name, fn in scenarios],
    )


def write_report(report: BenchmarkReport, path: str | Path) -> tuple[Path, Path]:
    """Write the report as ``<path>.json`` and ``<path>.md``; return both paths."""
    base = Path(path)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    summary = report.summary()
    lines = ["# Recovery benchmark report", ""]
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    lines.append("")
    lines.append(
        f"Total: {summary['total']}  Passed: {summary['passed']}  Failed: {summary['failed']}"
    )
    lines.append("")
    lines.append("| Scenario | Outcome | Attempts | Elapsed (ms) | Notes |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in report.results:
        note = " ".join(r.notes).replace("\n", " ")
        lines.append(
            f"| {r.scenario} | {r.outcome.value} | {r.attempts} | {r.elapsed_ms} | {note} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
