"""CLI entry: ``python -m benchmarks.latency_matrix`` (issue #766).

With no arguments it runs the matrix, writes the report under ``benchmarks/out/``,
and exits non-zero when a point regresses against the committed baseline. A
soft-budget miss is printed and never fails the run.

``--update-baseline`` is the only thing that rewrites ``baseline.json``: it
adopts the measured medians as the new expectation and says so, so the change
lands as a reviewable file diff. ``--list`` prints the grid and exits rather than
silently launching a measurement, matching ``benchmarks/run.py`` (issue #682).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Importable when run as `python -m benchmarks.latency_matrix` without an
# install, mirroring benchmarks/run.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.latency_matrix import baseline as baseline_mod  # noqa: E402
from benchmarks.latency_matrix import emitter, runner  # noqa: E402

EXIT_OK = 0
EXIT_REGRESSION = 1

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "out" / "latency_matrix_report"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.latency_matrix",
        description=(
            "Recovery latency-regression matrix: measures RecoveryEngine.assess_scoped "
            "across a deterministic grid of repository sizes and decision counts, "
            "derives the soft SLO budget per point, and compares medians against the "
            "committed baseline. Observational only; it never changes recovery behavior."
        ),
        epilog=(
            "With no arguments, runs the default matrix and exits non-zero on a "
            "regression against the checked-in baseline."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the matrix points and the tolerance policy, then exit.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=runner.DEFAULT_SAMPLES,
        help=f"timed assessments per point (default {runner.DEFAULT_SAMPLES}).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=runner.DEFAULT_WARMUP,
        help=f"untimed assessments before sampling (default {runner.DEFAULT_WARMUP}).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(DEFAULT_OUT),
        help="report output prefix; .json and .md are written beside it.",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help=(
            "rewrite benchmarks/latency_matrix/baseline.json with the measured "
            "medians. This is how expected values change; a normal run never writes it."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        print("latency-matrix points (files, decisions), in dimension order:")
        for files, decisions in runner.DEFAULT_MATRIX:
            budget = runner.soft_budget_ms(files, decisions)
            print(f"  files={files:<4} decisions={decisions:<4} budget={budget:7.1f} ms")
        tolerance = baseline_mod.Tolerance.from_env()
        print("tolerance:", tolerance.as_dict()["policy"])
        print(f"  regression_factor={tolerance.regression_factor}")
        print(f"  absolute_floor_ms={tolerance.absolute_floor_ms}")
        return EXIT_OK

    report = runner.run_matrix(samples=args.samples, warmup=args.warmup)
    json_path, md_path = emitter.emit_report(report, Path(args.out))
    emitter.print_summary(report)
    print(f"json: {json_path}")
    print(f"md:   {md_path}")

    if args.update_baseline:
        path = baseline_mod.write_baseline(report)
        print(
            f"baseline rewritten: {path}\n"
            "Commit this file; its diff is the review record for the new "
            "expectations. A normal run never modifies it."
        )
        # Adopting new numbers is not a failure, even where the old baseline was
        # breached: the update is the decision, and failing here would train
        # callers to drop the flag to get a green run.
        return EXIT_OK

    if report.regressions:
        for d in report.regressions:
            print(
                f"regression at files={d.files} decisions={d.decisions}: "
                f"median {d.median_ms:.1f} ms vs baseline {d.baseline_median_ms:.1f} ms. "
                "Fix the regression, or refresh the expectation with "
                "--update-baseline and justify the change in the commit message."
            )
        return EXIT_REGRESSION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
