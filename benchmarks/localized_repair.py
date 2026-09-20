"""Synthetic benchmark: operations preserved by localized repair (#106).

Builds a tiny multi-dependency task (3 dependencies, 2 clean, 1 that later
corrupts) and measures how many state items stay VALID under a *scoped*
(localized) recovery assessment versus a naive global reset, which by definition
preserves nothing.

This is a SYNTHETIC measurement only. It runs against an in-memory store and
the real recovery engine; it does not claim any external-world numbers. See
issue #106.
"""

from __future__ import annotations

import argparse

from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage


def _build() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="synthetic repair task"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    # Three dependencies, all pinned at v1.
    for dep in ("dataset", "model", "cache"):
        storage.append_event(
            "run_1", EventType.DEPENDENCY_DECLARED, {"resource": dep, "version": "v1"}
        )
    # Evidence each tied to one dependency via its `source`.
    storage.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "e1", "summary": "s1", "source": "dataset"},
    )
    storage.append_event(
        "run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "e2", "summary": "s2", "source": "model"}
    )
    storage.append_event(
        "run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "e3", "summary": "s3", "source": "cache"}
    )
    # Three findings, each citing a distinct evidence item.
    for fid, eid in (("f1", "e1"), ("f2", "e2"), ("f3", "e3")):
        storage.append_event(
            "run_1",
            EventType.FINDING_ADDED,
            {"finding_id": fid, "claim": "holds", "evidence": [eid]},
        )
    # Three decisions, each citing a distinct finding.
    for did, fid in (("d1", "f1"), ("d2", "f2"), ("d3", "f3")):
        storage.append_event(
            "run_1",
            EventType.DECISION_CREATED,
            {"decision_id": did, "decision": "act", "evidence": [fid]},
        )
    clean_env = capture("run_1", StaticProvider(dataset="v1", model="v1", cache="v1"))
    CheckpointManager(storage).checkpoint("run_1", environment=clean_env)
    return storage


def _preserved(decision) -> int:
    state = decision.validation.state
    items = list(state.evidence) + list(state.findings) + list(state.decisions)
    return sum(1 for it in items if it.status.value == "valid")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``benchmarks/localized_repair.py`` argument parser.

    Only ``--help`` is recognised; anything else fails with exit code 2
    (argparse convention) instead of silently launching the measurement
    (issue #773).
    """
    return argparse.ArgumentParser(
        prog="python benchmarks/localized_repair.py",
        description=(
            "Synthetic localized-repair measurement: compare how many state "
            "items stay VALID under a scoped recovery assessment versus a "
            "naive global reset. Runs against an in-memory store; no external "
            "world claims."
        ),
        epilog="With no arguments, runs the measurement and prints the summary.",
    )


def main() -> None:
    build_parser().parse_args()
    storage = _build()
    engine = RecoveryEngine(storage)

    # Corrupt only `dataset` (v1 -> v2); the other two stay clean.
    corrupt_env = capture("run_1", StaticProvider(dataset="v2", model="v1", cache="v1"))

    # Localized repair: confine the assessment to the broken dependency.
    localized = engine.assess_scoped("run_1", ["dataset"], current_environment=corrupt_env)
    preserved_localized = _preserved(localized)

    # Naive global reset preserves nothing by definition (it discards the bundle).
    preserved_global = 0

    total_items = (
        len(localized.validation.state.evidence)
        + len(localized.validation.state.findings)
        + len(localized.validation.state.decisions)
    )
    print("Synthetic localized-repair measurement (no external-world claims)")
    print("  dependencies:        3 (1 corrupted: dataset v1->v2)")
    print(f"  state items:        {total_items}")
    print(f"  preserved (local):  {preserved_localized}")
    print(f"  preserved (global): {preserved_global}  (naive reset discards the bundle)")
    print(
        f"  local keeps {preserved_localized}/{total_items} "
        f"({100 * preserved_localized / total_items:.0f}%) of work"
    )


if __name__ == "__main__":
    main()
