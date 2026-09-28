"""Emitter for fault-injection chaos suite.

Shares the metric schema with #398 (horizon) via the common
BenchmarkReport/ScenarioResult envelope. The emitter is the single
place that defines the JSON shape that both suites and CI will consume,
so the schema is documented here and coordinated via the board comment
on #399.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from continuum.benchmark.phase6.metrics import BenchmarkReport


def _mean_over(values: list[Any]) -> Any:
    """Mean of a collected metric, or 0 when nothing reported it.

    The envelope documents these summary keys unconditionally, so a suite where
    no scenario carried a given metric still answers 0 rather than dropping the
    key. Rounded to three places to match the rates the runner itself rounds.
    """
    if not values:
        return 0
    return round(sum(values) / len(values), 3)


def emit_fault_injection_report(
    report: BenchmarkReport, out_path: str | Path, benchmark_name: str = "fault-injection"
) -> tuple[Path, Path]:
    """Write the fault-injection report as JSON and Markdown.

    The report uses the shared envelope:
    {
      "benchmark": "fault-injection",
      "generated_at": "...",
      "summary": {
        "total": ...,
        "passed": ...,
        "failed": ...,
        "detection_rate": ...,
        "unsafe_resume_rate": ...,
        "false_positive_rate": ...,
        "propagation_distance": ...
      },
      "results": [...]
    }

    This shape is shared with the horizon suite (#398), which will emit
    the same envelope with its own summary metrics (accuracy,
    unnecessary_human_escalation_rate, etc.). Both suites reuse
    BenchmarkReport/ScenarioResult so benchmarks/run.py can wire them
    through the same write_report path.
    """
    base = Path(out_path)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")

    # Build the shared envelope
    summary = report.summary()
    # Extract fault-injection specific aggregates from results metrics
    # For backward compat, we also include the phase6 summary fields
    fault_summary: dict[str, Any] = dict(summary)
    # Compute detection-specific aggregates if present in results
    detection_rates = []
    unsafe_rates = []
    fp_rates = []
    prop_distances = []
    for r in report.results:
        if "detection_rate" in r.metrics:
            detection_rates.append(r.metrics["detection_rate"])
        if "unsafe_resume_rate" in r.metrics:
            unsafe_rates.append(r.metrics["unsafe_resume_rate"])
        if "false_positive_rate" in r.metrics:
            fp_rates.append(r.metrics["false_positive_rate"])
        if "propagation_distance" in r.metrics:
            prop_distances.append(r.metrics["propagation_distance"])
    # The suite-level rates are the mean over the scenarios that reported them,
    # not a copy of results[0]. The premise "they are all same" does not hold:
    # every fault scenario carries the suite's own aggregate, but the clean
    # control scenario reports only its false-positive rate, so reading the
    # first result made the published figure depend on result order -- a
    # control placed first rendered a detection rate of 0 for a suite that
    # detected every fault (#1061). Scenarios that never measured a rate are
    # skipped rather than counted as zero, which is what keeps the control out
    # of the detection average.
    fault_summary["detection_rate"] = _mean_over(detection_rates)
    fault_summary["unsafe_resume_rate"] = _mean_over(unsafe_rates)
    fault_summary["false_positive_rate"] = _mean_over(fp_rates)
    fault_summary["propagation_distance"] = _mean_over(prop_distances)

    envelope = {
        "benchmark": benchmark_name,
        "generated_at": report.generated_at.isoformat(),
        "summary": fault_summary,
        "results": [r.model_dump(mode="json") for r in report.results],
    }
    json_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

    # Markdown rendering
    lines = [f"# Fault-injection benchmark report ({benchmark_name})", ""]
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    lines.append("")
    lines.append(
        f"Total: {fault_summary.get('total', 0)}  Passed: {fault_summary.get('passed', 0)}  Failed: {fault_summary.get('failed', 0)}"
    )
    lines.append(
        f"Detection rate: {fault_summary.get('detection_rate', 0)}  Unsafe-resume rate: {fault_summary.get('unsafe_resume_rate', 0)}  False-positive rate: {fault_summary.get('false_positive_rate', 0)}"
    )
    lines.append("")
    lines.append("| Scenario | Outcome | Detected | Module | Propagation | Unsafe | Notes |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in report.results:
        detected = r.metrics.get("detection_module", "") or ""
        expected = r.metrics.get("expected_module", "") or ""
        module = detected or expected
        prop = r.metrics.get("propagation_distance", "")
        unsafe = r.metrics.get("unsafe_resume", "")
        note = " ".join(r.notes).replace("|", "/").replace("\n", " ")[:80]
        lines.append(
            f"| {r.scenario} | {r.outcome.value} | {r.passed} | {module} | {prop} | {unsafe} | {note} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
