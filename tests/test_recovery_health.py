"""Cadence contracts and read-path liveness evaluation (issue #1656).

``recovery.health`` is a pure comparative module: it reports
``now - last_event_ts`` against an operator-set threshold, with an injected
clock so tests never sleep. The tests below pin three contracts:

* the ``CadenceContract`` validator rejects malformed phase scopes;
* ``silence_seconds`` and ``evaluate`` handle the "no events" case without
  crashing, and normalise naive datetimes to UTC;
* ``advisory_text`` renders every branch of the advisory dict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from continuum.recovery.health import (
    DEFAULT_LIVENESS_PATH,
    DEFAULT_MAX_SILENCE_SECONDS,
    DEFAULT_PHASE_SCOPES,
    CadenceContract,
    LivenessResult,
    advisory_text,
    evaluate,
    load_cadence_contract,
    silence_seconds,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# CadenceContract — defaults


def test_contract_defaults_match_module_constants() -> None:
    cfg = CadenceContract()
    assert cfg.max_silence_seconds == DEFAULT_MAX_SILENCE_SECONDS
    assert cfg.phase_scopes == DEFAULT_PHASE_SCOPES


def test_contract_is_frozen() -> None:
    cfg = CadenceContract()
    with pytest.raises(ValidationError):
        cfg.max_silence_seconds = 1  # type: ignore[misc]


def test_contract_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CadenceContract(unexpected=1)  # type: ignore[call-arg]


def test_contract_rejects_max_silence_below_one() -> None:
    with pytest.raises(ValidationError):
        CadenceContract(max_silence_seconds=0)


# --------------------------------------------------------------------------- #
# CadenceContract — phase_scopes validator


def test_contract_accepts_valid_phase_scopes() -> None:
    cfg = CadenceContract(phase_scopes={"open_claim": 60, "otherwise": 120})
    assert cfg.phase_scopes == {"open_claim": 60, "otherwise": 120}


def test_contract_rejects_phase_scope_value_below_one() -> None:
    with pytest.raises(ValidationError):
        CadenceContract(phase_scopes={"open_claim": 0})


def test_contract_rejects_phase_scope_non_int_value() -> None:
    # A list cannot be coerced to int, so the validator is reached.
    with pytest.raises(ValidationError):
        CadenceContract(phase_scopes={"open_claim": [60]})  # type: ignore[dict-item]


def test_contract_rejects_empty_phase_scope_key() -> None:
    with pytest.raises(ValidationError):
        CadenceContract(phase_scopes={"   ": 60})


# --------------------------------------------------------------------------- #
# CadenceContract — threshold_for


def test_threshold_for_open_claim_uses_open_claim_scope() -> None:
    cfg = CadenceContract(phase_scopes={"open_claim": 60, "otherwise": 120})
    assert cfg.threshold_for(has_open_claim=True) == 60


def test_threshold_for_no_claim_uses_otherwise_scope() -> None:
    cfg = CadenceContract(phase_scopes={"open_claim": 60, "otherwise": 120})
    assert cfg.threshold_for(has_open_claim=False) == 120


def test_threshold_for_falls_back_to_max_silence_when_scope_missing() -> None:
    cfg = CadenceContract(
        max_silence_seconds=777,
        phase_scopes={"open_claim": 60},
    )
    assert cfg.threshold_for(has_open_claim=False) == 777


def test_threshold_for_returns_int() -> None:
    cfg = CadenceContract()
    assert isinstance(cfg.threshold_for(has_open_claim=True), int)
    assert isinstance(cfg.threshold_for(has_open_claim=False), int)


# --------------------------------------------------------------------------- #
# CadenceContract — to_dict round-trip


def test_to_dict_round_trips_through_model_validate() -> None:
    cfg = CadenceContract(
        max_silence_seconds=42,
        phase_scopes={"open_claim": 7, "otherwise": 11},
    )
    assert CadenceContract.model_validate(cfg.to_dict()) == cfg


# --------------------------------------------------------------------------- #
# silence_seconds


def test_silence_seconds_none_when_no_events() -> None:
    assert silence_seconds(T0, None) is None


def test_silence_seconds_positive_when_now_is_after_last_event() -> None:
    last = T0 - timedelta(seconds=90)
    assert silence_seconds(T0, last) == 90.0


def test_silence_seconds_negative_when_clock_skew_backwards() -> None:
    last = T0 + timedelta(seconds=30)
    assert silence_seconds(T0, last) == -30.0


def test_silence_seconds_normalises_naive_datetimes_to_utc() -> None:
    naive_now = T0.replace(tzinfo=None)
    naive_last = (T0 - timedelta(seconds=5)).replace(tzinfo=None)
    assert silence_seconds(naive_now, naive_last) == 5.0


# --------------------------------------------------------------------------- #
# evaluate — no events


def test_evaluate_no_events_is_not_breached() -> None:
    result = evaluate(T0, None)
    assert isinstance(result, LivenessResult)
    assert result.breached is False
    assert result.silence_seconds is None
    assert result.last_event_ts is None
    assert result.has_open_claim is False


def test_evaluate_no_events_uses_otherwise_phase_by_default() -> None:
    assert evaluate(T0, None).phase == "otherwise"


def test_evaluate_no_events_uses_open_claim_phase_when_claimed() -> None:
    assert evaluate(T0, None, has_open_claim=True).phase == "open_claim"


# --------------------------------------------------------------------------- #
# evaluate — breach logic


def _contract(max_silence: int, **scopes: int) -> CadenceContract:
    """Build a contract with explicit scopes, avoiding default fallbacks."""
    return CadenceContract(
        max_silence_seconds=max_silence,
        phase_scopes={"open_claim": max_silence, "otherwise": max_silence, **scopes},
    )


def test_evaluate_not_breached_when_silence_within_threshold() -> None:
    cfg = _contract(100)
    last = T0 - timedelta(seconds=50)
    result = evaluate(T0, last, contract=cfg)
    assert result.breached is False
    assert result.silence_seconds == 50.0
    assert result.threshold_seconds == 100


def test_evaluate_breached_when_silence_exceeds_threshold() -> None:
    cfg = _contract(100)
    last = T0 - timedelta(seconds=101)
    assert evaluate(T0, last, contract=cfg).breached is True


def test_evaluate_not_breached_exactly_at_threshold() -> None:
    cfg = _contract(100)
    last = T0 - timedelta(seconds=100)
    assert evaluate(T0, last, contract=cfg).breached is False


def test_evaluate_not_breached_when_silence_is_negative() -> None:
    cfg = _contract(100)
    last = T0 + timedelta(seconds=5)
    assert evaluate(T0, last, contract=cfg).breached is False


def test_evaluate_uses_open_claim_scope_when_claimed() -> None:
    cfg = _contract(1000, open_claim=50)
    last = T0 - timedelta(seconds=60)
    result = evaluate(T0, last, contract=cfg, has_open_claim=True)
    assert result.breached is True
    assert result.phase == "open_claim"
    assert result.threshold_seconds == 50


def test_evaluate_normalises_naive_datetimes() -> None:
    cfg = _contract(100)
    naive_now = T0.replace(tzinfo=None)
    naive_last = (T0 - timedelta(seconds=5)).replace(tzinfo=None)
    result = evaluate(naive_now, naive_last, contract=cfg)
    assert result.now.tzinfo is UTC
    assert result.last_event_ts is not None
    assert result.last_event_ts.tzinfo is UTC


def test_evaluate_contract_method_matches_module_function() -> None:
    cfg = _contract(100)
    last = T0 - timedelta(seconds=50)
    assert cfg.evaluate(T0, last) == evaluate(T0, last, contract=cfg)


# --------------------------------------------------------------------------- #
# load_cadence_contract


def test_load_cadence_contract_returns_defaults_when_path_missing(tmp_path: Path) -> None:
    cfg = load_cadence_contract(tmp_path / "does-not-exist.json")
    assert cfg == CadenceContract()


def test_load_cadence_contract_reads_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    path.write_text(
        '{"max_silence_seconds": 42, "phase_scopes": {"open_claim": 7}}',
        encoding="utf-8",
    )
    cfg = load_cadence_contract(path)
    assert cfg.max_silence_seconds == 42
    assert cfg.phase_scopes == {"open_claim": 7}


def test_load_cadence_contract_raises_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid liveness contract"):
        load_cadence_contract(path)


def test_load_cadence_contract_raises_on_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_cadence_contract(path)


def test_default_liveness_path_is_relative_to_cwd() -> None:
    assert Path(".continuum/liveness.json") == DEFAULT_LIVENESS_PATH


# --------------------------------------------------------------------------- #
# advisory_text


def test_advisory_text_no_events() -> None:
    text = advisory_text({"silence_seconds": None})
    assert text == "Liveness: no events yet, no silence to evaluate."


def test_advisory_text_breached_mentions_advisory_only() -> None:
    text = advisory_text(
        {
            "breached": True,
            "silence_seconds": 12.3,
            "threshold_seconds": 10,
            "phase": "open_claim",
        }
    )
    assert "BREACHED" in text
    assert "12.3s" in text
    assert "10s" in text
    assert "open_claim" in text
    assert "Advisory only" in text


def test_advisory_text_ok_mentions_ok() -> None:
    text = advisory_text(
        {
            "breached": False,
            "silence_seconds": 5.0,
            "threshold_seconds": 60,
            "phase": "otherwise",
        }
    )
    assert text.startswith("Liveness: ok,")
    assert "5.0s" in text
    assert "60s" in text


def test_advisory_text_defaults_phase_to_otherwise() -> None:
    text = advisory_text(
        {"breached": False, "silence_seconds": 1.0, "threshold_seconds": 60}
    )
    assert "otherwise" in text
