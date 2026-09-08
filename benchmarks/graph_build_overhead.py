"""Micro-benchmark: source DependencyGraph build time (#111).

Measures how long ``continuum.analysis.DependencyGraph`` takes to scan a
synthetic repository of N Python files. Pure measurement: it reports timings
for several sizes and makes no claim about caching or production scale. The
result informs whether a caching layer is worth adding (a separate decision).

See issue #111.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

from continuum.analysis import DependencyGraph


def _make_repo(root: Path, n_files: int) -> None:
    for i in range(n_files):
        f = root / f"module_{i:04d}.py"
        f.write_text("import os\nimport sys\nimport numpy\nimport pandas\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``benchmarks/graph_build_overhead.py`` argument parser.

    Only ``--help`` is recognised; anything else fails with exit code 2
    (argparse convention) instead of silently launching the measurement
    (issue #773).
    """
    return argparse.ArgumentParser(
        prog="python benchmarks/graph_build_overhead.py",
        description=(
            "Micro-benchmark for source DependencyGraph build time over a "
            "synthetic repository. Reports timings for several sizes; makes "
            "no claim about caching or production scale."
        ),
        epilog="With no arguments, runs the measurement for 50, 100, and 200 files.",
    )


def main() -> None:
    build_parser().parse_args()
    print("Source DependencyGraph build overhead (synthetic fixture, no caching claim)")
    for n in (50, 100, 200):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            _make_repo(root, n)
            start = time.perf_counter()
            graph = DependencyGraph(root)
            elapsed = time.perf_counter() - start
        print(
            f"  files={n:<4} build={elapsed * 1000:8.2f} ms  "
            f"declared={len(graph.declared)} files_scanned={len(graph._file_imports)}"
        )


if __name__ == "__main__":
    main()
