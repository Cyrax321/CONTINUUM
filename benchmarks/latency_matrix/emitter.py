"""Write the latency matrix report to disk (issue #766).

JSON is the contract CI and tests read; Markdown is for a human scanning a
nightly run. Both land under ``benchmarks/out/``, which is gitignored, so the
committed artifact is the baseline in ``baseline.json`` and these files stay
report-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.latency_matrix.runner import MatrixReport


def emit_report(report: MatrixReport, out_prefix: str | Path) -> tuple[Path, Path]:
    """Write ``<prefix>.json`` and ``<prefix>.md``; return both paths."""
    out_prefix = Path(out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def _markdown(report: MatrixReport) -> str:
    env = report.environment
    lines = [
        "# Recovery latency matrix",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Python {env['python']} on {env['platform']} ({env['machine']})",
        f"Samples per point: {report.config['samples']} (warmup {report.config['warmup']})",
        "",
        "Budget is the soft SLO from `docs/research/latency_budget.md`: "
        f"`{report.config['budget_formula']}`. A budget miss is informational. A "
        "regression is a breach of both tolerance thresholds against the committed "
        "baseline and is what fails CI.",
        "",
        "| Files | Decisions | Budget (ms) | Graph build (ms) | Median (ms) | P95 (ms) | Soft budget | Vs baseline |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in report.dimensions:
        baseline = (
            f"{d.baseline_median_ms:.1f} ({d.regression})"
            if d.baseline_median_ms is not None
            else "none recorded"
        )
        lines.append(
            f"| {d.files} | {d.decisions} | {d.budget_ms:.0f} | {d.graph_build_ms:.1f} | "
            f"{d.median_ms:.1f} | {d.p95_ms:.1f} | {d.soft_budget} | {baseline} |"
        )
    status = report.as_dict()["status"]
    lines += [
        "",
        f"Regressions: {status['regressions']}. Soft-budget misses: {status['soft_budget_misses']}.",
    ]
    return "\n".join(lines) + "\n"


def print_summary(report: MatrixReport) -> None:
    """Human-readable stdout summary; the exit code is the CI signal."""
    for d in report.dimensions:
        print(
            f"  files={d.files:<4} decisions={d.decisions:<4} "
            f"median={d.median_ms:8.2f} ms  budget={d.budget_ms:7.1f} ms  "
            f"soft_budget={d.soft_budget}  vs_baseline={d.regression}"
        )
    status: dict[str, Any] = report.as_dict()["status"]
    print(f"regressions={status['regressions']} soft_budget_misses={status['soft_budget_misses']}")
    for d in report.regressions:
        print(
            f"REGRESSION at files={d.files} decisions={d.decisions}: "
            f"median {d.median_ms:.1f} ms vs baseline "
            f"{d.baseline_median_ms:.1f} ms"
        )
