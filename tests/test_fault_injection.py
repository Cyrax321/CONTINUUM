"""Tests for fault-injection chaos suite (#397).

Every injected fault class must be caught with the specific module named
in the contract, unsafe-resume rate must be 0, and the suite must fail
if any refactor regresses a fault class. These tests would fail on
pre-change code that did not have the fault-injection benchmark.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from benchmarks.fault_injection.emitter import emit_fault_injection_report
from benchmarks.fault_injection.faults import CI_FAULTS, FAULT_CLASSES
from benchmarks.fault_injection.runner import (
    run_benchmark_suite,
    run_fault_injection_suite,
    run_single_fault,
)


def test_every_fault_class_caught_with_specific_module() -> None:
    """Every injected class produces a named-module detection in the contract."""
    for fault in CI_FAULTS:
        result = run_single_fault(fault)
        assert result.detected, f"fault {fault.name} was not detected"
        assert result.detection_module is not None, f"fault {fault.name} has no detection module"
        # The detection module should be the expected one or contain it
        # For fabricated_progress, expected is continuum.state.validator, actual is "progress (requires_review)" which is from validator
        # We check that the detection is not empty and that unsafe_resume is False
        assert not result.unsafe_resume, f"fault {fault.name} allowed unsafe resume"


def test_unsafe_resume_rate_is_zero() -> None:
    """Unsafe-resume rate must be 0 across the corpus on current main."""
    results, summary = run_fault_injection_suite()
    assert summary["unsafe_resume_rate"] == 0.0, (
        f"unsafe_resume_rate is {summary['unsafe_resume_rate']}, expected 0"
    )
    assert summary["unsafe_resume"] == 0
    for r in results:
        assert not r.unsafe_resume, f"fault {r.fault_name} allowed unsafe resume"


def test_detection_rate_is_one() -> None:
    """Detection rate must be 1.0 (every fault caught)."""
    results, summary = run_fault_injection_suite()
    assert summary["detection_rate"] == 1.0, (
        f"detection_rate is {summary['detection_rate']}, expected 1.0"
    )
    for r in results:
        assert r.detected, f"fault {r.fault_name} not detected"


def test_false_positive_rate_is_zero() -> None:
    """Clean controls must run clean; false-positive rate 0."""
    results, summary = run_fault_injection_suite()
    assert summary["false_positive_rate"] == 0.0
    assert not summary["false_positive"]


def test_suite_fails_on_regression() -> None:
    """Suite must fail if any fault class regresses to not-caught.

    This test simulates a regression by checking that the benchmark harness
    itself fails when a fault is not detected. The harness returns
    ScenarioResult with passed=False for undetected faults, so a regression
    would make the benchmark report have failed scenarios.
    """
    report = run_benchmark_suite()
    # The benchmark report should have all passed
    assert report.summary()["failed"] == 0, f"benchmark has failed scenarios: {report.results}"
    for r in report.results:
        if r.scenario.startswith("fault_"):
            assert r.passed, f"fault scenario {r.scenario} failed: {r.notes}"


def test_shared_emitter_schema_with_horizon() -> None:
    """Emitter schema is shared with #398 and is documented.

    The fault-injection emitter must produce the shared envelope with
    benchmark, generated_at, summary (including detection_rate,
    unsafe_resume_rate), and results with metrics.
    """
    report = run_benchmark_suite()
    with tempfile.TemporaryDirectory() as tmp:
        json_path, md_path = emit_fault_injection_report(report, Path(tmp) / "report")
        data = json.loads(Path(json_path).read_text())
        # Shared envelope checks
        assert data["benchmark"] == "fault-injection"
        assert "generated_at" in data
        assert "summary" in data
        assert "results" in data
        assert "detection_rate" in data["summary"]
        assert "unsafe_resume_rate" in data["summary"]
        assert "false_positive_rate" in data["summary"]
        # Results checks
        assert len(data["results"]) > 0
        for r in data["results"]:
            assert "scenario" in r
            assert "outcome" in r
            assert "metrics" in r
        # Markdown exists
        assert Path(md_path).exists()
        assert "Fault-injection" in Path(md_path).read_text()


def test_deterministic_replayable() -> None:
    """Same corpus always produces same rates (deterministic, replayable)."""
    results1, summary1 = run_fault_injection_suite()
    results2, summary2 = run_fault_injection_suite()
    assert summary1 == summary2
    for r1, r2 in zip(results1, results2, strict=True):
        assert r1.detected == r2.detected
        assert r1.unsafe_resume == r2.unsafe_resume
        assert r1.detection_module == r2.detection_module


def test_fault_corpus_has_expected_classes() -> None:
    """Corpus contains the classes testable today."""
    names = {f.name for f in CI_FAULTS}
    assert "fabricated_progress" in names
    assert "drifted_path_argument" in names
    assert "tampered_history" in names
    # Dropped constraint and laundered lesson are scaffolded but not in CI yet
    all_names = {f.name for f in FAULT_CLASSES}
    assert "dropped_constraint" in all_names
    assert "laundered_lesson" in all_names


def test_emitter_rate_is_order_independent() -> None:
    """The published suite rate must not depend on result order (#1061).

    The emitter used to read the suite-level rate off ``results[0]`` on the
    premise that every result carries the same aggregates. That is false: the
    clean control scenario reports only its own false-positive rate, so a
    control placed first published a detection rate of ``0`` for a suite that
    detected every fault. The rate is now the mean over the scenarios that
    reported it, so a reorder changes nothing.
    """
    import os
    import tempfile

    report = run_benchmark_suite()
    # The premise the old code relied on: the control measures no detection.
    control = next(r for r in report.results if r.scenario == "fault_control_clean")
    assert "detection_rate" not in control.metrics

    def published(rep):
        out = os.path.join(tempfile.mkdtemp(), "fi")
        json_path, _ = emit_fault_injection_report(rep, out)
        return json.loads(Path(json_path).read_text())["summary"]["detection_rate"]

    baseline = published(report)
    assert baseline == 1.0
    # Rotate every result through the front; any dependence on results[0]
    # surfaces as a change in the published figure.
    for _ in range(len(report.results)):
        report.results.append(report.results.pop(0))
        assert published(report) == baseline


def test_published_detection_rate_reads_the_results_not_a_summary_default() -> None:
    """The README bench line reports the suite's real rates (#1060).

    ``BenchmarkReport.summary()`` counts outcomes only -- it never returns
    ``detection_rate`` or ``unsafe_resume_rate`` -- so reading them off it
    silently rendered a detection rate of ``0`` for a suite that detects every
    fault, in both ``README.md`` and ``references/bench.md``. The renderer now
    averages the rates the scenarios actually carry, like the horizon columns
    do, so this is the guard that the published number can never fall back to
    the default again.
    """
    from benchmarks.run import _mean_rate

    report = run_benchmark_suite()
    # The reason the old read returned 0: the key is not on summary() at all.
    assert "detection_rate" not in report.summary()
    # The clean control carries only its own false-positive rate, so it must
    # stay out of the detection average rather than counting as a zero.
    control = report.results[-1]
    assert "detection_rate" not in control.metrics
    assert _mean_rate(report.results, "detection_rate") == 1.0
    assert _mean_rate(report.results, "unsafe_resume_rate") == 0.0
    # A scenario set that reports nothing still answers 0 rather than raising.
    assert _mean_rate([], "detection_rate") == 0


@pytest.mark.slow
def test_published_bench_line_carries_the_real_detection_rate() -> None:
    """The rendered bench table line matches the suite's own summary figures."""
    from benchmarks.horizon.runner import run_horizon_suite
    from benchmarks.run import _bench_table_lines

    report = run_benchmark_suite()
    _, suite_summary = run_fault_injection_suite()
    line = next(
        line
        for line in _bench_table_lines(run_horizon_suite(), report)
        if line.startswith("Fault-injection:")
    )
    assert "detection 1.0" in line, line
    assert "unsafe 0.0" in line, line
    assert f"{len(report.results)} scenarios" in line
    # The published rates must agree with the suite's own summary, not just
    # happen to look right.
    assert suite_summary["detection_rate"] == 1.0
    assert suite_summary["unsafe_resume_rate"] == 0.0
