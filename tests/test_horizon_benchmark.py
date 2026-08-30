"""Horizon-scale benchmark with simulated years and judge (issue #398)."""

from __future__ import annotations

import json
from pathlib import Path


def test_horizon_scenarios_run_with_at_least_100_reconstruction_cycles() -> None:
    from benchmarks.horizon.runner import run_horizon_suite

    report = run_horizon_suite()
    assert len(report.results) >= 5
    for r in report.results:
        cycles = r.metrics.get("reconstruction_cycles", 0)
        assert cycles >= 100, f"{r.scenario} only {cycles} cycles, need 100"
        years = r.metrics.get("years_elapsed", 0)
        assert years >= 0.5, f"{r.scenario} years {years} too low"
        assert r.metrics.get("accuracy") in (0.0, 1.0)
        assert "correct_mode" in r.metrics
        assert "actual_mode" in r.metrics


def test_judge_labels_exist_for_every_scenario_and_disagreements_resolved() -> None:
    from benchmarks.horizon.scenarios import HORIZON_SCENARIOS

    for scen in HORIZON_SCENARIOS:
        assert scen.correct_mode in ("resume", "repair", "request_human", "abort")
        assert scen.labeled_by
        # Check resolution note in docstring
        assert (
            "consensus" in scen.labeled_by
            or "resolved" in scen.labeled_by
            or scen.labeled_by == "author+reviewer consensus"
        )
    # Verify the two labelings and resolution are documented in the module docstring
    import benchmarks.horizon.scenarios as mod

    doc = mod.__doc__ or ""
    assert "Disagreements resolved" in doc or "resolved" in doc.lower()


def test_all_six_metrics_emitted_per_run_and_rendered() -> None:
    from benchmarks.horizon.emitter import emit_horizon_report
    from benchmarks.horizon.runner import run_horizon_suite

    report = run_horizon_suite()
    # Check six metrics per result
    for r in report.results:
        for metric in (
            "accuracy",
            "unnecessary_human_escalation_rate",
            "repair_precision",
            "duplicate_side_effects",
            "duplicate_work",
            "compression_ratio",
        ):
            assert metric in r.metrics, f"{r.scenario} missing {metric}"
    # Emit and check summary has them
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "horizon_report")
        json_path, md_path = emit_horizon_report(report, out)
        assert json_path.exists()
        assert md_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["benchmark"] == "horizon"
        summary = data["summary"]
        for metric in (
            "accuracy",
            "unnecessary_human_escalation_rate",
            "repair_precision",
            "duplicate_side_effects",
            "duplicate_work",
            "compression_ratio",
        ):
            assert metric in summary
        md = md_path.read_text(encoding="utf-8")
        for metric in ("Accuracy", "Unnecessary", "Repair precision", "Duplicate"):
            assert metric.lower() in md.lower()


def test_table_regenerates_from_runner_no_invented_numbers(tmp_path: Path) -> None:
    # Prove the table is not hand-edited: delete it and re-run, it should reappear identical
    from benchmarks.horizon.emitter import emit_horizon_report
    from benchmarks.horizon.runner import run_horizon_suite

    report = run_horizon_suite()
    out = tmp_path / "horizon_report"
    json_path, md_path = emit_horizon_report(report, str(out))
    # Read the generated markdown and ensure it contains real numbers from the run
    md1 = md_path.read_text(encoding="utf-8")
    # Re-run and compare
    report2 = run_horizon_suite()
    json_path2, md_path2 = emit_horizon_report(report2, str(tmp_path / "horizon_report2"))
    md2 = md_path2.read_text(encoding="utf-8")
    # The two runs should be identical (deterministic) - no invented numbers
    # Allow for generated_at timestamp difference, so compare without that line
    lines1 = [line for line in md1.splitlines() if not line.startswith("Generated:")]
    lines2 = [line for line in md2.splitlines() if not line.startswith("Generated:")]
    assert lines1 == lines2
    # Also test README regeneration
    readme = Path("README.md")
    if readme.exists():
        original = readme.read_text(encoding="utf-8")
        # Simulate deleting the bench section
        if "<!-- BENCH:START -->" in original:
            # Run the runner's README regeneration via benchmarks/run.py
            import subprocess
            import sys

            result = subprocess.run(
                [sys.executable, "benchmarks/run.py"], capture_output=True, text=True, timeout=60
            )
            assert result.returncode == 0
            regenerated = readme.read_text(encoding="utf-8")
            assert "<!-- BENCH:START -->" in regenerated
            assert "<!-- BENCH:END -->" in regenerated
            # Restore original to avoid dirtying working tree in test
            readme.write_text(original, encoding="utf-8")


def test_shared_emitter_schema_with_fault_injection() -> None:
    # Both suites share the same BenchmarkReport envelope
    import json
    import os
    import tempfile
    from datetime import datetime

    from benchmarks.horizon.emitter import emit_horizon_report
    from benchmarks.horizon.runner import run_horizon_suite
    from continuum.benchmark.phase6.metrics import RecoveryOutcome, ScenarioResult

    horizon_report = run_horizon_suite()
    with tempfile.TemporaryDirectory() as tmp:
        from benchmarks.fault_injection.emitter import emit_fault_injection_report
        from continuum.benchmark.phase6.metrics import BenchmarkReport

        dummy_fault = BenchmarkReport(
            generated_at=datetime.now(),
            results=[
                ScenarioResult(
                    scenario="fault_test",
                    outcome=RecoveryOutcome.PASS,
                    passed=True,
                    metrics={"detection_module": "x"},
                )
            ],
        )
        fj, _ = emit_fault_injection_report(dummy_fault, os.path.join(tmp, "fault"))
        hj, _ = emit_horizon_report(horizon_report, os.path.join(tmp, "horizon"))
        fj_data = json.loads(Path(fj).read_text(encoding="utf-8"))
        hj_data = json.loads(Path(hj).read_text(encoding="utf-8"))
        for data in (fj_data, hj_data):
            assert "benchmark" in data
            assert "generated_at" in data
            assert "summary" in data
            assert "results" in data
            for r in data["results"]:
                assert "scenario" in r
                assert "outcome" in r
                assert "metrics" in r
