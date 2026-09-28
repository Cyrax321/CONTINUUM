"""Escalation policy loading and action risk scoring (issue #1409).

Covers the three things the sub-issue ships: the ``.continuum/escalation.json``
schema, its fail-closed loader, and the deterministic scorer the deferred
review queue (#1410) and fatigue telemetry (#1411) will consume. Nothing here
touches the event log, so the tests are pure and fast like the module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.recovery.escalation import (
    ACTION_TYPE_WEIGHT_FALLBACK,
    DEFAULT_ESCALATION_POLICY,
    DEFAULT_ESCALATION_POLICY_PATH,
    ActionRisk,
    EscalationPolicyError,
    evaluate_action_risk,
    load_escalation_policy,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_absent_policy_falls_back_to_the_documented_defaults(tmp_path: Path) -> None:
    """No file means the fail-safe posture, not an error and not silence."""
    policy = load_escalation_policy(tmp_path / "absent.json")
    assert policy == DEFAULT_ESCALATION_POLICY
    assert policy["hourly_prompt_cap"] == 10
    assert policy["batch_window_seconds"] == 3600
    assert policy["blast_radius_threshold"] == 0.8
    # The default weight is 0.0 so an unrecognised action is never escalated
    # on its own; a policy that names no weights escalates nothing.
    assert policy["risk_weights"] == {ACTION_TYPE_WEIGHT_FALLBACK: 0.0}


def test_the_shipped_example_loads(tmp_path: Path) -> None:
    """The example we ship is valid against the loader that documents it."""
    example = Path(__file__).parent.parent / "examples" / "escalation-policy.json"
    assert example.exists()
    policy = load_escalation_policy(example)
    assert policy["hourly_prompt_cap"] == 10
    assert policy["risk_weights"]["mem_delete"] == 0.9


def test_a_partial_file_keeps_the_defaults_for_the_keys_it_omits(tmp_path: Path) -> None:
    policy = load_escalation_policy(
        _write(tmp_path / "escalation.json", json.dumps({"blast_radius_threshold": 0.5}))
    )
    assert policy["blast_radius_threshold"] == 0.5
    assert policy["hourly_prompt_cap"] == DEFAULT_ESCALATION_POLICY["hourly_prompt_cap"]
    assert policy["batch_window_seconds"] == DEFAULT_ESCALATION_POLICY["batch_window_seconds"]


def test_the_loaded_weights_are_a_copy_not_the_module_default(tmp_path: Path) -> None:
    """Mutating a loaded policy cannot leak into the next caller."""
    policy = load_escalation_policy(tmp_path / "absent.json")
    policy["risk_weights"]["default"] = 1.0
    assert DEFAULT_ESCALATION_POLICY["risk_weights"]["default"] == 0.0


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '"a string"', "null"])
def test_malformed_or_non_object_policy_fails_closed(tmp_path: Path, body: str) -> None:
    with pytest.raises(EscalationPolicyError):
        load_escalation_policy(_write(tmp_path / "bad.json", body))


@pytest.mark.parametrize("cap", [0, -1, 10.0, True, "10", None])
def test_an_hourly_prompt_cap_outside_its_contract_is_rejected(tmp_path: Path, cap: object) -> None:
    with pytest.raises(EscalationPolicyError, match="hourly_prompt_cap"):
        load_escalation_policy(
            _write(tmp_path / "bad.json", json.dumps({"hourly_prompt_cap": cap}))
        )


@pytest.mark.parametrize("window", [-1, 60.5, True, "3600"])
def test_a_negative_or_non_integer_batch_window_is_rejected(tmp_path: Path, window: object) -> None:
    with pytest.raises(EscalationPolicyError, match="batch_window_seconds"):
        load_escalation_policy(
            _write(tmp_path / "bad.json", json.dumps({"batch_window_seconds": window}))
        )


@pytest.mark.parametrize("threshold", [-0.1, 1.1, True, "0.8", None])
def test_a_blast_radius_threshold_outside_the_unit_interval_is_rejected(
    tmp_path: Path, threshold: object
) -> None:
    with pytest.raises(EscalationPolicyError, match="blast_radius_threshold"):
        load_escalation_policy(
            _write(tmp_path / "bad.json", json.dumps({"blast_radius_threshold": threshold}))
        )


def test_a_threshold_at_either_end_of_the_interval_is_accepted(tmp_path: Path) -> None:
    """0.0 and 1.0 are the operator's explicit choices, not out-of-range."""
    for value in (0.0, 1.0):
        policy = load_escalation_policy(
            _write(tmp_path / "escalation.json", json.dumps({"blast_radius_threshold": value}))
        )
        assert policy["blast_radius_threshold"] == value


@pytest.mark.parametrize("weights", ["not an object", 5, [0.5]])
def test_risk_weights_must_be_an_object(tmp_path: Path, weights: object) -> None:
    with pytest.raises(EscalationPolicyError, match="risk_weights"):
        load_escalation_policy(_write(tmp_path / "bad.json", json.dumps({"risk_weights": weights})))


@pytest.mark.parametrize("score", [-0.01, 1.01, True, "0.5"])
def test_a_weight_outside_the_unit_interval_is_rejected(tmp_path: Path, score: object) -> None:
    with pytest.raises(EscalationPolicyError, match="risk_weights"):
        load_escalation_policy(
            _write(
                tmp_path / "bad.json",
                json.dumps({"risk_weights": {"mem_delete": score}}),
            )
        )


def test_an_empty_weight_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(EscalationPolicyError, match="non-empty strings"):
        load_escalation_policy(
            _write(tmp_path / "bad.json", json.dumps({"risk_weights": {"": 0.5}}))
        )


