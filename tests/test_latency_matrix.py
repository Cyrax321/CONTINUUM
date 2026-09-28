"""Tests for the recovery latency-regression matrix (issue #766).

The timing-dependent paths are kept small so the suite stays fast; the policy
logic (schema, ordering, tolerance, baseline round-trip) is tested directly
because that is the part CI depends on. No test asserts an absolute millisecond
value, only classifications against a recorded baseline.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from benchmarks.latency_matrix import baseline as baseline_mod
from benchmarks.latency_matrix import emitter, fixtures, runner
from benchmarks.latency_matrix.baseline import Tolerance, classify_regression
from benchmarks.latency_matrix.runner import (
    DECISIONS,
    DEFAULT_MATRIX,
    FILES,
    _percentile,
    run_matrix,
    soft_budget_ms,
    soft_budget_status,
)

SMALL_POINTS: tuple[tuple[int, int], ...] = ((10, 2), (10, 100))
DEFAULT_TOLERANCE = Tolerance(regression_factor=3.0, absolute_floor_ms=25.0)


def _run_small(tmp_path: Path, generated_at: datetime | None = None) -> runner.MatrixReport:
    return run_matrix(
        points=SMALL_POINTS,
        samples=3,
        warmup=0,
        tolerance=DEFAULT_TOLERANCE,
        baseline_path=tmp_path / "absent_baseline.json",
        generated_at=generated_at,
    )


# --- Documented grid and budget -------------------------------------------


def test_default_matrix_covers_every_documented_support_point() -> None:
    """The grid is the full cross product of the note's measured points."""
    assert FILES == (50, 100, 200)
    assert DECISIONS == (10, 100)
    assert tuple((f, d) for f in FILES for d in DECISIONS) == DEFAULT_MATRIX


def test_budget_formula_matches_latency_budget_doc() -> None:
    """The two points the note names explicitly must derive as documented."""
    assert soft_budget_ms(100, 10) == pytest.approx(120.0)  # 50 + 20 + 50
    assert soft_budget_ms(200, 100) == pytest.approx(590.0)  # 50 + 40 + 500
    assert soft_budget_ms(50, 10) == pytest.approx(110.0)


def test_soft_budget_status_is_informational_not_failure() -> None:
    assert soft_budget_status(10.0, 100.0) == "pass"
    assert soft_budget_status(100.0, 10.0) == "informational_miss"


# --- Schema and stable ordering --------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "benchmark",
        "schema_version",
        "generated_at",
        "config",
        "environment",
        "tolerance",
        "dimensions",
        "status",
    ],
)
def test_report_schema_has_every_contract_key(tmp_path: Path, key: str) -> None:
    report = _run_small(tmp_path).as_dict()
    assert key in report


def test_report_dimensions_are_in_stable_order_and_repeatable(tmp_path: Path) -> None:
    """Two runs must emit the same dimension order and identical fixture metadata."""
    first = _run_small(tmp_path)
    second = _run_small(tmp_path)
    assert [(d.files, d.decisions) for d in first.dimensions] == list(SMALL_POINTS)
    assert [(d.files, d.decisions) for d in second.dimensions] == list(SMALL_POINTS)
    # Fixture metadata is the clock-free part of the record: it must not drift.
    assert [d.fixture for d in first.dimensions] == [d.fixture for d in second.dimensions]


def test_report_status_counts_match_the_dimensions(tmp_path: Path) -> None:
    report = _run_small(tmp_path)
    status = report.as_dict()["status"]
    assert status["regressions"] == len(report.regressions)
    assert status["soft_budget_misses"] == len(report.soft_budget_misses)


def test_every_dimension_carries_timing_summary_and_budget(tmp_path: Path) -> None:
    report = _run_small(tmp_path)
    for d in report.dimensions:
        assert d.samples == 3
        assert d.min_ms <= d.median_ms <= d.max_ms
        assert d.min_ms <= d.mean_ms <= d.max_ms
        assert d.min_ms <= d.p95_ms <= d.max_ms
        assert d.budget_ms == pytest.approx(soft_budget_ms(d.files, d.decisions))
        assert d.graph_build_ms >= 0.0
        # The measurement must actually have run the machinery, not timed a no-op.
        assert d.max_ms > 0.0


def test_environment_metadata_records_runner_without_network(tmp_path: Path) -> None:
    env = _run_small(tmp_path).as_dict()["environment"]
    assert {"python", "platform", "machine", "continuum_version", "ci"} <= set(env)
    assert isinstance(env["ci"], bool)


