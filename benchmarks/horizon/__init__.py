"""Horizon-scale scenarios with simulated years and a recovery-decision judge.

Public API for the horizon benchmark. The suite drives hundreds of
compaction/archive/briefing cycles per scenario under a simulated clock
decoupled from wall clock, then scores the recovery decision against a
pre-labeled correct mode.
"""

from .driver import SimulatedClock, run_horizon_scenario
from .judge import JudgeLabel, judge
from .runner import HORIZON_SCENARIOS, run_horizon_suite
from .scenarios import HorizonScenario

__all__ = [
    "HorizonScenario",
    "JudgeLabel",
    "SimulatedClock",
    "judge",
    "run_horizon_scenario",
    "run_horizon_suite",
    "HORIZON_SCENARIOS",
]
