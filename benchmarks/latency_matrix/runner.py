"""The recovery latency-regression matrix (issue #766).

Measures ``RecoveryEngine.assess_scoped`` across a deterministic grid of
repository sizes and decision counts, derives the soft SLO budget from
``docs/research/latency_budget.md`` for each point, and classifies the result
twice:

- against the derived budget (``pass`` / ``informational_miss``), which is
  informational only and never fails CI, and
- against the committed baseline (``pass`` / ``regression`` / ``no_baseline``),
  which is what CI acts on.

The benchmark is observational: it introduces no runtime timeout, changes no
recovery decision, and never turns the soft SLO into a hard production limit.
"""

from __future__ import annotations

import math
import os
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.latency_matrix import baseline as baseline_mod
from benchmarks.latency_matrix.baseline import Tolerance, classify_regression
from benchmarks.latency_matrix.fixtures import RUN_ID, fixture_summary, make_fixture
from continuum.recovery import RecoveryEngine

# The measured support points in docs/research/latency_budget.md: 50/100/200
# files and 10/100 decisions. The full cross product keeps every documented
# point in the matrix, including the two the note names explicitly (100 files +
# 10 decisions, 200 files + 100 dependencies).
FILES: tuple[int, ...] = (50, 100, 200)
DECISIONS: tuple[int, ...] = (10, 100)
DEFAULT_MATRIX: tuple[tuple[int, int], ...] = tuple((f, d) for f in FILES for d in DECISIONS)

DEFAULT_SAMPLES = 9
DEFAULT_WARMUP = 1
SCHEMA_VERSION = 1

# budget_ms = 50 + 0.2 * files + 5 * decision_count, from
# docs/research/latency_budget.md. Kept here as the single derivation so the
# report and the doc cannot drift apart silently.
BASE_BUDGET_MS = 50.0
PER_FILE_MS = 0.2
PER_DECISION_MS = 5.0


def soft_budget_ms(files: int, decisions: int) -> float:
    """The soft SLO budget for one matrix point, in milliseconds."""
    return BASE_BUDGET_MS + PER_FILE_MS * files + PER_DECISION_MS * decisions


def soft_budget_status(median_ms: float, budget_ms: float) -> str:
    """``pass`` inside the soft SLO, ``informational_miss`` outside it.

    The miss is reported, never enforced: the budget is a harness guideline and
    a slow-but-valid assessment is still a valid answer (see the note's
    rejection of a hard timeout inside ``assess``).
    """
    return "pass" if median_ms <= budget_ms else "informational_miss"


