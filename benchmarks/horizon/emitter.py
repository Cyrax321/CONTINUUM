"""Emitter for horizon-scale benchmark.

Shares the metric schema with fault-injection via the common
BenchmarkReport/ScenarioResult envelope.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from continuum.benchmark.phase6.metrics import BenchmarkReport


def emit_horizon_report(
    report: BenchmarkReport, out_path: str | Path, benchmark_name: str = "horizon"
) -> tuple[Path, Path]:
    """Write the horizon report as JSON and Markdown using shared envelope.

    {
      "benchmark": "horizon",
      "generated_at": "...",
      "summary": {
        "total": ...,
        "passed": ...,
        "failed": ...,
        "accuracy": ...,
        "unnecessary_human_escalation_rate": ...,
        "repair_precision": ...,
        "duplicate_side_effects": ...,
        "duplicate_work": ...,
        "compression_ratio": ...
      },
      "results": [...]
    }
    """
    base = Path(out_path)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")

    summary = report.summary()
    # Compute horizon aggregates
    # Aggregate six metrics
    accuracies = []
    unnecessary_rates = []
    repair_precisions = []
    dup_sides = []
    dup_works = []
    compressions = []
    for r in report.results:
        accuracies.append(r.metrics.get("accuracy", 0))
        unnecessary_rates.append(r.metrics.get("unnecessary_human_escalation_rate", 0))
        repair_precisions.append(r.metrics.get("repair_precision", 0))
        dup_sides.append(r.metrics.get("duplicate_side_effects", 0))
        dup_works.append(r.metrics.get("duplicate_work", 0))
        compressions.append(r.metrics.get("compression_ratio", 0))

    horizon_summary: dict[str, Any] = dict(summary)
    if accuracies:
        horizon_summary["accuracy"] = round(sum(accuracies) / len(accuracies), 3)
        horizon_summary["unnecessary_human_escalation_rate"] = round(
            sum(unnecessary_rates) / len(unnecessary_rates), 3
        )
        horizon_summary["repair_precision"] = round(
            sum(repair_precisions) / len(repair_precisions), 3
        )
        horizon_summary["duplicate_side_effects"] = sum(dup_sides)
        horizon_summary["duplicate_work"] = round(sum(dup_works) / len(dup_works), 3)
        horizon_summary["compression_ratio"] = (
            round(sum(compressions) / len(compressions), 3) if compressions else 0
        )

    envelope = {
        "benchmark": benchmark_name,
        "generated_at": report.generated_at.isoformat(),
        "summary": horizon_summary,
        "results": [r.model_dump(mode="json") for r in report.results],
    }
    json_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")

    # Markdown
    lines = [f"# Horizon benchmark report ({benchmark_name})", ""]
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    lines.append("")
    lines.append(
        f"Total: {horizon_summary.get('total', 0)}  Passed: {horizon_summary.get('passed', 0)}  Failed: {horizon_summary.get('failed', 0)}"
    )
    lines.append(
        f"Accuracy: {horizon_summary.get('accuracy', 0)}  Unnecessary escalation: {horizon_summary.get('unnecessary_human_escalation_rate', 0)}  Repair precision: {horizon_summary.get('repair_precision', 0)}"
    )
    lines.append(
        f"Duplicate side effects: {horizon_summary.get('duplicate_side_effects', 0)}  Duplicate work: {horizon_summary.get('duplicate_work', 0)}  Compression: {horizon_summary.get('compression_ratio', 0)}"
    )
    lines.append("")
    lines.append(
        "| Scenario | Outcome | Cycles | Years | Correct | Actual | Acc | Unnec | Repair | DupSide | DupWork | Compress | Notes |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in report.results:
        cycles = r.metrics.get("reconstruction_cycles", "")
        years = r.metrics.get("years_elapsed", "")
        correct = r.metrics.get("correct_mode", "")
        actual = r.metrics.get("actual_mode", "")
        acc = r.metrics.get("accuracy", "")
        unnec = r.metrics.get("unnecessary_human_escalation_rate", "")
        repair = r.metrics.get("repair_precision", "")
        dup_side = r.metrics.get("duplicate_side_effects", "")
        dup_work = r.metrics.get("duplicate_work", "")
        compress = r.metrics.get("compression_ratio", "")
        note = " ".join(r.notes).replace("|", "/").replace("\n", " ")[:60]
        lines.append(
            f"| {r.scenario} | {r.outcome.value} | {cycles} | {years} | {correct} | {actual} | {acc} | {unnec} | {repair} | {dup_side} | {dup_work} | {compress} | {note} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
