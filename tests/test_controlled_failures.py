from continuum.benchmark.controlled_failures import SCENARIOS, by_name
from continuum.models import RecoveryMode

# The ground-truth table is graded against the recovery engine, so every
# declared expectation has to be a mode the engine can actually propose.
# Asserting against the enum rather than a literal is what stops an
# impossible value from being blessed by the very test meant to catch it.
_VALID_MODES = {mode.name for mode in RecoveryMode}


def test_all_eleven_scenarios_declared() -> None:
    assert len(SCENARIOS) == 11
    names = {scenario.scenario for scenario in SCENARIOS}
    assert names == {
        "process_crash",
        "context_compaction",
        "tool_failure",
        "api_timeout",
        "dataset_change",
        "file_modification",
        "permission_change",
        "model_switch",
        "external_side_effect",
        "stale_decision",
        "partial_completion",
    }


def test_dataset_change_ground_truth() -> None:
    scenario = by_name("dataset_change")
    assert scenario.checkpoint_version == "v3"
    assert scenario.environment_version == "v4"
    assert scenario.expected == "REPAIR_AND_RESUME"


def test_each_scenario_has_ground_truth() -> None:
    for scenario in SCENARIOS:
        assert scenario.expected in _VALID_MODES, (
            f"{scenario.scenario!r} declares expected={scenario.expected!r}, "
            f"which is not a RecoveryMode member"
        )
        # A name the enum resolves too, so the harness can compare ground
        # truth straight against a RecoveryDecision.mode.
        RecoveryMode[scenario.expected]
        assert scenario.description


def test_recovered_rows_declare_real_modes() -> None:
    # Regression pin for the two rows that used to declare "RETRY", a name the
    # enum has never defined. Enum membership alone would still accept a
    # wrong-but-valid mode such as ABORT, so the values are named explicitly.
    assert by_name("tool_failure").expected == "RESUME"
    assert by_name("api_timeout").expected == "REQUEST_HUMAN"


def test_uncertain_side_effect_scenarios_agree() -> None:
    # Two rows describe the same shape, an effect that may have landed, so they
    # must ask for the same recovery. A drift between them would read as a
    # recovery regression once the table is graded against the engine.
    uncertain = {
        by_name("api_timeout").expected,
        by_name("external_side_effect").expected,
    }
    assert uncertain == {"REQUEST_HUMAN"}
