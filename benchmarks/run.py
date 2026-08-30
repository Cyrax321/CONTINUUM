"""Run the Phase 6 recovery-correctness scenario suite and emit a report.

Usage:
    uv run python benchmarks/run.py

Writes ``benchmarks/out/report.json`` and ``benchmarks/out/report.md``. The run
is reproducible: scenarios build their own in-memory state, so the output can be
diffed across commits to watch recovery guarantees hold.

Also runs the fault-injection chaos suite (#397) and emits its report
via the shared emitter schema coordinated with #398 (horizon).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure benchmarks is importable when run as `python benchmarks/run.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Any

from continuum.benchmark.phase6 import run_benchmark, scenarios, write_report


def _regenerate_readme_bench(horizon_report: Any, fault_report: Any | None = None) -> None:
    """Regenerate README bench section from real runner numbers (no invented numbers).

    Looks for markers <!-- BENCH:START --> and <!-- BENCH:END --> in README.md
    and replaces the content between them with a table derived from the
    horizon and fault-injection reports. If markers are missing, appends the
    section. Deleting the table and re-running `python benchmarks/run.py`
    regenerates it identically, proving no hand-edited numbers.
    """
    readme = Path(__file__).resolve().parent.parent / "README.md"
    if not readme.exists():
        return
    text = readme.read_text(encoding="utf-8")
    # Build table from real numbers
    h_summary = horizon_report.summary()
    lines: list[str] = []
    lines.append("<!-- BENCH:START -->")
    lines.append("### Horizon-scale benchmark (real runs, no invented numbers)")
    lines.append("")
    lines.append(
        f"Generated: {horizon_report.generated_at.isoformat()}  "
        f"Horizon scenarios: {h_summary.get('total', 0)}  "
        f"Passed: {h_summary.get('passed', 0)}  Failed: {h_summary.get('failed', 0)}"
    )
    lines.append("")
    # Six metrics from horizon
    acc = 0.0
    unnec = 0.0
    prec = 0.0
    dup_side = 0
    dup_work = 0.0
    comp = 0.0
    if horizon_report.results:
        acc = round(
            sum(r.metrics.get("accuracy", 0) for r in horizon_report.results)
            / len(horizon_report.results),
            3,
        )
        unnec = round(
            sum(
                r.metrics.get("unnecessary_human_escalation_rate", 0)
                for r in horizon_report.results
            )
            / len(horizon_report.results),
            3,
        )
        prec = round(
            sum(r.metrics.get("repair_precision", 0) for r in horizon_report.results)
            / len(horizon_report.results),
            3,
        )
        dup_side = sum(r.metrics.get("duplicate_side_effects", 0) for r in horizon_report.results)
        dup_work = round(
            sum(r.metrics.get("duplicate_work", 0) for r in horizon_report.results)
            / len(horizon_report.results),
            3,
        )
        comp = round(
            sum(r.metrics.get("compression_ratio", 0) for r in horizon_report.results)
            / len(horizon_report.results),
            3,
        )
    lines.append(
        f"Accuracy: {acc}  Unnecessary escalation: {unnec}  Repair precision: {prec}  Duplicate side effects: {dup_side}  Duplicate work: {dup_work}  Compression: {comp}"
    )
    lines.append("")
    lines.append("| Scenario | Cycles | Years | Correct | Actual | Accuracy |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in horizon_report.results:
        cycles = r.metrics.get("reconstruction_cycles", "")
        years = r.metrics.get("years_elapsed", "")
        correct = r.metrics.get("correct_mode", "")
        actual = r.metrics.get("actual_mode", "")
        a = r.metrics.get("accuracy", "")
        lines.append(f"| {r.scenario} | {cycles} | {years} | {correct} | {actual} | {a} |")
    if fault_report is not None:
        f_summary = fault_report.summary()
        lines.append("")
        lines.append(
            f"Fault-injection: {f_summary.get('total', 0)} scenarios, detection {f_summary.get('detection_rate', 0)}, unsafe {f_summary.get('unsafe_resume_rate', 0)}"
        )
    lines.append("<!-- BENCH:END -->")
    table = "\n".join(lines)
    if "<!-- BENCH:START -->" in text and "<!-- BENCH:END -->" in text:
        import re

        new_text = re.sub(r"<!-- BENCH:START -->.*<!-- BENCH:END -->", table, text, flags=re.DOTALL)
        readme.write_text(new_text, encoding="utf-8")
    else:
        # Append if no markers
        readme.write_text(text.rstrip() + "\n\n" + table + "\n", encoding="utf-8")


def main() -> None:
    report = run_benchmark(scenarios.ALL_SCENARIOS)
    out_dir = os.path.join(os.path.dirname(__file__), "out")
    os.makedirs(out_dir, exist_ok=True)
    json_path, md_path = write_report(report, os.path.join(out_dir, "report"))
    summary = report.summary()
    print(
        f"scenarios: {summary['total']}  passed: {summary['passed']}  failed: {summary['failed']}"
    )
    print(f"json: {json_path}")
    print(f"md:   {md_path}")

    # Fault-injection chaos suite (#397) — shares the emitter schema with #398
    try:
        from benchmarks.fault_injection.emitter import emit_fault_injection_report
        from benchmarks.fault_injection.runner import run_benchmark_suite

        fault_report = run_benchmark_suite()
        fault_json, fault_md = emit_fault_injection_report(
            fault_report, os.path.join(out_dir, "fault_injection_report")
        )
        fault_summary = fault_report.summary()
        print(
            f"fault-injection: {fault_summary['total']} passed={fault_summary['passed']} failed={fault_summary['failed']}"
        )
        print(f"fault json: {fault_json}")
        print(f"fault md:   {fault_md}")
    except Exception as exc:  # noqa: BLE001 - don't let fault suite break phase6
        print(f"fault-injection benchmark failed: {exc}")
        fault_report = None

    # Horizon-scale suite (#398) — years of simulated time, judge-scored
    try:
        from benchmarks.horizon.emitter import emit_horizon_report
        from benchmarks.horizon.runner import run_horizon_suite

        horizon_report = run_horizon_suite()
        horizon_json, horizon_md = emit_horizon_report(
            horizon_report, os.path.join(out_dir, "horizon_report")
        )
        horizon_summary = horizon_report.summary()
        print(
            f"horizon: {horizon_summary['total']} passed={horizon_summary['passed']} failed={horizon_summary['failed']}"
        )
        print(f"horizon json: {horizon_json}")
        print(f"horizon md:   {horizon_md}")
        # Regenerate README bench section from real numbers (no invented numbers)
        _regenerate_readme_bench(
            horizon_report, fault_report if "fault_report" in locals() else None
        )
    except Exception as exc:  # noqa: BLE001 - don't let horizon break phase6
        print(f"horizon benchmark failed: {exc}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
