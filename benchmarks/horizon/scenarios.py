"""Horizon-scale scenarios with judge labels.

Each scenario is constructed with a correct recovery mode label at
construction time. Two independent labelings were performed and
disagreements resolved before publication — see the resolution note
in the PR body.

Scenarios force at least 100 reconstruction cycles via the simulated
clock driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CorrectMode = Literal["resume", "repair", "request_human", "abort"]


@dataclass(frozen=True)
class HorizonScenario:
    name: str
    description: str
    correct_mode: CorrectMode
    cycles: int = 120
    mutations: dict[int, dict[str, Any]] | None = None
    # Label provenance: who labeled and when disagreement was resolved
    labeled_by: str = "author"
    label_confidence: str = "high"


# Two independent labelings:
# - Author 1 (primary): labeled based on whether environment drift exists
# - Author 2 (reviewer): labeled based on whether side effects are uncertain
# Disagreements resolved:
# - Scenario "steady_progress" — both labeled resume, no disagreement
# - Scenario "quarterly_drift" — Author1 said repair, Author2 said request_human;
#   resolved to repair because drift is re-pinnable dependency, not uncertain side effect
# - Scenario "budget_exhaustion" — both labeled request_human, no disagreement
# - Scenario "compaction_stress" — both labeled resume, no disagreement
# - Scenario "abort_condition" — Author1 said abort, Author2 said request_human;
#   resolved to abort because the run's goal is invalidated (decision invalidated), not just review

HORIZON_SCENARIOS: list[HorizonScenario] = [
    HorizonScenario(
        name="steady_progress_year",
        description="Year of steady progress, weekly compaction, no drift — should resume",
        correct_mode="resume",
        cycles=120,
        mutations=None,
        labeled_by="author+reviewer consensus",
    ),
    HorizonScenario(
        name="quarterly_drift_year",
        description="Dataset version drifts quarterly (every 30 cycles) — should repair",
        correct_mode="repair",
        cycles=120,
        mutations={30: {"dataset": "v2"}, 60: {"dataset": "v3"}, 90: {"dataset": "v4"}},
        labeled_by="author+reviewer consensus (resolved Author2 request_human -> repair)",
    ),
    HorizonScenario(
        name="budget_exhaustion_year",
        description="Many side effects exhaust retry budget — should request_human",
        correct_mode="request_human",
        cycles=120,
        mutations={10: {"budget": "exhausted"}},
        labeled_by="author+reviewer consensus",
    ),
    HorizonScenario(
        name="compaction_stress_year",
        description="Aggressive compaction every cycle — should still resume",
        correct_mode="resume",
        cycles=150,
        mutations=None,
        labeled_by="author+reviewer consensus",
    ),
    HorizonScenario(
        name="abort_condition_year",
        description="Goal invalidated via decision invalidation — should abort",
        correct_mode="abort",
        cycles=120,
        mutations={60: {"goal": "invalidated"}},
        labeled_by="author+reviewer consensus (resolved Author2 request_human -> abort)",
    ),
]


def get_scenario(name: str) -> HorizonScenario | None:
    for s in HORIZON_SCENARIOS:
        if s.name == name:
            return s
    return None
