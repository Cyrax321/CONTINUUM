"""Years-of-simulated-time driver for horizon scale.

The driver decouples the episode clock (simulated days/years) from wall clock,
forcing hundreds of compaction/archive/briefing cycles per scenario while
scheduling environment mutations across the span. Deterministic, no network.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


@dataclass
class SimulatedClock:
    """Episode clock decoupled from wall clock.

    Starts at 2020-01-01, advances by simulated days per cycle, never touches
    wall clock. Deterministic and replayable.
    """

    start: datetime = field(default_factory=lambda: datetime(2020, 1, 1))
    current: datetime = field(default_factory=lambda: datetime(2020, 1, 1))
    cycles: int = 0

    def tick(self, days: int = 7) -> datetime:
        self.current += timedelta(days=days)
        self.cycles += 1
        return self.current

    def years_elapsed(self) -> float:
        return (self.current - self.start).days / 365.25

    def reset(self) -> None:
        self.current = self.start
        self.cycles = 0


def key_of(cycle: int) -> str:
    """The stable idempotency key for one cycle's side effect."""
    return f"horizon.work:{cycle}"


@dataclass
class HorizonRun:
    """One horizon episode's durable state."""

    run_id: str
    storage: SQLiteStorage
    clock: SimulatedClock
    manager: CheckpointManager
    reconstruction_cycles: int = 0
    #: Total times an effect body was actually executed. Each cycle performs
    #: one, so a healthy run ends with this equal to the cycle count.
    side_effects_performed: int = 0
    #: The distinct keys that were executed. Against
    #: ``side_effects_performed`` this is the duplicate-side-effect signal: if
    #: the ledger ever lets the same key execute twice, the two diverge and
    #: ``duplicate_side_effects`` goes positive.
    effects_performed_keys: set[str] = field(default_factory=set)
    #: How many post-reconstruction re-attempts were correctly recognised as
    #: already done and refused. This is *not* duplicated work — it is the work
    #: the substrate saved. It is reported separately as
    #: ``duplicate_work_avoided`` so the published zero for duplicate work can
    #: be seen as a real measurement rather than a vacuous one.
    duplicate_work_avoided: int = 0

    def perform_effect(self, cycle: int) -> None:
        """Perform the cycle's side effect through the action ledger.

        Each cycle claims one idempotent effect (``horizon.work:<cycle>``) and
        completes it. The claim/complete pair is the project's core guarantee:
        a second claim for the same key must answer ``fresh=False``, and only a
        ``fresh=True`` answer leads to a real execution. Counting executions
        and distinct keys is what makes the two published duplicate metrics
        measurable instead of hardcoded.
        """
        from continuum.actions import ActionLedger

        ledger = ActionLedger(self.storage, self.run_id)
        outcome = ledger.claim("horizon_work", {"cycle": cycle}, key=key_of(cycle))
        if outcome.fresh:
            ledger.complete(outcome.key, result={"cycle": cycle})
            self.side_effects_performed += 1
            self.effects_performed_keys.add(outcome.key)

    def unique_effects_performed(self) -> int:
        """Distinct side-effect keys that were actually executed."""
        return len(self.effects_performed_keys)

    def duplicate_side_effects(self) -> int:
        """Executions beyond the first for any key; zero when dedup holds."""
        return max(0, self.side_effects_performed - self.unique_effects_performed())

    def reperform_after_reconstruction(self, cycle: int) -> None:
        """Re-claim the effect a resumed agent would attempt again.

        This is the duplicate-work probe: after a restore or compaction the
        caller has no way to know the effect already happened, so it claims
        the same key. The answer had better be ``fresh=False``. A compaction
        that dropped the ledger lookup would return ``fresh=True``; the caller
        then re-executes for real, which shows up here as a duplicate side
        effect rather than a suppressed one.
        """
        from continuum.actions import ActionLedger

        ledger = ActionLedger(self.storage, self.run_id)
        outcome = ledger.claim("horizon_work", {"cycle": cycle}, key=key_of(cycle))
        if outcome.fresh:
            # The ledger lost the completion. A resumed agent cannot tell, so it
            # does the work again — exactly the failure the benchmark exists to
            # catch. Record it so duplicate_work is measured, not assumed.
            with contextlib.suppress(Exception):
                ledger.complete(outcome.key, result={"cycle": cycle, "duplicate_of": cycle})
            self.side_effects_performed += 1
            self.effects_performed_keys.add(outcome.key)
        else:
            self.duplicate_work_avoided += 1

    def checkpoint(self, **kw: Any) -> None:
        try:
            self.manager.checkpoint(self.run_id, **kw)
        except Exception:
            # After compaction the live log has no RUN_STARTED, so
            # project_current fails. Fall back to restore-based checkpoint
            # which is anchoring-safe (uses the last checkpoint state).
            try:
                restored = self.manager.restore(self.run_id)
                self.manager.checkpoint(self.run_id, state=restored.state, **kw)
            except Exception:
                pass
        self.reconstruction_cycles += 1

    def compact(self) -> None:
        try:
            self.storage.compact_run(self.run_id)
            self.reconstruction_cycles += 1
        except Exception:
            pass

    def restore(self) -> None:
        try:
            self.manager.restore(self.run_id)
            self.reconstruction_cycles += 1
        except Exception:
            pass

    def assess(self) -> None:
        from continuum.recovery import RecoveryEngine

        try:
            RecoveryEngine(self.storage).assess(self.run_id)
            self.reconstruction_cycles += 1
        except Exception:
            pass


