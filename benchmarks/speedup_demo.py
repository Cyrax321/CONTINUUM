"""Demo of the 186 to 187 speedup without an LLM.

Simulates the five part guide with the old per section tool path versus the
new async hook path. No model is called, only the checkpoint and ledger
machinery, so the numbers show the machinery cost in isolation. The real
world re measurement with Claude Code will show the larger token cost delta.
"""

from __future__ import annotations

import argparse

import time

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.hooks import make_async_auto_checkpoint_hook, make_auto_checkpoint_hook
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _seed(storage: SQLiteStorage) -> None:
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})


def run_per_section_sync() -> float:
    storage = SQLiteStorage(":memory:")
    _seed(storage)
    manager = CheckpointManager(storage)
    hook = make_auto_checkpoint_hook(manager, "run_1")
    start = time.perf_counter()
    for i in range(5):
        storage.append_event(
            "run_1",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": f"e{i}", "summary": "s", "source": "d"},
        )
        hook()
    return time.perf_counter() - start


def run_async_single() -> float:
    storage = SQLiteStorage(":memory:")
    _seed(storage)
    manager = CheckpointManager(storage)
    hook = make_async_auto_checkpoint_hook(manager, "run_1")
    for i in range(5):
        storage.append_event(
            "run_1",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": f"e{i}", "summary": "s", "source": "d"},
        )
    start = time.perf_counter()
    hook()
    elapsed = time.perf_counter() - start
    time.sleep(0.2)
    return elapsed


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Compare per-section sync checkpoints vs a single async checkpoint.",
        epilog="With no arguments, runs the timing demo and prints the summary.",
    )


def main() -> None:
    build_parser().parse_args()
    per_section = run_per_section_sync()
    async_single = run_async_single()
    print(f"per section sync (5 checkpoints): {per_section * 1000:.1f} ms")
    print(f"async single at end:            {async_single * 1000:.1f} ms")
    if async_single > 0:
        print(f"ratio: {per_section / async_single:.1f}x")


if __name__ == "__main__":
    main()
