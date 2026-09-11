"""Controlled failure scenarios with ground truth (issue 10).

Eleven deterministic scenarios, each isolating one failure mode. Every
scenario declares its expected recovery behaviour upfront so the harness is
never grading against an assumption it also produced. The shapes follow
project.md, for example dataset_change has checkpoint_version v3,
environment_version v4, expected REPAIR_AND_RESUME.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ControlledScenario:
    """Describe one deterministic failure scenario and its expected recovery."""

    scenario: str
    checkpoint_version: str
    environment_version: str
    expected: str
    description: str


SCENARIOS: tuple[ControlledScenario, ...] = (
    ControlledScenario(
        scenario="process_crash",
        checkpoint_version="v1",
        environment_version="v1",
        expected="RESUME",
        description="Agent process killed after checkpoint, no environment change",
    ),
    ControlledScenario(
        scenario="context_compaction",
        checkpoint_version="v1",
        environment_version="v1",
        expected="RESUME",
        description="Transcript compacted after checkpoint, state still valid",
    ),
    ControlledScenario(
        scenario="tool_failure",
        checkpoint_version="v1",
        environment_version="v1",
        expected="RETRY",
        description="Tool returns error, retry is safe",
    ),
    ControlledScenario(
        scenario="api_timeout",
        checkpoint_version="v1",
        environment_version="v1",
        expected="RETRY",
        description="External API timed out, side effect uncertain",
    ),
    ControlledScenario(
        scenario="dataset_change",
        checkpoint_version="v3",
        environment_version="v4",
        expected="REPAIR_AND_RESUME",
        description="Pinned dataset moved, evidence derived from it is stale",
    ),
    ControlledScenario(
        scenario="file_modification",
        checkpoint_version="v2",
        environment_version="v3",
        expected="REPAIR_AND_RESUME",
        description="File watched by checkpoint was modified",
    ),
    ControlledScenario(
        scenario="permission_change",
        checkpoint_version="v1",
        environment_version="v1",
        expected="REQUEST_HUMAN",
        description="Credentials revoked, recovery requires human",
    ),
    ControlledScenario(
        scenario="model_switch",
        checkpoint_version="v1",
        environment_version="v2",
        expected="REPLAN",
        description="Model changed, goal assumptions invalidated",
    ),
    ControlledScenario(
        scenario="external_side_effect",
        checkpoint_version="v1",
        environment_version="v1",
        expected="REQUEST_HUMAN",
        description="Side effect outcome unknown after crash",
    ),
    ControlledScenario(
        scenario="stale_decision",
        checkpoint_version="v2",
        environment_version="v3",
        expected="REPAIR_AND_RESUME",
        description="Decision rests on stale evidence",
    ),
    ControlledScenario(
        scenario="partial_completion",
        checkpoint_version="v1",
        environment_version="v1",
        expected="RESUME",
        description="Some work done, checkpoint covers it, resume continues",
    ),
)


def by_name(name: str) -> ControlledScenario:
    """Look up a controlled failure scenario by name."""

    for scenario in SCENARIOS:
        if scenario.scenario == name:
            return scenario
    raise KeyError(f"unknown controlled scenario {name!r}")
