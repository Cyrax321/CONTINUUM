"""Horizon-scale benchmark with simulated years and judge (issue #398)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.slow
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


@pytest.mark.slow
def test_abort_scenario_reaches_abort_through_the_risk_path() -> None:
    """The abort scenario must exercise a mechanism the engine really has.

    DECISION_INVALIDATED does not produce an abort proposal, so a scenario that
    drives abort through it fails by construction and deflates the published
    accuracy figure without any real regression in decision quality (#1028).
    The engine reaches ABORT only through the risk policy
    (recovery/risk.py: side_effect_duplicate -> ABORT). This pins that path end
    to end: if the engine drops it, this test and the scenario both fail, and
    the number moves visibly instead of silently.
    """
    from benchmarks.horizon.runner import run_single_horizon

    result = run_single_horizon("abort_condition_year")
    assert result.passed, (
        f"abort_condition_year did not reach abort: got "
        f"{result.metrics['actual_mode']}, notes={result.notes}"
    )
    assert result.metrics["actual_mode"] == "abort"
    assert result.metrics["correct_mode"] == "abort"


def test_the_engines_abort_path_is_reachable_independently_of_the_scenario() -> None:
    """The mechanism the abort scenario depends on is real engine behaviour.

    A regression that removes the risk -> abort mapping would otherwise only
    show up as a slow benchmark failure. This is the fast unit-level guard for
    the same property, so it runs in every CI matrix job.
    """
    from continuum.events import EventType
    from continuum.models import Origin, Run
    from continuum.recovery import RecoveryEngine
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="abort_probe", goal="probe"))
    storage.append_event("abort_probe", EventType.RUN_STARTED, {"goal": "probe"})
    storage.append_event(
        "abort_probe",
        EventType.RISK_OBSERVED,
        {"trigger": "side_effect_duplicate", "score": 1.0},
        source=Origin.EXTERNAL_MONITOR,
    )
    decision = RecoveryEngine(storage).assess("abort_probe")
    assert decision.mode.value == "abort", (
        f"side_effect_duplicate risk should reach abort, got {decision.mode.value}"
    )
    assert decision.safe is False


@pytest.mark.slow
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


@pytest.mark.slow
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
    # Also test README regeneration. Anchor to the repo root: a relative
    # README.md only resolves when pytest runs from the root, and the
    # regeneration subprocess needs the anchored script path and cwd alike
    # (issue #837).
    root = Path(__file__).resolve().parents[1]
    readme = root / "README.md"
    if readme.exists():
        original = readme.read_text(encoding="utf-8")
        # Simulate deleting the bench section
        if "<!-- BENCH:START -->" in original:
            # Run the runner's README regeneration via benchmarks/run.py
            import subprocess
            import sys

            try:
                # The runner executes the full Phase 6 recovery suite, the
                # fault-injection suite and the crash-recovery benchmark before
                # it can rewrite the table, so under branch coverage on a
                # loaded Windows runner it clears 60s. Match the --publish
                # budget below rather than timing out a run that is working.
                result = subprocess.run(
                    [sys.executable, str(root / "benchmarks/run.py")],
                    capture_output=True,
                    text=True,
                    # The runner executes the full horizon, fault-injection and
                    # crash-recovery suites, which takes minutes on a cold
                    # Windows runner -- 60s timed out there (seen on this job
                    # across unrelated PRs). Match the 600s the --publish test
                    # below already grants the same script.
                    timeout=600,
                    cwd=root,
                )
                assert result.returncode == 0, result.stderr
                regenerated = readme.read_text(encoding="utf-8")
                assert "<!-- BENCH:START -->" in regenerated
                assert "<!-- BENCH:END -->" in regenerated
            finally:
                # Restore the tracked file even when an assertion above fails,
                # so a red test never leaves the working tree dirty.
                readme.write_text(original, encoding="utf-8")


@pytest.mark.slow
def test_publish_flag_refreshes_bench_md() -> None:
    # --publish refreshes the latest-results block in references/bench.md
    # without touching the default-run behavior above. Same anchoring and
    # restore discipline as the README test (issue #837).
    root = Path(__file__).resolve().parents[1]
    bench_md = root / "references" / "bench.md"
    readme = root / "README.md"
    if bench_md.exists():
        original_bench = bench_md.read_text(encoding="utf-8")
        original_readme = readme.read_text(encoding="utf-8") if readme.exists() else None
        import subprocess
        import sys

        try:
            result = subprocess.run(
                [sys.executable, str(root / "benchmarks/run.py"), "--publish"],
                capture_output=True,
                text=True,
                timeout=600,
                cwd=root,
            )
            assert result.returncode == 0, result.stderr
            regenerated = bench_md.read_text(encoding="utf-8")
            assert "<!-- BENCH:START -->" in regenerated
            assert "<!-- BENCH:END -->" in regenerated
            assert regenerated.count("<!-- BENCH:START -->") == 1
        finally:
            # The subprocess also rewrites README.md with a fresh timestamp,
            # so restore both files and never leave the tree dirty.
            bench_md.write_text(original_bench, encoding="utf-8")
            if original_readme is not None:
                readme.write_text(original_readme, encoding="utf-8")


@pytest.mark.slow
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
