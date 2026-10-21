"""The liveness cadence contract (issue #1656, module from #302).

``continuum watch`` and every read path depend on this module: the contract is
what turns "no events for a while" into a WAIT proposal, and WAIT reaches
``continuum resume`` as exit code 20. The reading is comparative only, so the
tests inject a clock and never sleep.

Nothing here reads the operator's real ``.continuum/liveness.json``. Every load
test points at a file in ``tmp_path``; that is what keeps the suite honest on a
machine that happens to have one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from continuum.recovery.health import (
    DEFAULT_MAX_SILENCE_SECONDS,
    CadenceContract,
    advisory_text,
    load_cadence_contract,
    silence_seconds,
)

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def write_contract(tmp_path: Path, payload: object) -> Path:
    target = tmp_path / "liveness.json"
    target.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return target


# --- silence_seconds -------------------------------------------------------- #


def test_silence_seconds_passes_none_through() -> None:
    assert silence_seconds(NOW, None) is None


def test_silence_seconds_coerces_a_naive_now_to_utc() -> None:
    naive_now = datetime(2026, 10, 17, 12, 0, 30)
    last = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

    assert silence_seconds(naive_now, last) == 30.0


def test_silence_seconds_coerces_a_naive_last_event_to_utc() -> None:
    last = datetime(2026, 10, 17, 11, 0, 0)

    assert silence_seconds(NOW, last) == 3600.0


def test_silence_seconds_subtracts_two_aware_datetimes() -> None:
    assert silence_seconds(NOW, NOW - timedelta(seconds=90)) == 90.0


def test_silence_seconds_is_zero_at_the_same_instant() -> None:
    assert silence_seconds(NOW, NOW) == 0.0


# --- load_cadence_contract -------------------------------------------------- #


def test_load_returns_the_defaults_when_the_file_is_absent(tmp_path: Path) -> None:
    contract = load_cadence_contract(tmp_path / "not-written.json")

    assert contract.max_silence_seconds == DEFAULT_MAX_SILENCE_SECONDS
    assert contract.phase_scopes == {"open_claim": 600, "otherwise": 3600}


def test_load_reads_a_valid_contract(tmp_path: Path) -> None:
    path = write_contract(
        tmp_path, {"max_silence_seconds": 120, "phase_scopes": {"open_claim": 30}}
    )

    contract = load_cadence_contract(path)

    assert contract.max_silence_seconds == 120
    assert contract.phase_scopes == {"open_claim": 30}


def test_load_refuses_a_malformed_file(tmp_path: Path) -> None:
    path = write_contract(tmp_path, "{not json at all")

    with pytest.raises(ValueError, match="invalid liveness contract"):
        load_cadence_contract(path)


def test_load_refuses_a_schema_that_is_not_an_object(tmp_path: Path) -> None:
    path = write_contract(tmp_path, [1, 2, 3])

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_cadence_contract(path)


def test_load_refuses_a_contract_that_fails_validation(tmp_path: Path) -> None:
    path = write_contract(tmp_path, {"max_silence_seconds": 0})

    with pytest.raises(ValidationError):
        load_cadence_contract(path)


def test_load_refuses_an_unknown_key(tmp_path: Path) -> None:
    path = write_contract(tmp_path, {"max_silence_seconds": 60, "max_silence": 60})

    with pytest.raises(ValidationError):
        load_cadence_contract(path)


# --- the validator ---------------------------------------------------------- #


def test_a_blank_phase_scope_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="non-empty strings"):
        CadenceContract(phase_scopes={"   ": 60})


def test_an_empty_phase_scope_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="non-empty strings"):
        CadenceContract(phase_scopes={"": 60})


def test_a_non_string_phase_scope_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        CadenceContract.model_validate({"phase_scopes": {7: 60}})


def test_a_phase_scope_below_one_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be an int >= 1"):
        CadenceContract(phase_scopes={"open_claim": 0})


def test_a_numeric_string_phase_scope_is_coerced_rather_than_refused() -> None:
    # The model is not strict, so "60" is read as 60 before the validator sees
    # it. Pinned because the validator's own isinstance check is on the
    # coerced value, not on the text an operator typed.
    contract = CadenceContract.model_validate({"phase_scopes": {"open_claim": "60"}})

    assert contract.phase_scopes == {"open_claim": 60}


def test_a_non_numeric_phase_scope_is_refused() -> None:
    with pytest.raises(ValidationError):
        CadenceContract.model_validate({"phase_scopes": {"open_claim": "ten minutes"}})


def test_a_valid_contract_passes_the_validator() -> None:
    contract = CadenceContract(phase_scopes={"open_claim": 600, "otherwise": 3600})

    assert contract.phase_scopes == {"open_claim": 600, "otherwise": 3600}


# --- threshold_for ---------------------------------------------------------- #


def test_threshold_for_uses_the_open_claim_scope_when_a_claim_is_open() -> None:
    contract = CadenceContract(max_silence_seconds=999, phase_scopes={"open_claim": 42})

    assert contract.threshold_for(True) == 42


def test_threshold_for_uses_the_otherwise_scope_when_no_claim_is_open() -> None:
    contract = CadenceContract(max_silence_seconds=999, phase_scopes={"otherwise": 77})

    assert contract.threshold_for(False) == 77


def test_threshold_for_falls_back_to_the_default_for_an_absent_scope() -> None:
    contract = CadenceContract(max_silence_seconds=555, phase_scopes={})

    assert contract.threshold_for(True) == 555
    assert contract.threshold_for(False) == 555


def test_threshold_for_accepts_the_kind_of_flag_the_callers_pass() -> None:
    contract = CadenceContract()

    assert isinstance(contract.threshold_for(has_open_claim=False), int)


# --- to_dict ---------------------------------------------------------------- #


def test_to_dict_round_trips_through_load(tmp_path: Path) -> None:
    contract = CadenceContract(max_silence_seconds=120, phase_scopes={"open_claim": 30})
    path = write_contract(tmp_path, contract.to_dict())

    assert load_cadence_contract(path) == contract


def test_to_dict_is_the_shape_stored_on_disk() -> None:
    contract = CadenceContract(max_silence_seconds=120, phase_scopes={"open_claim": 30})

    assert contract.to_dict() == {
        "max_silence_seconds": 120,
        "phase_scopes": {"open_claim": 30},
    }


# --- advisory_text ---------------------------------------------------------- #


def test_advisory_text_reports_no_events() -> None:
    text = advisory_text({"breached": False, "silence_seconds": None})

    assert text == "Liveness: no events yet, no silence to evaluate."


def test_advisory_text_reports_a_breach_with_the_threshold_and_phase() -> None:
    text = advisory_text(
        {
            "breached": True,
            "silence_seconds": 4001.25,
            "threshold_seconds": 3600,
            "phase": "open_claim",
        }
    )

    assert "BREACHED" in text
    assert "4001.2s" in text
    assert "3600s" in text
    assert "open_claim" in text
    assert "Advisory only" in text


def test_advisory_text_reports_a_reading_within_the_threshold() -> None:
    text = advisory_text(
        {
            "breached": False,
            "silence_seconds": 12.5,
            "threshold_seconds": 600,
            "phase": "open_claim",
        }
    )

    assert "ok" in text
    assert "12.5s" in text
    assert "600s" in text


def test_advisory_text_defaults_the_phase_when_the_key_is_absent() -> None:
    text = advisory_text({"breached": False, "silence_seconds": 1.0, "threshold_seconds": 60})

    assert "(phase otherwise)" in text
