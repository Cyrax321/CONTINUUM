"""Recovery latency-regression matrix (issue #766).

Measures ``RecoveryEngine.assess_scoped`` over a deterministic grid of
repository sizes and decision counts, derives the soft SLO budget per point, and
compares medians against a committed baseline with a documented tolerance.

Observational only: no runtime timeout, no change to recovery decisions.

Run it with::

    python -m benchmarks.latency_matrix
"""

from __future__ import annotations

from benchmarks.latency_matrix import baseline, emitter, fixtures, runner
from benchmarks.latency_matrix.baseline import Tolerance, classify_regression
from benchmarks.latency_matrix.runner import (
    DECISIONS,
    DEFAULT_MATRIX,
    DEFAULT_SAMPLES,
    FILES,
    MatrixReport,
    run_matrix,
    soft_budget_ms,
)

__all__ = [
    "DECISIONS",
    "DEFAULT_MATRIX",
    "DEFAULT_SAMPLES",
    "FILES",
    "MatrixReport",
    "Tolerance",
    "baseline",
    "classify_regression",
    "emitter",
    "fixtures",
    "run_matrix",
    "runner",
    "soft_budget_ms",
]