def test_percentile_is_nearest_rank() -> None:
    """``ceil(q * n)`` selects the rank the docstring names, not an interpolated index."""
    nine = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    assert _percentile(nine, 0.95) == 9.0
    assert _percentile(nine, 0.0) == 1.0
    assert _percentile(nine, 1.0) == 9.0
    with pytest.raises(ValueError, match="empty sample set"):
        _percentile([], 0.5)


def test_run_matrix_records_generator_points(tmp_path: Path) -> None:
    """A generator input must feed both the measurements and the recorded grid."""
    report = run_matrix(
        points=(p for p in SMALL_POINTS),
        samples=2,
        warmup=0,
        tolerance=DEFAULT_TOLERANCE,
        baseline_path=tmp_path / "absent.json",
    )
    assert [(d.files, d.decisions) for d in report.dimensions] == list(SMALL_POINTS)
    assert [tuple(p) for p in report.config["matrix_points"]] == list(SMALL_POINTS)


# --- Tolerance policy -------------------------------------------------------


def test_classify_regression_without_baseline_is_not_a_failure() -> None:
    assert classify_regression(10.0, None, DEFAULT_TOLERANCE) == baseline_mod.NO_BASELINE
    assert classify_regression(10.0, 0.0, DEFAULT_TOLERANCE) == baseline_mod.NO_BASELINE


def test_classify_regression_flags_a_real_slowdown() -> None:
    """3x slower and more than 25 ms slower is the regression signal."""
    assert classify_regression(300.0, 50.0, DEFAULT_TOLERANCE) == baseline_mod.REGRESSION


def test_classify_regression_passes_when_faster_or_unchanged() -> None:
    assert classify_regression(50.0, 50.0, DEFAULT_TOLERANCE) == baseline_mod.PASS
    assert classify_regression(40.0, 50.0, DEFAULT_TOLERANCE) == baseline_mod.PASS


def test_normal_variability_is_not_a_regression() -> None:
    """A modest relative rise below the absolute floor must not fail CI."""
    assert classify_regression(60.0, 50.0, DEFAULT_TOLERANCE) == baseline_mod.PASS
    # A large relative jump on a tiny baseline is jitter, not a regression:
    # 4x of a 0.5 ms baseline is 1.5 ms of excess, under the floor.
    assert classify_regression(2.0, 0.5, DEFAULT_TOLERANCE) == baseline_mod.PASS


def test_regression_requires_both_thresholds() -> None:
    """Ratio alone or excess alone is not enough; the AND is the conservative part."""
    ratio_only = Tolerance(regression_factor=1.5, absolute_floor_ms=25.0)
    # Breaches the ratio, not the floor.
    assert classify_regression(6.0, 2.0, ratio_only) == baseline_mod.PASS
    floor_only = Tolerance(regression_factor=100.0, absolute_floor_ms=1.0)
    # Breaches the floor, not the ratio.
    assert classify_regression(12.0, 10.0, floor_only) == baseline_mod.PASS


def test_tolerance_reads_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUUM_LATENCY_REGRESSION_FACTOR", "2.0")
    monkeypatch.setenv("CONTINUUM_LATENCY_ABSOLUTE_FLOOR_MS", "1.0")
    tol = Tolerance.from_env()
    assert tol.regression_factor == 2.0
    assert tol.absolute_floor_ms == 1.0
    # With both thresholds lowered this pair now counts as a regression.
    assert classify_regression(6.0, 2.0, tol) == baseline_mod.REGRESSION