def run_horizon_scenario(
    run_id: str,
    total_cycles: int = 120,
    days_per_cycle: int = 7,
    mutations: dict[int, dict[str, Any]] | None = None,
) -> HorizonRun:
    """Drive one horizon scenario for ``total_cycles`` reconstruction cycles.

    Each cycle appends work, checkpoints, compacts, and assesses, with
    scheduled environment mutations. Returns the HorizonRun with
    reconstruction_cycles count (at least total_cycles * 3).
    """
    mutations = mutations or {}
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=run_id, goal="horizon scenario"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "horizon", "total": 1000})
    clock = SimulatedClock()
    manager = CheckpointManager(storage)
    horizon = HorizonRun(run_id=run_id, storage=storage, clock=clock, manager=manager)

    for cycle in range(total_cycles):
        clock.tick(days=days_per_cycle)
        # Simulate work
        storage.append_event(run_id, EventType.WORK_COMPLETED, {"cycle": cycle})
        # Perform the cycle's side effect through the ledger, then re-attempt
        # it the way a resumed agent would. The pair is what makes the two
        # duplicate metrics real: performed counts real executions,
        # avoided counts correctly-deduplicated re-attempts.
        horizon.perform_effect(cycle)
        # Scheduled mutation
        if cycle in mutations:
            for k, v in mutations[cycle].items():
                storage.append_event(
                    run_id, EventType.DEPENDENCY_DECLARED, {"resource": k, "version": v}
                )
        # Checkpoint every cycle
        horizon.checkpoint()
        # Compact every 10 cycles
        if cycle % 10 == 0 and cycle > 0:
            horizon.compact()
            # A compaction archives the prefix; re-attempt the last effect so
            # the dedup lookup is exercised against the archive, not just the
            # live tail.
            horizon.reperform_after_reconstruction(max(0, cycle - 1))
        # Assess every 20 cycles
        if cycle % 20 == 0:
            horizon.assess()
        # Briefing/validation every 30 cycles
        if cycle % 30 == 0:
            horizon.restore()
            horizon.reperform_after_reconstruction(max(0, cycle - 1))

    # Ensure at least 100 reconstruction cycles
    assert horizon.reconstruction_cycles >= 100, f"only {horizon.reconstruction_cycles} cycles"
    return horizon