def test_a_weight_key_is_trimmed_but_not_case_folded(tmp_path: Path) -> None:
    """Action types are registry identifiers: whitespace is noise, case is not."""
    policy = load_escalation_policy(
        _write(tmp_path / "escalation.json", json.dumps({"risk_weights": {" mem_write ": 0.4}}))
    )
    assert "mem_write" in policy["risk_weights"]


def test_unknown_top_level_keys_are_ignored_so_the_schema_can_extend(tmp_path: Path) -> None:
    policy = load_escalation_policy(
        _write(tmp_path / "escalation.json", json.dumps({"future_field": {"x": 1}}))
    )
    assert "future_field" not in policy


# -- scoring ------------------------------------------------------------------- #


@pytest.fixture
def policy() -> dict[str, object]:
    return {
        "hourly_prompt_cap": 5,
        "batch_window_seconds": 600,
        "blast_radius_threshold": 0.8,
        "risk_weights": {
            "default": 0.2,
            "mem_write": 0.4,
            "pgvector": 0.9,
            "mem_delete": 1.0,
        },
    }


def test_an_action_type_weight_is_used_and_scored_below_threshold(
    policy: dict[str, object],
) -> None:
    risk = evaluate_action_risk("mem_write", {"record_key": "k"}, policy)
    assert risk == ActionRisk(score=0.4, immediate=False)


def test_a_score_at_the_threshold_is_immediate(policy: dict[str, object]) -> None:
    """The boundary is inclusive: exactly the threshold interrupts at once."""
    assert evaluate_action_risk("mem_delete", {}, policy).immediate is True


def test_an_unrecognised_action_falls_back_to_the_default_weight(policy: dict[str, object]) -> None:
    assert evaluate_action_risk("unknown_action", {}, policy).score == 0.2


def test_a_resource_class_in_the_arguments_can_raise_the_score(policy: dict[str, object]) -> None:
    """An unweighted action whose resource class is weighted takes that weight."""
    risk = evaluate_action_risk("unweighted", {"resource_class": "pgvector"}, policy)
    assert risk.score == 0.9
    assert risk.immediate is True


def test_the_more_dangerous_classification_wins_when_both_apply(policy: dict[str, object]) -> None:
    """A low generic weight must never dull a high specific one."""
    risk = evaluate_action_risk("mem_write", {"resource_class": "pgvector"}, policy)
    assert risk.score == 0.9


def test_the_action_type_wins_when_its_weight_is_the_higher_one(policy: dict[str, object]) -> None:
    risk = evaluate_action_risk("mem_delete", {"resource_class": "pgvector"}, policy)
    assert risk.score == 1.0


def test_an_unrecognised_resource_class_is_ignored(policy: dict[str, object]) -> None:
    risk = evaluate_action_risk("mem_write", {"resource_class": "untrusted_store"}, policy)
    assert risk.score == 0.4


def test_a_policy_without_a_default_scores_unknown_actions_zero() -> None:
    """Fail-safe floor: nothing unrecognised is ever escalated on its own."""
    strict = {"blast_radius_threshold": 0.5, "risk_weights": {"mem_delete": 0.9}}
    assert evaluate_action_risk("unknown", {}, strict) == ActionRisk(score=0.0, immediate=False)


def test_a_zero_threshold_makes_everything_immediate() -> None:
    """The operator's explicit choice, not a fallback the scorer invents."""
    zero = {"blast_radius_threshold": 0.0, "risk_weights": {"default": 0.0}}
    assert evaluate_action_risk("anything", {}, zero).immediate is True


def test_a_non_mapping_weight_section_degrades_to_the_floor() -> None:
    """A hand-built policy that is malformed scores rather than raising."""
    broken = {"blast_radius_threshold": 0.5, "risk_weights": "oops"}
    assert evaluate_action_risk("mem_delete", {}, broken) == ActionRisk(score=0.0, immediate=False)


def test_a_non_mapping_threshold_degrades_to_the_default() -> None:
    broken = {"blast_radius_threshold": "oops", "risk_weights": {"mem_delete": 0.9}}
    assert evaluate_action_risk("mem_delete", {}, broken).immediate is True


def test_a_bool_weight_is_not_read_as_the_maximum_score(policy: dict[str, object]) -> None:
    """JSON true is not 1.0: the whole point of the gate is that it not be.

    The rejected weight is skipped, so the action falls back to the policy's
    ``default`` rather than to zero: the refusal removes one bad entry, it
    does not silently re-score the action.
    """
    policy["risk_weights"]["mem_delete"] = True  # type: ignore[assignment]
    assert evaluate_action_risk("mem_delete", {}, policy) == ActionRisk(score=0.2, immediate=False)


def test_non_mapping_arguments_do_not_raise(policy: dict[str, object]) -> None:
    """The scorer is called from gate paths; bad input scores, never crashes."""
    assert evaluate_action_risk("mem_write", None, policy).score == 0.4
    assert evaluate_action_risk("mem_write", "not a mapping", policy).score == 0.4


def test_scoring_is_deterministic(policy: dict[str, object]) -> None:
    arguments = {"resource_class": "pgvector", "record_key": "k"}
    first = evaluate_action_risk("mem_write", arguments, policy)
    second = evaluate_action_risk("mem_write", arguments, policy)
    assert first == second


def test_the_default_policy_path_is_the_documented_one() -> None:
    assert Path(".continuum/escalation.json") == DEFAULT_ESCALATION_POLICY_PATH
