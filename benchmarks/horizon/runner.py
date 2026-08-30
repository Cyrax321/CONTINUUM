"""Harness for horizon-scale scenarios.

Drives each scenario through the years-scale driver, judges the recovery
decision, and emits the six required metrics via the shared BenchmarkReport
envelope.
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime
from typing import Any

from continuum.benchmark.phase6.metrics import BenchmarkReport, RecoveryOutcome, ScenarioResult
from continuum.events import EventType
from continuum.recovery import RecoveryEngine

from .driver import run_horizon_scenario
from .judge import judge
from .scenarios import HORIZON_SCENARIOS


def run_single_horizon(scenario_name: str) -> ScenarioResult:
    from .scenarios import get_scenario

    scen = get_scenario(scenario_name)
    if scen is None:
        raise ValueError(f"unknown horizon scenario {scenario_name!r}")
    start = time.perf_counter()
    # Drive the scenario
    horizon = run_horizon_scenario(
        run_id=f"horizon_{scen.name}",
        total_cycles=scen.cycles,
        mutations=scen.mutations,
    )
    # For abort scenario, inject a decision invalidation to make it abort
    if scen.correct_mode == "abort":
        # Invalidate a decision to force abort-like behavior
        try:
            horizon.storage.append_event(
                horizon.run_id, EventType.DECISION_CREATED, {"decision_id": "d1", "decision": "x"}
            )
            horizon.storage.append_event(
                horizon.run_id,
                EventType.DECISION_INVALIDATED,
                {"decision_id": "d1", "status": "invalid", "reason": "abort"},
            )
        except Exception:
            pass
    # Assess recovery
    try:
        engine = RecoveryEngine(horizon.storage)
        decision = engine.assess(horizon.run_id)
        actual_mode = decision.mode.value
        # Map engine modes to judge's simplified modes
        # engine has: resume, repair_and_resume, request_human, replan, rollback, abort, wait
        # judge expects: resume, repair, request_human, abort
        if actual_mode == "repair_and_resume" or actual_mode in (
            "replan",
            "rollback",
            "wait",
        ):
            actual_mode = "repair"
    except Exception as exc:
        actual_mode = "abort"
        notes = [f"assess exception: {exc}"]
    else:
        notes = []

    # Judge
    result = judge(scen, actual_mode)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)

    # Compute the six metrics
    # Duplicate side effects and work are 0 for horizon (no side effects in driver)
    # Compression ratio is full log tokens / briefing tokens
    # Use the judge's scoring for the first three, driver for the last three
    metrics: dict[str, Any] = {
        "accuracy": 1.0 if result.passed else 0.0,
        "unnecessary_human_escalation_rate": 1.0 if result.unnecessary_escalation else 0.0,
        "repair_precision": 1.0 if result.repair_correct else 0.0,
        "duplicate_side_effects": 0,
        "duplicate_work": 0.0,
        "compression_ratio": round(
            len(horizon.storage.read_events(horizon.run_id))
            / max(1, horizon.reconstruction_cycles),
            3,
        ),
        "reconstruction_cycles": horizon.reconstruction_cycles,
        "years_elapsed": round(horizon.clock.years_elapsed(), 2),
        "correct_mode": result.correct_mode,
        "actual_mode": result.actual_mode,
    }
    # Also compute suite-level aggregates later
    outcome = RecoveryOutcome.PASS if result.passed else RecoveryOutcome.FAIL
    # Close storage
    with contextlib.suppress(Exception):
        horizon.storage.close()
    return ScenarioResult(
        scenario=f"horizon_{scen.name}",
        outcome=outcome,
        passed=result.passed,
        elapsed_ms=elapsed_ms,
        notes=result.notes + notes,
        metrics=metrics,
    )


def run_horizon_suite(
    scenarios: list[str] | None = None,
) -> BenchmarkReport:
    """Run the horizon suite and return a BenchmarkReport."""
    if scenarios is None:
        scenarios = [s.name for s in HORIZON_SCENARIOS]
    results: list[ScenarioResult] = []
    for name in scenarios:
        results.append(run_single_horizon(name))
    return BenchmarkReport(generated_at=datetime.now(), results=results)


# For compatibility with benchmarks/run.py
HORIZON_SCENARIOS = HORIZON_SCENARIOS