def test_tolerance_rejects_nonsensical_factor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUUM_LATENCY_REGRESSION_FACTOR", "0.5")
    with pytest.raises(ValueError, match="must be > 1.0"):
        Tolerance.from_env()
    monkeypatch.setenv("CONTINUUM_LATENCY_REGRESSION_FACTOR", "not-a-number")
    with pytest.raises(ValueError, match="must be a number"):
        Tolerance.from_env()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_tolerance_rejects_non_finite_thresholds(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """NaN and infinity silently disable the gate rather than raising, so reject them.

    Every comparison against NaN is False and nothing can exceed infinity, so
    either one would turn every run into a pass without an error to signal it.
    """
    monkeypatch.setenv("CONTINUUM_LATENCY_REGRESSION_FACTOR", value)
    with pytest.raises(ValueError, match="must be a finite number"):
        Tolerance.from_env()
    monkeypatch.delenv("CONTINUUM_LATENCY_REGRESSION_FACTOR")
    monkeypatch.setenv("CONTINUUM_LATENCY_ABSOLUTE_FLOOR_MS", value)
    with pytest.raises(ValueError, match="must be a finite number"):
        Tolerance.from_env()


# --- Baseline artifact ------------------------------------------------------


def test_baseline_round_trip(tmp_path: Path) -> None:
    report = _run_small(tmp_path)
    path = tmp_path / "baseline.json"
    written = baseline_mod.write_baseline(report, path)
    assert written == path
    for d in report.dimensions:
        assert baseline_mod.baseline_median(d.files, d.decisions, path) == pytest.approx(
            d.median_ms, abs=1e-3
        )


def test_baseline_lookup_returns_none_for_unrecorded_point(tmp_path: Path) -> None:
    report = _run_small(tmp_path)
    path = tmp_path / "baseline.json"
    baseline_mod.write_baseline(report, path)
    assert baseline_mod.baseline_median(999, 999, path) is None


def test_missing_or_malformed_baseline_degrades_to_none(tmp_path: Path) -> None:
    assert baseline_mod.load_baseline(tmp_path / "nope.json") is None
    junk = tmp_path / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    assert baseline_mod.load_baseline(junk) is None
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"benchmark": "something-else"}), encoding="utf-8")
    assert baseline_mod.load_baseline(other) is None


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "points",
    [
        "not-a-list",
        None,
        ["not-a-dict"],
        [{"files": 10, "decisions": 2}],  # no median_ms
        [{"files": "ten", "decisions": 2, "median_ms": 1.0}],
        [{"files": 10, "decisions": 2, "median_ms": "fast"}],
    ],
)
def test_malformed_baseline_degrades_to_no_baseline(tmp_path: Path, points: object) -> None:
    """A lookup must never raise on a hand-edited artifact; it reads as absent."""
    path = _write(tmp_path, {"benchmark": baseline_mod.BENCHMARK_NAME, "points": points})
    assert baseline_mod.load_baseline(path) is None
    assert baseline_mod.baseline_median(10, 2, path) is None