@dataclass(frozen=True, slots=True)
class DimensionResult:
    """One measured point of the matrix."""

    files: int
    decisions: int
    budget_ms: float
    graph_build_ms: float
    samples: int
    min_ms: float
    median_ms: float
    mean_ms: float
    p95_ms: float
    max_ms: float
    fixture: dict[str, Any]
    soft_budget: str
    regression: str
    baseline_median_ms: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "decisions": self.decisions,
            "budget_ms": round(self.budget_ms, 3),
            "graph_build_ms": round(self.graph_build_ms, 3),
            "samples": self.samples,
            "min_ms": round(self.min_ms, 3),
            "median_ms": round(self.median_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "fixture": self.fixture,
            "soft_budget": self.soft_budget,
            "regression": self.regression,
            "baseline_median_ms": (
                round(self.baseline_median_ms, 3) if self.baseline_median_ms is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class MatrixReport:
    """A complete matrix run: envelope plus one entry per dimension point."""

    benchmark: str
    schema_version: int
    generated_at: datetime
    config: dict[str, Any]
    environment: dict[str, Any]
    tolerance: dict[str, Any]
    dimensions: list[DimensionResult] = field(default_factory=list)

    @property
    def regressions(self) -> list[DimensionResult]:
        return [d for d in self.dimensions if d.regression == baseline_mod.REGRESSION]

    @property
    def soft_budget_misses(self) -> list[DimensionResult]:
        return [d for d in self.dimensions if d.soft_budget == "informational_miss"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "config": self.config,
            "environment": self.environment,
            "tolerance": self.tolerance,
            "dimensions": [d.as_dict() for d in self.dimensions],
            "status": {
                "regressions": len(self.regressions),
                "soft_budget_misses": len(self.soft_budget_misses),
            },
        }


def environment_metadata() -> dict[str, Any]:
    """Machine and interpreter facts. No network, no dependency lookups.

    Recorded so a baseline from one runner can be read against a measurement
    from another; the tolerance policy is what makes that comparison safe.
    """
    try:
        import continuum

        # The imported package is what is actually being measured, so its own
        # __version__ wins over the installed distribution metadata, which can
        # disagree when the benchmark runs from a source checkout.
        pkg_version = getattr(continuum, "__version__", None)
        if pkg_version is None:
            from importlib.metadata import PackageNotFoundError, version

            try:
                pkg_version = version("continuum-agent")
            except PackageNotFoundError:
                pkg_version = "unknown"
    except Exception:  # pragma: no cover - importlib edge cases on odd installs
        pkg_version = "unknown"
    return {
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "continuum_version": pkg_version,
        "ci": os.environ.get("CI") == "true",
    }


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Deterministic nearest-rank percentile of already-sorted samples."""
    if not sorted_samples:
        raise ValueError("percentile of an empty sample set")
    # Nearest-rank: the ceil(q * n)-th value. Clamped at both ends because
    # ceil(0 * n) is 0 and a negative index would silently select the maximum.
    idx = max(0, min(math.ceil(q * len(sorted_samples)) - 1, len(sorted_samples) - 1))
    return sorted_samples[idx]


def measure_point(
    files: int,
    decisions: int,
    *,
    samples: int,
    warmup: int,
    tolerance: Tolerance,
    baseline_path: Path,
) -> DimensionResult:
    """Build one fixture and time its assessment ``samples`` times."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp) / "repo"
        repo_root.mkdir()
        fixture = make_fixture(repo_root, files, decisions)
        engine = RecoveryEngine(fixture.storage)

        # A read-only assessment called repeatedly is the usage the budget
        # describes, so the fixture is built once and reused across samples.
        for _ in range(warmup):
            engine.assess_scoped(
                RUN_ID,
                fixture.scope,
                current_environment=fixture.current_environment,
                source_graph=fixture.graph,
            )
        timings: list[float] = []
        for _ in range(samples):
            start = time.perf_counter()
            engine.assess_scoped(
                RUN_ID,
                fixture.scope,
                current_environment=fixture.current_environment,
                source_graph=fixture.graph,
            )
            timings.append((time.perf_counter() - start) * 1000.0)

        median_ms = statistics.median(timings)
        ordered = sorted(timings)
        budget = soft_budget_ms(files, decisions)
        baseline_median = baseline_mod.baseline_median(files, decisions, baseline_path)
        return DimensionResult(
            files=files,
            decisions=decisions,
            budget_ms=budget,
            samples=len(timings),
            min_ms=ordered[0],
            median_ms=median_ms,
            mean_ms=statistics.fmean(timings),
            p95_ms=_percentile(ordered, 0.95),
            max_ms=ordered[-1],
            graph_build_ms=fixture.graph_build_ms,
            fixture=fixture_summary(fixture),
            soft_budget=soft_budget_status(median_ms, budget),
            regression=classify_regression(median_ms, baseline_median, tolerance),
            baseline_median_ms=baseline_median,
        )


def run_matrix(
    *,
    points: Iterable[tuple[int, int]] = DEFAULT_MATRIX,
    samples: int = DEFAULT_SAMPLES,
    warmup: int = DEFAULT_WARMUP,
    tolerance: Tolerance | None = None,
    baseline_path: Path | None = None,
    generated_at: datetime | None = None,
) -> MatrixReport:
    """Run every point and return the report.

    ``generated_at`` is overridable so a test can produce a byte-stable report;
    production runs take the current time. ``baseline_path`` resolves lazily so
    a test can repoint the artifact by patching the module default.
    """
    if samples < 1:
        raise ValueError(f"samples must be >= 1 (got {samples})")
    if warmup < 0:
        raise ValueError(f"warmup must be >= 0 (got {warmup})")
    tolerance = tolerance or Tolerance.from_env()
    if baseline_path is None:
        baseline_path = baseline_mod.BASELINE_PATH
    # Materialized once so a generator input can feed both the measurements and
    # the recorded config; reading it twice would exhaust it and silently log
    # an empty grid beside the points that were actually run.
    points = tuple(points)
    dimensions = [
        measure_point(
            files,
            decisions,
            samples=samples,
            warmup=warmup,
            tolerance=tolerance,
            baseline_path=baseline_path,
        )
        for files, decisions in points
    ]
    return MatrixReport(
        benchmark=baseline_mod.BENCHMARK_NAME,
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(UTC),
        config={
            "samples": samples,
            "warmup": warmup,
            "budget_formula": f"{BASE_BUDGET_MS:g} + {PER_FILE_MS:g} * files + {PER_DECISION_MS:g} * decisions",
            "matrix_points": [[f, d] for f, d in points],
        },
        environment=environment_metadata(),
        tolerance=tolerance.as_dict(),
        dimensions=dimensions,
    )
