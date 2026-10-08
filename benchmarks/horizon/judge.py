"""Judge rubric for horizon recovery decisions.

Per scenario, the correct mode is labeled at construction time. The judge
scores the engine's actual decision against the label. The six required
metrics are computed by the runner from real storage and ledger state, not
here: a judge that could produce them from labels alone would make them
constants. Deterministic, no LLM, no network.
"""

from __future__ import annotations

from dataclasses import dataclass

from .scenarios import HorizonScenario


@dataclass(frozen=True)
class JudgeLabel:
    scenario: str
    correct_mode: str
    labeled_by: str


@dataclass
class JudgeResult:
    scenario: str
    correct_mode: str
    actual_mode: str
    passed: bool
    unnecessary_escalation: bool
    repair_needed: bool
    repair_correct: bool
    notes: list[str]


def judge(scenario: HorizonScenario, actual_mode: str) -> JudgeResult:
    """Score one scenario's actual mode against its label.

    Metrics:
    - passed: actual == correct
    - unnecessary_escalation: actual is request_human/abort when correct is resume/repair
    - repair_correct: for repair scenarios, whether actual correctly demanded repair
    """
    correct = scenario.correct_mode
    passed = actual_mode == correct
    # Unnecessary escalation: escalated to human when not needed
    escalation_modes = {"request_human", "abort"}
    unnecessary = actual_mode in escalation_modes and correct not in escalation_modes
    # Repair precision: for scenarios that need repair, did we get repair?
    repair_needed = correct in ("repair", "request_human", "abort")
    repair_correct = (actual_mode == "repair") if correct == "repair" else True
    if correct == "repair" and actual_mode != "repair":
        repair_correct = False

    notes: list[str] = []
    if not passed:
        notes.append(f"expected {correct}, got {actual_mode}")
    if unnecessary:
        notes.append("unnecessary human escalation")
    return JudgeResult(
        scenario=scenario.name,
        correct_mode=correct,
        actual_mode=actual_mode,
        passed=passed,
        unnecessary_escalation=unnecessary,
        repair_needed=repair_needed,
        repair_correct=repair_correct,
        notes=notes,
    )
