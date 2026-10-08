"""Tests for the declarative risk policy schema, parser, and action evaluator (issue #1422)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.models import RecoveryMode
from continuum.recovery.risk_policy import (
    BASELINE_RISK_POLICY,
    DEFAULT_RISK_POLICY,
    KNOWN_TRIGGERS,
    RISK_POLICY_SCHEMA,
    RiskPolicy,
    RiskPolicyError,
    RiskPolicySchema,
    evaluate_risk,
    evaluate_risk_action,
    is_more_conservative,
    load_risk_policy,
)


def test_default_risk_policy_has_all_canonical_triggers() -> None:
    expected_triggers = {
        "loop",
        "loop_persisting",
        "error_cascade",
        "latency_anomaly",
        "token_runaway",
        "silent_abort",
        "meltdown",
        "side_effect_duplicate",
        "governance_decay",
    }
    assert set(DEFAULT_RISK_POLICY.keys()) == expected_triggers
    assert set(KNOWN_TRIGGERS) == expected_triggers
    assert BASELINE_RISK_POLICY == DEFAULT_RISK_POLICY
    assert is_more_conservative("rollback", "replan") is True
    assert is_more_conservative("replan", "rollback") is False
    assert evaluate_risk("loop") == "replan"
    assert evaluate_risk("loop_persisting") == "rollback"
    assert evaluate_risk("latency_anomaly") is None
    assert evaluate_risk("unknown") is None
    assert DEFAULT_RISK_POLICY["loop"] == "replan"
    assert DEFAULT_RISK_POLICY["loop_persisting"] == "rollback"
    assert DEFAULT_RISK_POLICY["error_cascade"] == "wait"
    assert DEFAULT_RISK_POLICY["latency_anomaly"] == "annotate"
    assert DEFAULT_RISK_POLICY["token_runaway"] == "wait"
    assert DEFAULT_RISK_POLICY["silent_abort"] == "repair_and_resume"
    assert DEFAULT_RISK_POLICY["meltdown"] == "rollback"
    assert DEFAULT_RISK_POLICY["side_effect_duplicate"] == "abort"
    assert DEFAULT_RISK_POLICY["governance_decay"] == "request_human"


def test_load_risk_policy_defaults_when_absent(tmp_path: Path) -> None:
    missing_path = tmp_path / "absent-risk-policy.json"
    policy = load_risk_policy(missing_path)
    assert isinstance(policy, RiskPolicy)
    assert policy["loop"] == "replan"
    assert policy["loop_persisting"] == "rollback"
    assert policy["meltdown"] == "rollback"
    assert policy["side_effect_duplicate"] == "abort"
    assert policy.token_runaway_threshold == 0.8


def test_load_risk_policy_fail_closed_on_invalid_json(tmp_path: Path) -> None:
    bad_json = tmp_path / "broken.json"
    bad_json.write_text("{not: valid: json", encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="invalid risk policy"):
        load_risk_policy(bad_json)


def test_load_risk_policy_fail_closed_on_non_object(tmp_path: Path) -> None:
    non_obj = tmp_path / "array.json"
    non_obj.write_text(json.dumps(["loop", "replan"]), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="must be a JSON object"):
        load_risk_policy(non_obj)


def test_load_risk_policy_fail_closed_on_unknown_trigger(tmp_path: Path) -> None:
    unknown_trig = tmp_path / "unknown_trigger.json"
    unknown_trig.write_text(
        json.dumps({"unknown_anomaly": "wait", "loop": "replan"}),
        encoding="utf-8",
    )
    with pytest.raises(RiskPolicyError, match="unknown trigger 'unknown_anomaly'"):
        load_risk_policy(unknown_trig)


def test_load_risk_policy_fail_closed_on_invalid_mode(tmp_path: Path) -> None:
    bad_mode = tmp_path / "bad_mode.json"
    bad_mode.write_text(
        json.dumps({"loop": "invalid_action_mode"}),
        encoding="utf-8",
    )
    with pytest.raises(RiskPolicyError, match="must be one of"):
        load_risk_policy(bad_mode)


def test_load_risk_policy_fail_closed_on_downgrades(tmp_path: Path) -> None:
    p = tmp_path / "downgrade.json"

    # Downgrading side_effect_duplicate from abort to rollback
    p.write_text(json.dumps({"side_effect_duplicate": "rollback"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="downgrades 'abort' to 'rollback'"):
        load_risk_policy(p)

    # Downgrading meltdown from rollback to replan
    p.write_text(json.dumps({"meltdown": "replan"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="downgrades 'rollback' to 'replan'"):
        load_risk_policy(p)

    # Downgrading governance_decay from request_human to wait
    p.write_text(json.dumps({"governance_decay": "wait"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="downgrades 'request_human' to 'wait'"):
        load_risk_policy(p)

    # Downgrading loop_persisting from rollback to replan
    p.write_text(json.dumps({"loop_persisting": "replan"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="downgrades 'rollback' to 'replan'"):
        load_risk_policy(p)

    # Downgrading error_cascade from wait to replan
    p.write_text(json.dumps({"error_cascade": "replan"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="downgrades 'wait' to 'replan'"):
        load_risk_policy(p)

    # Downgrading token_runaway from wait to annotate
    p.write_text(json.dumps({"token_runaway": "annotate"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="downgrades 'wait' to 'annotate'"):
        load_risk_policy(p)


def test_load_risk_policy_allows_conservative_upgrades(tmp_path: Path) -> None:
    p = tmp_path / "upgrade.json"
    p.write_text(
        json.dumps(
            {
                "loop": "wait",
                "loop_persisting": "abort",
                "meltdown": "abort",
                "latency_anomaly": "replan",
            }
        ),
        encoding="utf-8",
    )
    policy = load_risk_policy(p)
    assert policy["loop"] == "wait"
    assert policy["loop_persisting"] == "abort"
    assert policy["meltdown"] == "abort"
    assert policy["latency_anomaly"] == "replan"
    # Unspecified triggers retain their conservative defaults
    assert policy["side_effect_duplicate"] == "abort"
    assert policy["error_cascade"] == "wait"


def test_load_risk_policy_allows_schema_reference(tmp_path: Path) -> None:
    p = tmp_path / "with_schema.json"
    p.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "loop": "replan",
                "meltdown": "rollback",
            }
        ),
        encoding="utf-8",
    )
    policy = load_risk_policy(p)
    assert policy["loop"] == "replan"
    assert policy["meltdown"] == "rollback"


def test_load_risk_policy_token_runaway_threshold(tmp_path: Path) -> None:
    p = tmp_path / "threshold.json"
    p.write_text(
        json.dumps({"token_runaway": "wait", "token_runaway_threshold": 0.65}),
        encoding="utf-8",
    )
    policy = load_risk_policy(p)
    assert policy.token_runaway_threshold == 0.65

    # Invalid threshold type or range
    p.write_text(
        json.dumps({"token_runaway_threshold": "not_a_number"}),
        encoding="utf-8",
    )
    with pytest.raises(RiskPolicyError, match="token_runaway_threshold must be a number"):
        load_risk_policy(p)

    p.write_text(
        json.dumps({"token_runaway_threshold": 1.5}),
        encoding="utf-8",
    )
    with pytest.raises(RiskPolicyError, match="must be within"):
        load_risk_policy(p)


def test_evaluate_risk_action_default_mappings() -> None:
    policy = load_risk_policy(Path("non_existent_file.json"))

    assert evaluate_risk_action("loop", 0.5, policy) == RecoveryMode.REPLAN
    assert evaluate_risk_action("loop_persisting", 0.5, policy) == RecoveryMode.ROLLBACK
    assert evaluate_risk_action("error_cascade", 0.5, policy) == RecoveryMode.WAIT
    assert evaluate_risk_action("latency_anomaly", 0.5, policy) is None
    assert evaluate_risk_action("silent_abort", 0.5, policy) == RecoveryMode.REPAIR_AND_RESUME
    assert evaluate_risk_action("meltdown", 0.5, policy) == RecoveryMode.ROLLBACK
    assert evaluate_risk_action("side_effect_duplicate", 0.5, policy) == RecoveryMode.ABORT
    assert evaluate_risk_action("governance_decay", 0.5, policy) == RecoveryMode.REQUEST_HUMAN
    assert evaluate_risk_action("unknown_trigger", 0.5, policy) is None
    assert evaluate_risk_action("", 0.5, policy) is None


def test_evaluate_risk_action_token_runaway_threshold_escalation() -> None:
    policy = load_risk_policy(Path("non_existent_file.json"))
    # Below default threshold 0.8: WAIT
    assert evaluate_risk_action("token_runaway", 0.79, policy) == RecoveryMode.WAIT
    # At or above threshold 0.8: escalates to ABORT
    assert evaluate_risk_action("token_runaway", 0.80, policy) == RecoveryMode.ABORT
    assert evaluate_risk_action("token_runaway", 0.99, policy) == RecoveryMode.ABORT

    # Custom threshold
    custom_policy = RiskPolicy(token_runaway_threshold=0.5)
    assert evaluate_risk_action("token_runaway", 0.49, custom_policy) == RecoveryMode.WAIT
    assert evaluate_risk_action("token_runaway", 0.50, custom_policy) == RecoveryMode.ABORT


def test_evaluate_risk_action_with_upgraded_policy(tmp_path: Path) -> None:
    p = tmp_path / "upgraded.json"
    p.write_text(json.dumps({"loop": "rollback"}), encoding="utf-8")
    policy = load_risk_policy(p)
    assert evaluate_risk_action("loop", 0.2, policy) == RecoveryMode.ROLLBACK


def test_json_schema_validates_and_matches_model() -> None:
    schema = RISK_POLICY_SCHEMA
    assert schema["type"] == "object"
    assert "loop" in schema["properties"]
    assert "loop_persisting" in schema["properties"]
    assert "token_runaway_threshold" in schema["properties"]

    # Verify model_validate works with the default policy
    validated = RiskPolicySchema.model_validate(DEFAULT_RISK_POLICY)
    assert validated.loop == "replan"
    assert validated.loop_persisting == "rollback"
    assert validated.meltdown == "rollback"
