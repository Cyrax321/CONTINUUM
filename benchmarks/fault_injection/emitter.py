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
    # Use the first result's aggregates as the suite-level (they are all same)
    if report.results:
        first = report.results[0].metrics
        fault_summary["detection_rate"] = first.get("detection_rate", 0)
        fault_summary["unsafe_resume_rate"] = first.get("unsafe_resume_rate", 0)
        fault_summary["false_positive_rate"] = first.get("false_positive_rate", 0)
        # Average propagation distance
        if prop_distances:
            fault_summary["propagation_distance"] = round(
                sum(prop_distances) / len(prop_distances), 3
            )

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
