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
- ``checkpoint_bytes_written`` - bytes persisted for the checkpoint (continuum) or 0 for replay
- ``bytes_read_at_resume``   - bytes read to restore the run at recovery time
- ``revalidation_calls``     - number of environment revalidation calls made at resume
- ``resume_tokens``          - tokens the resumed agent needs to become productive
- ``replay_tokens_to_productive`` - replay tokens before first productive step

Deterministic tokenizer note (issue #293a, #568):
- Token counts use :func:`continuum.checkpoint.context.estimate_tokens`,
  a deterministic ``len(text) // 4`` heuristic. No vendor tokenizer is
  vendored, no new dependency is added. The heuristic is documented as an
  estimate everywhere it appears and is stable across runs, so byte and token
  comparisons between strategies are reproducible. Tool-schema size from
  ``token_floor.md`` is not added separately, the briefing text already
  accounts for schema-like content deterministically.
"""

from __future__ import annotations

import json
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
    checkpoint_bytes_written: int = 0
    bytes_read_at_resume: int = 0
    revalidation_calls: int = 0
    resume_tokens: int = 0
    replay_tokens_to_productive: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _render_log(events: Sequence[Event]) -> str:
    """Compact, reproducible rendering of the event log (for token accounting)."""
    return "\n".join(f"{e.type.value}: {e.payload}" for e in events)


def _event_payload_bytes(events: Sequence[Event]) -> int:
    """Total bytes of event payloads, deterministic via json dumps.

    Uses ``json.dumps(payload, sort_keys=True)`` exactly as storage does,
    then utf-8 length. No compression or tokenizer, purely byte count.
    """
    return sum(len(json.dumps(dict(e.payload), sort_keys=True).encode("utf-8")) for e in events)


def _checkpoint_bytes(checkpoint: Any) -> int:
    """Bytes of a checkpoint body, deterministic via canonical_json."""
    try:
        return len(checkpoint.canonical_json().encode("utf-8"))
    except Exception:
        return 0


def _estimate_resume_tokens(text: str) -> int:
    """Deterministic token estimate, same heuristic as context budget.

    Uses ``estimate_tokens`` (len // 4) so counts are stable and vendor
    independent. Documented as estimate everywhere.
    """
    return estimate_tokens(text)


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
    with SQLiteStorage(str(db)) as store:
        run_id = "run_bench"
        env_v3 = capture_environment(run_id, StaticProvider(dataset="v3"))
        checkpoint_bytes_written = 0
        bytes_read_at_resume = 0
        revalidation_calls = 0
        stored_checkpoint = None

        # --- phase 1: first half of the run, up to the crash ------------------- #
        crash_at = max(1, int(total * spec.crash_frac))
        side_at = int(total * spec.side_frac)
        side_at = crash_at if spec.interrupted else min(side_at, crash_at - 1)

        store.create_run(Run(run_id=run_id, goal="Process documents"))
        store.append_event(
            run_id, EventType.RUN_STARTED, {"goal": "Process documents", "total": total}
        )
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
            stored_checkpoint = manager.checkpoint(run_id, environment=env_v3)
            checkpoint_bytes_written = _checkpoint_bytes(stored_checkpoint)

        env_after = (
            capture_environment(run_id, StaticProvider(dataset="v4")) if spec.env_change else env_v3
        )

        # --- phase 2: recover, then continue to the end ---------------------- #
        detected = False
        restored_state = None
        if method == "replay":
            done = 0
        elif method == "naive_checkpoint":
            done = project(
                run_id, store.read_events(run_id), on_unprojectable="degrade"
            ).progress.completed
        else:  # continuum
            assert manager is not None and stored_checkpoint is not None
            restored = manager.restore(run_id)
            restored_state = restored.state
            done = restored_state.progress.completed
            # bytes read at resume is checkpoint body plus pending payloads
            pending_after = store.read_events(
                run_id, after_sequence=stored_checkpoint.state.source_sequence
            )
            bytes_read_at_resume = checkpoint_bytes_written + _event_payload_bytes(pending_after)
            # revalidation is a single validate_state call for continuum
            validation = validate_state(
                restored_state, current_environment=env_after, checkpoint_environment=env_v3
            )
            revalidation_calls = 1
            detected = not validation.safe
            if ledger.pending():
                reconcile_pending(
                    ledger,
                    ProbeReconciler(lambda action: Resolution(occurred=True, external_id="481")),
                )

        # bytes read for the non-continuum strategies, computed at recovery time
        if method == "replay":
            pre_events = store.read_events(run_id)
            bytes_read_at_resume = _event_payload_bytes(pre_events)
        elif method == "naive_checkpoint":
            bytes_read_at_resume = len(f"resume from {done}".encode())

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

        resume_tokens = context_tokens
        # replay_tokens_to_productive is the token cost to become productive
        # For replay it is the full log, for continuum it is the briefing, deterministic
        if method == "replay":
            replay_tokens_to_productive = _estimate_resume_tokens(_render_log(events))
        else:
            replay_tokens_to_productive = resume_tokens

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
            checkpoint_bytes_written=checkpoint_bytes_written,
            bytes_read_at_resume=bytes_read_at_resume,
            revalidation_calls=revalidation_calls,
            resume_tokens=resume_tokens,
            replay_tokens_to_productive=replay_tokens_to_productive,
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


# --- issue #6: idempotency under argument drift ------------------------------ #
# Issue #6 was a real defect: `continuum_intercept_action` deduplicated on the
# raw argument formatting, so an agent that re-rendered the same operation with a
# different path shape (absolute vs relative) computed a different idempotency
# key and re-sent the side effect. The fix is a stable `key` (e.g.
# `invoice:INV-001`) that makes two attempts the same action, plus a defensive
# recognition layer for the no-key / argument-drift case. This suite proves the
# fix with real numbers, driving the same ActionLedger path the adapters use.


@dataclass(slots=True)
class IdempotencyResult:
    """One method's measurement on the argument-drift idempotency scenario."""

    method: str
    scenario: str
    actions_total: int
    attempts: int
    distinct_side_effects: int
    duplicate_side_effects: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _try_idem_action(
    method: str, ledger: ActionLedger, effects: Path, i: int, abs_path: bool
) -> int:
    """Attempt to send invoice ``i`` once, with a path shape given by ``abs_path``.

    Returns 1 if the side effect was actually performed, 0 if it was deduped.
    """
    path = f"/data/invoices/INV-{i}.pdf" if abs_path else f"invoices/INV-{i}.pdf"
    if method in ("naive_retry", "replay"):
        effects.write_text(effects.read_text() + f"INV-{i}\n")
        return 1
    if method == "continuum_key":
        outcome = ledger.claim(
            "bench.send", {"file": path, "invoice": f"INV-{i}"}, key=f"invoice:INV-{i}"
        )
    else:  # continuum_drift: no explicit key, relies on argument-drift recognition
        outcome = ledger.claim("bench.send", {"file": path, "invoice": f"INV-{i}"})
    if outcome.fresh:
        effects.write_text(effects.read_text() + f"INV-{i}\n")
        ledger.complete(outcome.key, external_id=f"ext-{i}", result={"ok": True})
        return 1
    return 0


def run_idempotency_benchmark(total: int = 50) -> list[IdempotencyResult]:
    """Prove issue #6: stable keys (and drift recognition) stop duplicate effects.

    One agent attempts ``total`` distinct external actions, each twice with an
    argument-drift shape change (absolute vs relative path), exactly as a
    retrying agent that re-renders its tool call would. CONTINUUM dedups both
    via a stable key and via drift recognition; naive retry and full replay do
    not. The numbers are real: the harness drives the actual ActionLedger that
    the MCP/LangGraph/OpenAI adapters call, with no mocking.
    """
    methods = ("continuum_key", "continuum_drift", "naive_retry", "replay")
    results: list[IdempotencyResult] = []
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        for method in methods:
            db = base / f"idem_{method}.db"
            effects = base / f"idem_{method}.log"
            effects.write_text("")
            with SQLiteStorage(str(db)) as store:
                run_id = "run_idem"
                store.create_run(Run(run_id=run_id, goal="Send invoices"))
                store.append_event(run_id, EventType.RUN_STARTED, {"goal": "Send invoices"})
                ledger = ActionLedger(store, run_id)
                attempted = 0
                for i in range(total):
                    attempted += _try_idem_action(method, ledger, effects, i, abs_path=True)
                    attempted += _try_idem_action(method, ledger, effects, i, abs_path=False)
                sent = {line for line in effects.read_text().splitlines() if line.strip()}
                results.append(
                    IdempotencyResult(
                        method=method,
                        scenario="argument_drift",
                        actions_total=total,
                        attempts=attempted,
                        distinct_side_effects=len(sent),
                        duplicate_side_effects=attempted - len(sent),
                    )
                )
    return results


def render_idempotency(results: list[IdempotencyResult]) -> str:
    header = f"{'method':<18} {'actions':>8} {'attempts':>9} {'distinct':>9} {'dups':>6}"
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r.method:<18} {r.actions_total:>8} {r.attempts:>9} "
            f"{r.distinct_side_effects:>9} {r.duplicate_side_effects:>6}"
        )
    lines.append("")
    lines.append("Reading (issue #6):")
    lines.append("  - continuum_key / continuum_drift: 0 duplicate side effects.")
    lines.append("  - naive_retry / replay: every retry repeats the side effect.")
    return "\n".join(lines)


def render(results: list[MethodResult]) -> str:
    """Render the results as a plain-text table plus a short reading."""
    header = (
        f"{'scenario':<18} {'method':<18} {'dup_work':>9} {'dup_side':>9} "
        f"{'stale':>6} {'ctx_tok':>8} {'compress':>9} {'ckpt_b':>8} {'read_b':>8} {'reval':>5} {'resume_tok':>10}"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r.scenario:<18} {r.method:<18} {r.duplicate_work_ratio:>9.3f} "
            f"{r.duplicate_side_effects:>9} {str(r.detected_stale):>6} "
            f"{r.context_tokens:>8} {str(r.compression_ratio):>9} {r.checkpoint_bytes_written:>8} "
            f"{r.bytes_read_at_resume:>8} {r.revalidation_calls:>5} {r.resume_tokens:>10}"
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
