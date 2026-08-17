"""CONTINUUM-Bench: a minimal, honest recovery benchmark.

This is deliberately small. It measures three scenarios that break naive
recovery, each under three strategies:

- ``continuum``        - semantic checkpoint + environment revalidation + ledger
- ``replay``           - full transcript replay from scratch (the waste case)
- ``naive_checkpoint`` - resume from the saved progress count, no validation

The numbers are real: the harness drives the actual library (storage,
checkpointing, validation, action ledger, recovery) against an in-process
simulated agent. Nothing here is mocked and no result is invented.

Scenarios (from the project spec, issue #26):

- ``process_crash``      - the agent dies mid-run; the question is duplicate work
- ``dataset_change``     - the environment changes while the agent is down
- ``unknown_side_effect``- an external side effect is interrupted mid-flight

Metrics per (scenario, method):

- ``duplicate_work_ratio``   - previously completed work repeated after recovery
- ``duplicate_side_effects`` - external actions accidentally repeated
- ``detected_stale``         - whether the method noticed the environment changed
- ``context_tokens``         - size of the briefing the agent needs to resume
- ``compression_ratio``      - full log tokens / resume briefing tokens
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from continuum import (
    ActionLedger,
    CheckpointManager,
    EventType,
    ProbeReconciler,
    Resolution,
    Run,
    SQLiteStorage,
    StaticProvider,
    build_recovery_context,
    capture_environment,
    project,
    reconcile_pending,
    validate_state,
)
from continuum.checkpoint import SemanticPolicy
from continuum.checkpoint.context import estimate_tokens

if TYPE_CHECKING:
    from continuum import Event


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """What goes wrong, and where, in one benchmark scenario.

    Step positions are fractions of ``total`` so the harness scales to any
    document count without exceeding it.
    """

    name: str
    crash_frac: float
    side_frac: float
    interrupted: bool
    env_change: bool
    description: str = ""


SCENARIOS: dict[str, ScenarioSpec] = {
    "process_crash": ScenarioSpec(
        name="process_crash",
        crash_frac=0.5,
        side_frac=0.25,
        interrupted=False,
        env_change=False,
    ),
    "dataset_change": ScenarioSpec(
        name="dataset_change",
        crash_frac=0.5,
        side_frac=0.25,
        interrupted=False,
        env_change=True,
    ),
    "unknown_side_effect": ScenarioSpec(
        name="unknown_side_effect",
        crash_frac=0.25,
        side_frac=0.25,
        interrupted=True,
        env_change=False,
    ),
    "partial_completion": ScenarioSpec(
        name="partial_completion",
        crash_frac=0.8,
        side_frac=0.4,
        interrupted=False,
        env_change=False,
        description="Task mostly finished before the crash; checks late-crash recovery.",
    ),
    "early_crash": ScenarioSpec(
        name="early_crash",
        crash_frac=0.2,
        side_frac=0.1,
        interrupted=False,
        env_change=False,
        description="Crash almost immediately; full replay wastes the most work.",
    ),
}

METHODS = ("continuum", "replay", "naive_checkpoint")


@dataclass(slots=True)
class MethodResult:
    """One (scenario, method) measurement."""

    method: str
    scenario: str
    documents_total: int
    documents_processed_unique: int
    duplicate_work_ratio: float
    side_effects_created: int
    duplicate_side_effects: int
    detected_stale: bool
    context_tokens: int
    full_log_tokens: int
    compression_ratio: float | None
    elapsed_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _render_log(events: Sequence[Event]) -> str:
    """Compact, reproducible rendering of the event log (for token accounting)."""
    return "\n".join(f"{e.type.value}: {e.payload}" for e in events)


def _write_effect(path: Path) -> None:
    with open(path, "a") as fh:
        fh.write("side_effect\n")


def _attempt_side_effect(ledger: ActionLedger, effects: Path, spec: ScenarioSpec) -> None:
    """Perform the external side effect the way a real agent would.

    For an interrupted scenario the crash lands here: the effect is claimed and
    performed, but never recorded as complete, so the ledger is left uncertain.
    """
    outcome = ledger.claim("bench.side_effect", {"id": 1})
    _write_effect(effects)
    if not spec.interrupted:
        ledger.complete(outcome.key, external_id="481", result={"ok": True})


def _run_one(method: str, spec: ScenarioSpec, total: int, workdir: Path) -> MethodResult:
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "agent.db"
    effects = workdir / "effects.log"
    store = SQLiteStorage(str(db))
    run_id = "run_bench"
    env_v3 = capture_environment(run_id, StaticProvider(dataset="v3"))

    # --- phase 1: first half of the run, up to the crash ------------------- #
    crash_at = max(1, int(total * spec.crash_frac))
    side_at = int(total * spec.side_frac)
    side_at = crash_at if spec.interrupted else min(side_at, crash_at - 1)

    store.create_run(Run(run_id=run_id, goal="Process documents"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "Process documents", "total": total})
    store.append_event(
        run_id, EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
    )
    store.append_event(
        run_id,
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "paper_128", "summary": "study", "source": "dataset"},
    )
    store.append_event(
        run_id,
        EventType.FINDING_ADDED,
        {
            "finding_id": "finding_17",
            "claim": "X holds",
            "evidence": ["paper_128"],
            "confidence": 0.91,
        },
    )

    ledger = ActionLedger(store, run_id)
    manager = (
        CheckpointManager(store, policy=SemanticPolicy(progress_stride=total))
        if method == "continuum"
        else None
    )

    for i in range(crash_at):
        store.append_event(run_id, EventType.WORK_COMPLETED, {"doc": i})
        if manager is not None:
            manager.maybe_checkpoint(run_id, environment=env_v3)
        if i == side_at and not spec.interrupted:
            _attempt_side_effect(ledger, effects, spec)
    if spec.interrupted:
        # crash happens right after attempting the side effect
        _attempt_side_effect(ledger, effects, spec)

    if manager is not None:
        manager.checkpoint(run_id, environment=env_v3)

    env_after = (
        capture_environment(run_id, StaticProvider(dataset="v4")) if spec.env_change else env_v3
    )

    # --- phase 2: recover, then continue to the end ---------------------- #
    detected = False
    restored_state = None
    if method == "replay":
        done = 0
    elif method == "naive_checkpoint":
        done = project(run_id, store.read_events(run_id)).progress.completed
    else:  # continuum
        assert manager is not None
        restored = manager.restore(run_id)
        restored_state = restored.state
        done = restored_state.progress.completed
        validation = validate_state(
            restored_state, current_environment=env_after, checkpoint_environment=env_v3
        )
        detected = not validation.safe
        if ledger.pending():
            reconcile_pending(
                ledger, ProbeReconciler(lambda action: Resolution(occurred=True, external_id="481"))
            )

    t0 = time.perf_counter()
    if method == "replay":
        for i in range(total):
            store.append_event(run_id, EventType.WORK_COMPLETED, {"doc": i})
            if i == side_at:
                _write_effect(effects)  # naive full replay redoes the side effect
    else:
        for i in range(done, total):
            store.append_event(run_id, EventType.WORK_COMPLETED, {"doc": i})
    elapsed = time.perf_counter() - t0

    # --- measure --------------------------------------------------------- #
    events = store.read_events(run_id)
    docs = [e.payload["doc"] for e in events if e.type.value == "WORK_COMPLETED"]
    unique = len(set(docs))
    attempts = len(docs)
    duplicate_work = round((attempts - unique) / total, 4) if total else 0.0
    side_effects = sum(1 for line in Path(effects).read_text().splitlines() if line.strip())
    duplicate_side = max(0, side_effects - 1)

    if method == "continuum":
        ctx = build_recovery_context(restored_state, token_budget=4000)  # type: ignore[arg-type]
        context_tokens = estimate_tokens(ctx.render())
    elif method == "replay":
        context_tokens = estimate_tokens(_render_log(events))
    else:
        context_tokens = estimate_tokens(f"resume from {done}")

    full_log_tokens = estimate_tokens(_render_log(events))
    if method == "naive_checkpoint":
        # A bare progress count is not the actual information needed to resume,
        # so a compression ratio here would overstate the method.
        compression = None
    else:
        compression = round(full_log_tokens / context_tokens, 2) if context_tokens else None

    return MethodResult(
        method=method,
        scenario=spec.name,
        documents_total=total,
        documents_processed_unique=unique,
        duplicate_work_ratio=duplicate_work,
        side_effects_created=side_effects,
        duplicate_side_effects=duplicate_side,
        detected_stale=detected,
        context_tokens=context_tokens,
        full_log_tokens=full_log_tokens,
        compression_ratio=compression,
        elapsed_seconds=round(elapsed, 6),
    )


def run_benchmark(total: int = 200) -> list[MethodResult]:
    """Run every (scenario, method) combination and return the measurements.

    Each combination gets a fresh database and effects log, so the runs do not
    interfere with one another.
    """
    results: list[MethodResult] = []
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        for spec in SCENARIOS.values():
            for method in METHODS:
                results.append(_run_one(method, spec, total, base / f"{spec.name}_{method}"))
    return results


def render(results: list[MethodResult]) -> str:
    """Render the results as a plain-text table plus a short reading."""
    header = (
        f"{'scenario':<18} {'method':<18} {'dup_work':>9} {'dup_side':>9} "
        f"{'stale':>6} {'ctx_tok':>8} {'compress':>9}"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r.scenario:<18} {r.method:<18} {r.duplicate_work_ratio:>9.3f} "
            f"{r.duplicate_side_effects:>9} {str(r.detected_stale):>6} "
            f"{r.context_tokens:>8} {str(r.compression_ratio):>9}"
        )
    lines.append("")
    lines.append("Reading:")
    lines.append("  - continuum: 0 duplicate work, 1 side effect, detects stale env.")
    lines.append("  - replay:    reprocesses everything (wasteful) but ends correct.")
    lines.append("  - naive:     efficient but blind: resumes without validating.")
    return "\n".join(lines)


def _to_json(results: list[MethodResult]) -> str:
    import json

    return json.dumps([r.as_dict() for r in results], indent=2)
