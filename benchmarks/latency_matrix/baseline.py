"""Checked-in baseline and the regression tolerance policy (issue #766).

The baseline is a committed JSON artifact, not a number computed at runtime. A
normal matrix run never writes it: the only way the expected values change is
``python -m benchmarks.latency_matrix --update-baseline``, which rewrites the
file so the diff is read in review. That is what "baseline updates require an
explicit, reviewable artifact" means in practice.

Tolerance policy
----------------

A point is a regression only when *both* of these hold:

1. ``median_ms > factor * baseline_median_ms`` (default factor 3.0)
2. ``median_ms - baseline_median_ms > absolute_floor_ms`` (default 25.0 ms)

Neither alone is enough. The factor is relative and so travels with the runner:
a 3x slowdown on a fast machine and a 3x slowdown on a slow one are the same
signal. But a ratio alone is jitter-happy at the small end, where a baseline of
half a millisecond triples into a "regression" of a few milliseconds on a
loaded CI worker. The absolute floor filters that out. Requiring both is what
keeps normal variability from failing CI; the AND, not either threshold, is the
conservative part.

A soft-budget miss is a different thing entirely and is never a CI failure: see
``runner.soft_budget_status``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"
BASELINE_SCHEMA_VERSION = 1
BENCHMARK_NAME = "latency-matrix"

DEFAULT_REGRESSION_FACTOR = 3.0
DEFAULT_ABSOLUTE_FLOOR_MS = 25.0

_REGRESSION_FACTOR_ENV = "CONTINUUM_LATENCY_REGRESSION_FACTOR"
_ABSOLUTE_FLOOR_ENV = "CONTINUUM_LATENCY_ABSOLUTE_FLOOR_MS"

# Regressions are relative to the baseline, so the file that recorded the
# tolerance policy is part of the artifact and travels with the points.
NO_BASELINE = "no_baseline"
PASS = "pass"
REGRESSION = "regression"


@dataclass(frozen=True, slots=True)
class Tolerance:
    """The thresholds used to classify one point against its baseline."""

    regression_factor: float
    absolute_floor_ms: float

    @classmethod
    def from_env(cls) -> Tolerance:
        """Load thresholds, falling back to documented defaults.

        Overridable on the command line of a debugging run, so a suspected
        machine-mismatch can be widened without editing code.
        """
        factor = _env_float(_REGRESSION_FACTOR_ENV, DEFAULT_REGRESSION_FACTOR)
        floor = _env_float(_ABSOLUTE_FLOOR_ENV, DEFAULT_ABSOLUTE_FLOOR_MS)
        # NaN or infinity would not raise here but silently disables the gate:
        # every comparison against NaN is False, and no median can exceed
        # infinity, so no point could ever regress.
        for name, value in ((_REGRESSION_FACTOR_ENV, factor), (_ABSOLUTE_FLOOR_ENV, floor)):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number, got {value}")
        if factor <= 1.0:
            raise ValueError(
                f"{_REGRESSION_FACTOR_ENV} must be > 1.0 (got {factor}); a factor at or "
                "below 1.0 would flag every run that is not faster than the baseline."
            )
        if floor < 0.0:
            raise ValueError(f"{_ABSOLUTE_FLOOR_ENV} must be >= 0.0 (got {floor})")
        return cls(regression_factor=factor, absolute_floor_ms=floor)

    def as_dict(self) -> dict[str, Any]:
        return {
            "regression_factor": self.regression_factor,
            "absolute_floor_ms": self.absolute_floor_ms,
            "policy": (
                "regression iff median > factor * baseline_median AND "
                "median - baseline_median > absolute_floor_ms; soft-budget misses "
                "are informational and never fail CI"
            ),
        }


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def classify_regression(
    median_ms: float,
    baseline_median_ms: float | None,
    tolerance: Tolerance,
) -> str:
    """Classify one measured median against its baseline entry.

    Returns ``no_baseline`` when the point has nothing to compare against,
    ``regression`` when it breaches both thresholds, ``pass`` otherwise.
    """
    if baseline_median_ms is None or baseline_median_ms <= 0.0:
        return NO_BASELINE
    if median_ms <= baseline_median_ms:
        return PASS
    excess = median_ms - baseline_median_ms
    if (
        median_ms > tolerance.regression_factor * baseline_median_ms
        and excess > tolerance.absolute_floor_ms
    ):
        return REGRESSION
    return PASS


def _well_formed(data: Any) -> bool:
    """Whether ``data`` has the shape ``baseline_median`` is about to assume.

    Every field read without a guard is checked here, so a malformed artifact
    degrades to ``no_baseline`` instead of raising partway through a lookup.
    """
    if not isinstance(data, dict):
        return False
    points = data.get("points")
    if not isinstance(points, list):
        return False
    for point in points:
        if not isinstance(point, dict):
            return False
        median = point.get("median_ms")
        if not isinstance(median, (int, float)) or isinstance(median, bool):
            return False
        if not isinstance(point.get("files"), int) or isinstance(point.get("files"), bool):
            return False
        if not isinstance(point.get("decisions"), int) or isinstance(point.get("decisions"), bool):
            return False
    return True


def load_baseline(path: Path | None = None) -> dict[str, Any] | None:
    """Read the committed baseline, or ``None`` when it is absent or unreadable.

    A missing or malformed baseline degrades to ``no_baseline`` rather than
    raising: the matrix is observational, so an unreadable baseline must not
    break the suite (the same reason ``benchmarks/run.py`` guards its suites).
    """
    path = path if path is not None else BASELINE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("benchmark") != BENCHMARK_NAME:
        return None
    if not _well_formed(data):
        return None
    return data


def baseline_median(
    files: int,
    decisions: int,
    path: Path | None = None,
) -> float | None:
    """The committed median for one matrix point, ``None`` if unrecorded."""
    data = load_baseline(path)
    if data is None:
        return None
    for point in data["points"]:
        if point["files"] == files and point["decisions"] == decisions:
            return float(point["median_ms"])
    return None


def write_baseline(report: Any, path: Path | None = None) -> Path:
    """Freeze a report's medians into the baseline artifact.

    Called only by ``--update-baseline``. The point list is written in the
    matrix's stable dimension order so successive baselines diff cleanly.
    """
    path = path if path is not None else BASELINE_PATH
    data: dict[str, Any] = {
        "benchmark": BENCHMARK_NAME,
        "schema_version": BASELINE_SCHEMA_VERSION,
        "note": (
            "Expected recovery-assessment medians. Regenerate with "
            "'python -m benchmarks.latency_matrix --update-baseline'; a normal "
            "run never rewrites this file, so every change appears in review."
        ),
        "tolerance": report.tolerance,
        "points": [
            {
                "files": d.files,
                "decisions": d.decisions,
                "median_ms": round(d.median_ms, 3),
            }
            for d in report.dimensions
        ],
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path
