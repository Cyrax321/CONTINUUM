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
        """Mark the scenario failed with ``message``; execution continues.

        The non-raising failure channel: appends a ``FAIL:`` note and flags
        the context, so a scenario can assert several independent invariants
        in one run and report every violation instead of dying on the first.
        The harness converts the flag into a ``FAIL`` result after the
        scenario returns; a raised exception marks failure too, but stops
        the scenario at the raise.
        """
        self.notes.append(f"FAIL: {message}")
        self._failed = True


def run_scenario(name: str, fn: ScenarioFn) -> ScenarioResult:
    """Run one scenario to a result, never raising past the caller.

    Hands ``fn`` a fresh :class:`ScenarioContext` and times the run.
    Failure is whichever signal fires first: a raised exception (captured
    as a note, converted to ``FAIL``) or ``ctx.fail`` leaving the context
    flagged. A normal return with no flag is a ``PASS``. The result always
    carries the scenario's notes, ``attempts``, free-form metrics and
    elapsed milliseconds, so one broken scenario yields data, not an
    aborted suite.
    """
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
    """Run every scenario in order and return the aggregate report.

    Each ``(name, scenario)`` pair becomes one :class:`ScenarioResult`
    via :func:`run_scenario`, so a failing scenario never aborts the rest
    - the point of a correctness suite is the full picture, including
    which scenarios still pass after a change. The report's
    ``generated_at`` is wall-clock, so reports are comparable but not
    byte-reproducible; compare ``summary()`` figures, not timestamps.
    """
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
