"""Long-horizon scalability and stress benchmark for CONTINUUM.

Measures resource growth, latency profiles, memory footprints, and compaction
effectiveness across progressively larger workloads (1,000, 10,000, 100,000 steps).

Mixture of operations:
- 25% Agent decisions (DECISION_CREATED)
- 20% Tool calls (TOOL_CALLED, TOOL_COMPLETED)
- 15% Findings and evidence (FINDING_ADDED, EVIDENCE_ADDED)
- 10% Plan updates (TASK_UPDATED, WORK_ADDED)
- 10% Checkpoints (CheckpointManager.checkpoint)
- 10% External actions (ActionLedger claim/complete)
- 5% Validation (StateValidator.validate, STATE_VALIDATED)
- 5% Recovery and risk events (RISK_OBSERVED, RECOVERY_STARTED, RECOVERY_COMPLETED)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import sys
import tempfile
import time
import tracemalloc
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Ensure continuum package is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from continuum.actions.ledger import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import EnvResource, Run, SemanticState
from continuum.state.semantic import project_incremental
from continuum.state.validator import StateValidator
from continuum.storage.sqlite import SQLiteStorage


class MemoryTracker:
    """Measures process RSS (WorkingSetSize) and peak working set via OS APIs."""

    def __init__(self) -> None:
        self.is_windows = sys.platform == "win32"
        self._setup_windows_counters()

    def _setup_windows_counters(self) -> None:
        if not self.is_windows:
            return

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_uint32),
                ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        self._pmc_type = PROCESS_MEMORY_COUNTERS_EX
        self._k32 = ctypes.windll.kernel32
        self._psapi = ctypes.windll.psapi
        self._k32.GetCurrentProcess.restype = ctypes.c_void_p
        self._psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            ctypes.c_uint32,
        ]
        self._psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        self._process_handle = self._k32.GetCurrentProcess()

    def sample_rss_bytes(self) -> int:
        if self.is_windows:
            pmc = self._pmc_type()
            pmc.cb = ctypes.sizeof(pmc)
            ret = self._psapi.GetProcessMemoryInfo(
                self._process_handle, ctypes.byref(pmc), ctypes.sizeof(pmc)
            )
            if ret:
                return int(pmc.WorkingSetSize)
        return 0

    def sample_peak_rss_bytes(self) -> int:
        if self.is_windows:
            pmc = self._pmc_type()
            pmc.cb = ctypes.sizeof(pmc)
            ret = self._psapi.GetProcessMemoryInfo(
                self._process_handle, ctypes.byref(pmc), ctypes.sizeof(pmc)
            )
            if ret:
                return int(pmc.PeakWorkingSetSize)
        return 0


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    s = sorted(values)
    n = len(s)
    p50 = s[int(n * 0.50)]
    p95 = s[min(int(n * 0.95), n - 1)]
    p99 = s[min(int(n * 0.99), n - 1)]
    pmax = s[-1]
    pmean = sum(s) / n
    return {
        "p50": round(p50, 3),
        "p95": round(p95, 3),
        "p99": round(p99, 3),
        "max": round(pmax, 3),
        "mean": round(pmean, 3),
    }


@dataclass
class BenchmarkMetrics:
    target_steps: int
    executed_steps: int
    events_count: int
    active_events_count: int
    archived_events_count: int
    event_payload_bytes: int
    checkpoints_count: int
    checkpoint_bytes_total: int
    checkpoint_bytes_avg: int
    latest_checkpoint_bytes: int
    db_file_bytes: int
    wal_file_bytes: int
    total_disk_bytes: int
    rss_mb: float
    peak_rss_mb: float
    tracemalloc_current_mb: float
    tracemalloc_peak_mb: float
    process_cpu_seconds: float
    wall_seconds: float
    steps_per_second: float
    append_latency_ms: dict[str, float]
    checkpoint_latency_ms: dict[str, float]
    restore_latency_ms: float
    replay_latency_ms: float
    validation_latency_ms: float
    compaction_runs: int
    compacted: bool
    compaction_duration_ms: float = 0.0
    compaction_archived_total: int = 0


def run_workload(
    target_steps: int,
    db_dir: Path,
    compact_interval: int = 0,
    run_id_prefix: str = "stress_run",
) -> BenchmarkMetrics:
    """Execute long-horizon workload for target_steps, measuring all metrics."""
    tracemalloc.start()
    tracemalloc.reset_peak()
    mem_tracker = MemoryTracker()

    db_path = db_dir / f"{run_id_prefix}_{target_steps}.db"
    if db_path.exists():
        with suppress(OSError):
            db_path.unlink()
    wal_path = Path(str(db_path) + "-wal")
    if wal_path.exists():
        with suppress(OSError):
            wal_path.unlink()

    run_id = f"{run_id_prefix}_{target_steps}"

    storage = SQLiteStorage(str(db_path))
    try:
        storage.create_run(Run(run_id=run_id, goal=f"Scalability test with {target_steps} steps"))

        # Initial anchor events
        storage.append_event(
            run_id,
            EventType.RUN_STARTED,
            {"goal": "Scalability stress test", "total": target_steps},
        )
        storage.append_event(
            run_id,
            EventType.DEPENDENCY_DECLARED,
            {"resource": "dataset://core_corpus", "version": "v1.0.0"},
        )
        storage.append_event(
            run_id,
            EventType.DEPENDENCY_DECLARED,
            {"resource": "model://primary_engine", "version": "v2.5"},
        )

        env = capture(
            run_id,
            StaticProvider(
                resources={
                    "dataset://core_corpus": EnvResource(
                        name="dataset://core_corpus", version="v1.0.0"
                    ),
                    "model://primary_engine": EnvResource(
                        name="model://primary_engine", version="v2.5"
                    ),
                }
            ),
        )

        manager = CheckpointManager(storage)
        ledger = ActionLedger(storage, run_id)
        validator = StateValidator()

        # Initial baseline checkpoint
        initial_ckpt = manager.checkpoint(run_id, environment=env, reason="initial_anchor")
        current_state: SemanticState = initial_ckpt.state

        append_latencies: list[float] = []
        checkpoint_latencies: list[float] = []
        compaction_times: list[float] = []
        total_archived = 0
        compaction_count = 0

        pending_events_for_fold: list[Any] = []
        checkpoint_bytes_list: list[int] = [len(initial_ckpt.canonical_json().encode("utf-8"))]

        t_wall_start = time.perf_counter()
        t_cpu_start = time.process_time()

        # Exact 100-step distribution:
        # 25% decisions (DECISION_CREATED)
        # Realistic 20-step interleaved mixture:
        # 25% decisions (slots 0, 1, 2, 3, 4 -> 5/20)
        # 20% tools (slots 5, 6, 7, 8 -> 4/20)
        # 10% checkpoint 1 (slot 9 -> 1/20)
        # 15% findings/evidence (slots 10, 11, 12 -> 3/20)
        # 10% plan updates (slots 13, 14 -> 2/20)
        # 10% external actions (slots 15, 16 -> 2/20)
        # 5% validation (slot 17 -> 1/20)
        # 5% recovery events (slot 18 -> 1/20)
        # 10% checkpoint 2 (slot 19 -> 1/20)
        last_claimed_key: str | None = None
        for step in range(1, target_steps + 1):
            slot = step % 20

            t_app0 = time.perf_counter()
            ev = None

            if slot in (0, 1, 2, 3, 4):
                # 25% Agent Decisions
                dec_id = f"dec_{step}"
                ev = storage.append_event(
                    run_id,
                    EventType.DECISION_CREATED,
                    {
                        "decision_id": dec_id,
                        "decision": f"Agent decision regarding task milestone {step}",
                        "reason": f"Optimal policy choice at step {step}",
                        "evidence": [],
                    },
                )
                pending_events_for_fold.append(ev)

            elif slot in (5, 6, 7, 8):
                # 20% Tool calls
                tool_idx = step // 2
                if slot % 2 == 1:
                    ev = storage.append_event(
                        run_id,
                        EventType.TOOL_CALLED,
                        {
                            "tool": "corpus_search",
                            "call_id": f"call_{tool_idx}",
                            "arguments": {"query": f"term_{step}", "limit": 10},
                        },
                    )
                else:
                    ev = storage.append_event(
                        run_id,
                        EventType.TOOL_COMPLETED,
                        {
                            "tool": "corpus_search",
                            "call_id": f"call_{tool_idx}",
                            "result": {"matches": 3, "score": 0.94},
                        },
                    )

            elif slot in (10, 11, 12):
                # 15% Findings and Evidence
                if slot % 2 == 0:
                    ev = storage.append_event(
                        run_id,
                        EventType.EVIDENCE_ADDED,
                        {
                            "evidence_id": f"ev_{step}",
                            "summary": f"Observed document verification at step {step}",
                            "source": "dataset://core_corpus",
                        },
                    )
                else:
                    ev = storage.append_event(
                        run_id,
                        EventType.FINDING_ADDED,
                        {
                            "finding_id": f"find_{step}",
                            "claim": f"Hypothesis verified for partition {step}",
                            "evidence": [f"ev_{step - 1}"],
                            "confidence": 0.92,
                        },
                    )
                pending_events_for_fold.append(ev)

            elif slot in (13, 14):
                # 10% Plan Updates (WORK_ADDED and TASK_UPDATED)
                if slot == 13:
                    ev = storage.append_event(
                        run_id,
                        EventType.WORK_ADDED,
                        {
                            "task_id": f"task_{step}",
                            "description": f"Pending work task {step}",
                        },
                    )
                else:
                    ev = storage.append_event(
                        run_id,
                        EventType.TASK_UPDATED,
                        {
                            "completed": step // 10,
                            "pending": max(target_steps - (step // 10), 0),
                            "total": target_steps,
                        },
                    )
                pending_events_for_fold.append(ev)

            elif slot in (15, 16):
                # 10% External Actions (ActionLedger)
                action_idx = step // 2
                if slot == 15:
                    outcome = ledger.claim(
                        "service.external_sync",
                        {"partition": action_idx, "ts": step},
                    )
                    last_claimed_key = str(outcome.key)
                else:
                    if last_claimed_key is not None:
                        ledger.complete(
                            last_claimed_key,
                            external_id=f"ext_{action_idx}",
                            result={"ok": True},
                        )
                        last_claimed_key = None
                    else:
                        outcome = ledger.claim(
                            "service.external_sync",
                            {"partition": action_idx, "ts": step},
                        )
                        ledger.complete(
                            str(outcome.key),
                            external_id=f"ext_{action_idx}",
                            result={"ok": True},
                        )

            elif slot == 17:
                # 5% Validation
                validator.validate(
                    current_state,
                    current_environment=env,
                    checkpoint_environment=env,
                    checkpoint_version=current_state.version,
                )
                ev = storage.append_event(
                    run_id,
                    EventType.STATE_VALIDATED,
                    {"version": current_state.version, "safe": True},
                )

            elif slot == 18:
                # 5% Recovery and Risk Events
                if step % 2 == 0:
                    ev = storage.append_event(
                        run_id,
                        EventType.RISK_OBSERVED,
                        {
                            "trigger": "drift_sensor",
                            "score": 0.15,
                            "detail": f"Divergence check at step {step}",
                        },
                    )
                else:
                    ev = storage.append_event(
                        run_id,
                        EventType.RECOVERY_STARTED,
                        {"trigger": "routine_audit", "step": step},
                    )

            elif slot in (9, 19):
                # 10% Checkpoints (spaced 10 steps apart)
                if pending_events_for_fold:
                    current_state, _ = project_incremental(
                        run_id, pending_events_for_fold, base=current_state
                    )
                    pending_events_for_fold.clear()

                t_ckpt0 = time.perf_counter()
                ckpt = manager.checkpoint(
                    run_id,
                    state=current_state,
                    reason=f"step_{step}_checkpoint",
                    environment=env,
                    force_version=True,
                )
                t_ckpt1 = time.perf_counter()
                checkpoint_latencies.append((t_ckpt1 - t_ckpt0) * 1000.0)
                checkpoint_bytes_list.append(len(ckpt.canonical_json().encode("utf-8")))

            t_app1 = time.perf_counter()
            append_latencies.append((t_app1 - t_app0) * 1000.0)

            # Compaction triggered at specified interval
            if compact_interval > 0 and step % compact_interval == 0 and step < target_steps:
                if pending_events_for_fold:
                    current_state, _ = project_incremental(
                        run_id, pending_events_for_fold, base=current_state
                    )
                    pending_events_for_fold.clear()

                t_cmp0 = time.perf_counter()
                res = storage.compact_run(run_id, environment=env)
                t_cmp1 = time.perf_counter()
                compaction_times.append((t_cmp1 - t_cmp0) * 1000.0)
                compaction_count += 1
                total_archived += res.get("archived", 0)

            # Periodic progress reporting
            if target_steps >= 1000 and step % 1000 == 0:
                print(
                    f"    ... step {step:,}/{target_steps:,} ({step / target_steps * 100:.0f}%)",
                    flush=True,
                )

        # Final wrap-up
        if pending_events_for_fold:
            current_state, _ = project_incremental(
                run_id, pending_events_for_fold, base=current_state
            )
            pending_events_for_fold.clear()

        # Final checkpoint
        t_ckpt0 = time.perf_counter()
        final_ckpt = manager.checkpoint(
            run_id,
            state=current_state,
            reason="workload_completion",
            environment=env,
            force_version=True,
        )
        t_ckpt1 = time.perf_counter()
        checkpoint_latencies.append((t_ckpt1 - t_ckpt0) * 1000.0)
        checkpoint_bytes_list.append(len(final_ckpt.canonical_json().encode("utf-8")))

        t_wall_end = time.perf_counter()
        t_cpu_end = time.process_time()

        wall_duration = t_wall_end - t_wall_start
        cpu_duration = t_cpu_end - t_cpu_start

        # Measure restore latency
        t_res0 = time.perf_counter()
        restored = manager.restore(run_id, replay=True)
        t_res1 = time.perf_counter()
        restore_latency_ms = (t_res1 - t_res0) * 1000.0

        # Measure replay latency (reading pending tail)
        t_rep0 = time.perf_counter()
        cursor = manager._cursor_for(final_ckpt)
        _ = storage.read_events(run_id, after_sequence=cursor)
        t_rep1 = time.perf_counter()
        replay_latency_ms = (t_rep1 - t_rep0) * 1000.0

        # Measure validation latency
        t_val0 = time.perf_counter()
        _ = validator.validate(
            restored.state,
            current_environment=env,
            checkpoint_environment=env,
            checkpoint_version=final_ckpt.version,
        )
        t_val1 = time.perf_counter()
        validation_latency_ms = (t_val1 - t_val0) * 1000.0

        # File and memory measurements
        db_size = db_path.stat().st_size if db_path.exists() else 0
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        total_disk = db_size + wal_size

        rss_bytes = mem_tracker.sample_rss_bytes()
        peak_rss_bytes = mem_tracker.sample_peak_rss_bytes()
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Read events counts
        all_events = storage.read_all_events(run_id)
        live_events = storage.read_events(run_id)
        archived_events = storage.read_archived_events(run_id)
        payload_bytes_total = sum(
            len(json.dumps(dict(e.payload), sort_keys=True).encode("utf-8")) for e in all_events
        )

        return BenchmarkMetrics(
            target_steps=target_steps,
            executed_steps=target_steps,
            events_count=len(all_events),
            active_events_count=len(live_events),
            archived_events_count=len(archived_events),
            event_payload_bytes=payload_bytes_total,
            checkpoints_count=len(checkpoint_bytes_list),
            checkpoint_bytes_total=sum(checkpoint_bytes_list),
            checkpoint_bytes_avg=int(sum(checkpoint_bytes_list) / len(checkpoint_bytes_list)),
            latest_checkpoint_bytes=checkpoint_bytes_list[-1],
            db_file_bytes=db_size,
            wal_file_bytes=wal_size,
            total_disk_bytes=total_disk,
            rss_mb=round(rss_bytes / (1024 * 1024), 2),
            peak_rss_mb=round(peak_rss_bytes / (1024 * 1024), 2),
            tracemalloc_current_mb=round(traced_current / (1024 * 1024), 2),
            tracemalloc_peak_mb=round(traced_peak / (1024 * 1024), 2),
            process_cpu_seconds=round(cpu_duration, 3),
            wall_seconds=round(wall_duration, 3),
            steps_per_second=round(target_steps / max(wall_duration, 0.001), 1),
            append_latency_ms=_percentiles(append_latencies),
            checkpoint_latency_ms=_percentiles(checkpoint_latencies),
            restore_latency_ms=round(restore_latency_ms, 3),
            replay_latency_ms=round(replay_latency_ms, 3),
            validation_latency_ms=round(validation_latency_ms, 3),
            compaction_runs=compaction_count,
            compacted=compact_interval > 0,
            compaction_duration_ms=round(sum(compaction_times), 3),
            compaction_archived_total=total_archived,
        )
    finally:
        storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CONTINUUM Long-Horizon Scalability Benchmark")
    parser.add_argument(
        "--steps",
        type=str,
        default="1000,10000,100000",
        help="Comma-separated target steps (e.g. 1000,10000,100000)",
    )
    parser.add_argument(
        "--compact-interval",
        type=int,
        default=500,
        help="Interval for log compaction (0 to disable)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="benchmarks/out/scalability_benchmark_results.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--run-uncompacted-comparison",
        action="store_true",
        default=True,
        help="Also run uncompacted workloads to measure compaction effectiveness",
    )
    args = parser.parse_args()

    steps_list = [int(s.strip()) for s in args.steps.split(",") if s.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "benchmark": "long_horizon_scalability",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "compacted_runs": [],
        "uncompacted_runs": [],
        "comparisons": [],
    }

    base_temp = tempfile.mkdtemp(prefix="continuum_scalability_")
    work_dir = Path(base_temp)
    try:
        print("=" * 80)
        print("CONTINUUM LONG-HORIZON SCALABILITY BENCHMARK")
        print(f"Target Workloads: {steps_list}")
        print(f"Compaction Interval: {args.compact_interval} steps")
        print("=" * 80)

        # 1. Run Compacted Workloads
        print("\n--- PHASE 1: COMPACTED WORKLOADS ---", flush=True)
        for target in steps_list:
            print(f"\n[RUNNING] Compacted workload: {target:,} steps...", flush=True)
            metrics = run_workload(
                target_steps=target,
                db_dir=work_dir,
                compact_interval=args.compact_interval,
                run_id_prefix="compacted",
            )
            results["compacted_runs"].append(asdict(metrics))
            print(
                f"[DONE {target:,} steps] Wall: {metrics.wall_seconds}s | CPU: {metrics.process_cpu_seconds}s | "
                f"Rate: {metrics.steps_per_second} ops/s | DB: {metrics.db_file_bytes / 1024:.1f} KB | "
                f"Peak RSS: {metrics.peak_rss_mb} MB | Restore: {metrics.restore_latency_ms} ms",
                flush=True,
            )

        # 2. Run Uncompacted Workloads (for 1,000 and 10,000 steps, plus 100,000 if requested)
        if args.run_uncompacted_comparison:
            print(
                "\n--- PHASE 2: UNCOMPACTED WORKLOADS (COMPACTION EFFECTIVENESS BASELINE) ---",
                flush=True,
            )
            for target in steps_list:
                print(f"\n[RUNNING] Uncompacted workload: {target:,} steps...", flush=True)
                metrics = run_workload(
                    target_steps=target,
                    db_dir=work_dir,
                    compact_interval=0,
                    run_id_prefix="uncompacted",
                )
                results["uncompacted_runs"].append(asdict(metrics))
                print(
                    f"[DONE {target:,} steps] Wall: {metrics.wall_seconds}s | CPU: {metrics.process_cpu_seconds}s | "
                    f"Rate: {metrics.steps_per_second} ops/s | DB: {metrics.db_file_bytes / 1024:.1f} KB | "
                    f"Peak RSS: {metrics.peak_rss_mb} MB | Restore: {metrics.restore_latency_ms} ms",
                    flush=True,
                )

        # 3. Compute Compaction Effectiveness Comparisons
        print("\n--- PHASE 3: COMPACTION EFFECTIVENESS ANALYSIS ---")
        for c_run in results["compacted_runs"]:
            target = c_run["target_steps"]
            matching_u = next(
                (u for u in results["uncompacted_runs"] if u["target_steps"] == target), None
            )
            if matching_u:
                disk_saving_pct = round(
                    (1.0 - (c_run["total_disk_bytes"] / max(matching_u["total_disk_bytes"], 1)))
                    * 100,
                    2,
                )
                restore_speedup = round(
                    matching_u["restore_latency_ms"] / max(c_run["restore_latency_ms"], 0.001), 2
                )
                comparison = {
                    "target_steps": target,
                    "uncompacted_active_events": matching_u["active_events_count"],
                    "compacted_active_events": c_run["active_events_count"],
                    "compacted_archived_events": c_run["archived_events_count"],
                    "uncompacted_disk_bytes": matching_u["total_disk_bytes"],
                    "compacted_disk_bytes": c_run["total_disk_bytes"],
                    "disk_savings_percent": disk_saving_pct,
                    "uncompacted_restore_latency_ms": matching_u["restore_latency_ms"],
                    "compacted_restore_latency_ms": c_run["restore_latency_ms"],
                    "restore_speedup_x": restore_speedup,
                    "uncompacted_peak_rss_mb": matching_u["peak_rss_mb"],
                    "compacted_peak_rss_mb": c_run["peak_rss_mb"],
                }
                results["comparisons"].append(comparison)
                print(
                    f"[{target:,} steps comparison] Active events: {c_run['active_events_count']} (compacted) vs {matching_u['active_events_count']} (uncompacted) | "
                    f"Disk: {c_run['total_disk_bytes'] / 1024:.1f} KB vs {matching_u['total_disk_bytes'] / 1024:.1f} KB | "
                    f"Restore speedup: {restore_speedup}x"
                )

        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\n[SAVED] Benchmark results written to {out_path}")
    finally:
        shutil.rmtree(base_temp, ignore_errors=True)


if __name__ == "__main__":
    main()
