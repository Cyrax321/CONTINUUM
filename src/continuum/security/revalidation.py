"""Periodic revalidation scheduler (Extension 2).

CONTINUUM already revalidates semantic state against the environment at
crash/resume (proven against real SIGKILL sessions). This module adds a
scheduling path that invokes that *same* logic during a normal, uninterrupted
run: on a step interval and on app switch. No new comparison logic is written;
only a new trigger into the existing, verified recovery engine. See
docs/PROBLEM.md (Extension 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from continuum.recovery import RecoveryDecision, RecoveryEngine


class RevalidationTrigger(StrEnum):
    """Why a revalidation ran. ``CRASH_RESUME`` is the pre-existing path."""

    CRASH_RESUME = "crash_resume"
    STEP_INTERVAL = "step_interval"
    APP_SWITCH = "app_switch"


class RevalidationPolicy(BaseModel):
    """When to revalidate during an uninterrupted run."""

    model_config = ConfigDict(frozen=True)

    step_interval: int = 25  # revalidate every N steps; 0 disables the interval
    revalidate_on_app_switch: bool = True


@dataclass(frozen=True)
class RevalidationResult:
    """What a revalidation pass decided."""

    trigger: RevalidationTrigger
    decision: RecoveryDecision | None = None


def run_revalidation(
    trigger: RevalidationTrigger,
    *,
    storage: object,
    run_id: str,
    env: object | None = None,
) -> RecoveryDecision:
    """Reuse the existing crash/resume revalidation against the live world.

    This is the exact proven logic the recovery engine uses at resume; we only
    call it on a new schedule.
    """
    engine = RecoveryEngine(storage)  # type: ignore[arg-type]
    return engine.assess(run_id, current_environment=env)  # type: ignore[arg-type]


def maybe_revalidate(
    step_count: int,
    app_changed: bool,
    policy: RevalidationPolicy,
    *,
    storage: object | None = None,
    run_id: str | None = None,
    env: object | None = None,
) -> RevalidationResult | None:
    """Decide whether to revalidate now, and do it if a run is supplied.

    Returns ``None`` when no revalidation is due. When ``storage`` and
    ``run_id`` are given, the revalidation actually runs and the result carries
    the engine's decision; otherwise it returns the trigger that *would* fire
    so callers can assert scheduling without a live run.
    """
    trigger: RevalidationTrigger | None = None

    if app_changed and policy.revalidate_on_app_switch:
        trigger = RevalidationTrigger.APP_SWITCH
    elif policy.step_interval and step_count > 0 and step_count % policy.step_interval == 0:
        trigger = RevalidationTrigger.STEP_INTERVAL

    if trigger is None:
        return None

    decision = None
    if storage is not None and run_id is not None:
        decision = run_revalidation(trigger, storage=storage, run_id=run_id, env=env)

    return RevalidationResult(trigger=trigger, decision=decision)