def test_matrix_uses_the_committed_baseline(tmp_path: Path) -> None:
    """A recorded baseline close to the measurement classifies as pass."""
    probe = _run_small(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_mod.write_baseline(probe, baseline_path)
    rerun = run_matrix(
        points=SMALL_POINTS,
        samples=3,
        warmup=0,
        tolerance=DEFAULT_TOLERANCE,
        baseline_path=baseline_path,
    )
    assert all(d.regression == baseline_mod.PASS for d in rerun.dimensions)
    assert all(d.baseline_median_ms is not None for d in rerun.dimensions)


def test_matrix_detects_regression_against_a_stale_baseline(tmp_path: Path) -> None:
    """A baseline recording an implausibly fast assessment flags the real cost."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "benchmark": baseline_mod.BENCHMARK_NAME,
                "schema_version": 1,
                "tolerance": DEFAULT_TOLERANCE.as_dict(),
                "points": [
                    # An implausibly fast expectation for the 100-decision point
                    # and an implausibly slow one for the 2-decision point.
                    {"files": 10, "decisions": 100, "median_ms": 0.001},
                    {"files": 10, "decisions": 2, "median_ms": 1000.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    # A zero absolute floor makes the classification depend on the ratio alone,
    # so the assertion holds on a fast runner as well as a slow one; the
    # default 25 ms floor would otherwise set a minimum machine speed.
    integration_tolerance = Tolerance(regression_factor=1.5, absolute_floor_ms=0.0)
    report = run_matrix(
        points=SMALL_POINTS,
        samples=3,
        warmup=0,
        tolerance=integration_tolerance,
        baseline_path=baseline_path,
    )
    heavy = next(d for d in report.dimensions if d.decisions == 100)
    light = next(d for d in report.dimensions if d.decisions == 2)
    assert heavy.regression == baseline_mod.REGRESSION
    assert light.regression == baseline_mod.PASS


# --- Determinism of the fixture --------------------------------------------


def test_fixture_is_deterministic_across_builds(tmp_path: Path) -> None:
    """Same parameters, byte-identical structure: a regression is then code, not noise."""
    a = fixtures.make_fixture(tmp_path / "a", 20, 8)
    b = fixtures.make_fixture(tmp_path / "b", 20, 8)
    assert fixtures.fixture_summary(a) == fixtures.fixture_summary(b)
    assert a.graph_build_ms > 0.0
    assert len(a.graph.files_using("dep0")) > 0
    assert a.scope == fixtures.SCOPE


def test_fixture_rejects_degenerate_points(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=">= 1 file"):
        fixtures.make_fixture(tmp_path / "c", 0, 10)
    with pytest.raises(ValueError, match=">= 2 decisions"):
        fixtures.make_fixture(tmp_path / "d", 10, 1)


# --- CLI behaviour ----------------------------------------------------------


def test_cli_list_prints_the_grid_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    from benchmarks.latency_matrix.__main__ import main

    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "files=200" in out
    assert "regression_factor" in out


def test_cli_normal_run_does_not_touch_the_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only --update-baseline rewrites the artifact."""
    from benchmarks.latency_matrix.__main__ import main

    baseline_path = tmp_path / "baseline.json"
    # Generous expectations so the classification is a clean pass on any runner.
    baseline_path.write_text(
        json.dumps(
            {
                "benchmark": baseline_mod.BENCHMARK_NAME,
                "schema_version": 1,
                "tolerance": DEFAULT_TOLERANCE.as_dict(),
                "points": [
                    {"files": 10, "decisions": 2, "median_ms": 5000.0},
                    {"files": 10, "decisions": 100, "median_ms": 5000.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    # Point both the resolution helper and the module default at the temp copy.
    monkeypatch.setattr(baseline_mod, "BASELINE_PATH", baseline_path)
    before = baseline_path.read_text(encoding="utf-8")
    code = main(["--samples", "3", "--warmup", "0", "--out", str(tmp_path / "report")])
    assert code == 0
    assert baseline_path.read_text(encoding="utf-8") == before


def test_cli_reports_regression_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The exit code is the CI signal, and it is driven by the classification."""
    from benchmarks.latency_matrix.__main__ import main

    heavy = runner.DimensionResult(
        files=10,
        decisions=100,
        budget_ms=soft_budget_ms(10, 100),
        graph_build_ms=1.0,
        samples=3,
        min_ms=20.0,
        median_ms=30.0,
        mean_ms=30.0,
        p95_ms=31.0,
        max_ms=32.0,
        fixture={},
        soft_budget="pass",
        regression=baseline_mod.REGRESSION,
        baseline_median_ms=1.0,
    )
    clean = runner.DimensionResult(
        files=10,
        decisions=2,
        budget_ms=soft_budget_ms(10, 2),
        graph_build_ms=1.0,
        samples=3,
        min_ms=1.0,
        median_ms=1.0,
        mean_ms=1.0,
        p95_ms=1.0,
        max_ms=1.0,
        fixture={},
        soft_budget="informational_miss",
        regression=baseline_mod.PASS,
        baseline_median_ms=1.0,
    )
    report = runner.MatrixReport(
        benchmark=baseline_mod.BENCHMARK_NAME,
        schema_version=runner.SCHEMA_VERSION,
        generated_at=datetime(2026, 1, 1),
        config={"samples": 3, "warmup": 0, "budget_formula": "x", "matrix_points": []},
        environment=runner.environment_metadata(),
        tolerance=DEFAULT_TOLERANCE.as_dict(),
        dimensions=[clean, heavy],
    )
    monkeypatch.setattr(runner, "run_matrix", lambda **kwargs: report)
    code = main(["--out", str(tmp_path / "report")])
    # A regression fails; the soft-budget miss on the clean point does not.
    assert code == 1


def test_cli_update_baseline_adopts_new_numbers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--update-baseline writes the artifact and does not fail on a breach."""
    from benchmarks.latency_matrix.__main__ import main

    baseline_path = tmp_path / "baseline.json"
    monkeypatch.setattr(baseline_mod, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(
        runner,
        "run_matrix",
        lambda **kwargs: run_matrix(
            points=((10, 2),),
            samples=2,
            warmup=0,
            tolerance=DEFAULT_TOLERANCE,
            baseline_path=tmp_path / "absent.json",
        ),
    )
    assert main(["--update-baseline", "--out", str(tmp_path / "report")]) == 0
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert data["benchmark"] == baseline_mod.BENCHMARK_NAME
    assert [(p["files"], p["decisions"]) for p in data["points"]] == [(10, 2)]


def test_emitter_writes_json_and_markdown(tmp_path: Path) -> None:
    report = _run_small(tmp_path)
    json_path, md_path = emitter.emit_report(report, tmp_path / "nested" / "report")
    assert json_path.exists() and md_path.exists()
    assert (
        json.loads(json_path.read_text(encoding="utf-8"))["benchmark"]
        == baseline_mod.BENCHMARK_NAME
    )
    assert "Recovery latency matrix" in md_path.read_text(encoding="utf-8")
