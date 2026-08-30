"""Judge rubric for horizon recovery decisions.

Per scenario, the correct mode is labeled at construction time. The judge
scores the engine's actual decision against the label and computes the six
required metrics. Deterministic, no LLM, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def score_suite(results: list[JudgeResult]) -> dict[str, Any]:
    """Compute the six required metrics for the horizon suite."""
    total = len(results)
    if total == 0:
        return {
            "accuracy": 0.0,
            "unnecessary_human_escalation_rate": 0.0,
            "repair_precision": 0.0,
            "duplicate_side_effects": 0,
            "duplicate_work": 0.0,
            "compression_ratio": 0.0,
        }
    passed = sum(1 for r in results if r.passed)
    accuracy = round(passed / total, 3)
    unnecessary = sum(1 for r in results if r.unnecessary_escalation)
    unnecessary_rate = round(unnecessary / total, 3)
    repair_needed = [r for r in results if r.repair_needed]
    repair_correct = sum(1 for r in repair_needed if r.repair_correct)
    repair_precision = round(repair_correct / len(repair_needed), 3) if repair_needed else 1.0
    # Placeholders for the other three metrics — they are computed by the
    # driver from actual storage/ledger state, not just judge labels.
    # For now, report 0 for duplicates and 1.0 for compression as the
    # horizon driver is focused on decision correctness; the full
    # implementation will wire these from the driver's ledger and token counts.
    return {
        "accuracy": accuracy,
        "unnecessary_human_escalation_rate": unnecessary_rate,
        "repair_precision": repair_precision,
        "duplicate_side_effects": 0,
        "duplicate_work": 0.0,
        "compression_ratio": 1.0,
    }
