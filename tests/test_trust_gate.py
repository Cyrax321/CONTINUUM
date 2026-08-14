"""Tests for the secure planning loop trust gate (Extension 1)."""

from __future__ import annotations

import pytest

from continuum.events import EventType
from continuum.models import Run
from continuum.security.provenance import ObservationProvenance, PlanBranch
from continuum.security.trust_gate import (
    resolve_branch,
    verify_observation,
)


def _branch(risk: str, depends: bool = True) -> PlanBranch:
    return PlanBranch(
        branch_id="b1",
        risk_tier=risk,  # type: ignore[arg-type]
        action_intent="submit_payment",
        depends_on_observation=depends,
    )


def _obs(trust: str, source: str = "environment_observed") -> ObservationProvenance:
    return ObservationProvenance(
        observation_id="obs1",
        source=source,  # type: ignore[arg-type]
        trust_level=trust,  # type: ignore[arg-type]
        verifier="consensus_only",
        content_hash="h",
        q_vlm_model="vlm",
        raw_claim="Accept",
    )


def test_models_are_frozen() -> None:
    b = _branch("high")
    with pytest.raises(ValueError):
        b.branch_id = "x"  # type: ignore[misc]


def test_verify_observation_verified_when_both_checks_pass() -> None:
    obs = verify_observation(
        "Accept",
        "hash",
        dom_snapshot='button text="Accept"',
        dom_check=lambda claim, dom: claim in dom,
        consensus_check=lambda claim, h: True,
    )
    assert obs.trust_level == "verified"


def test_verify_observation_contested_when_checks_disagree() -> None:
    obs = verify_observation(
        "Accept",
        "hash",
        dom_snapshot='aria-label="Decline all"',
        dom_check=lambda claim, dom: claim in dom,  # False
        consensus_check=lambda claim, h: True,  # True
    )
    assert obs.trust_level == "contested"


def test_verify_observation_unverified_with_single_signal() -> None:
    obs = verify_observation("Accept", "hash", consensus_check=lambda claim, h: True)
    assert obs.trust_level == "unverified"
    assert obs.verifier == "consensus_only"


def test_high_risk_unverified_requires_review() -> None:
    gate = resolve_branch(_branch("high"), _obs("unverified"))
    assert gate.requires_review is True


def test_high_risk_contested_requires_review() -> None:
    gate = resolve_branch(_branch("high"), _obs("contested"))
    assert gate.requires_review is True


def test_high_risk_verified_proceeds() -> None:
    gate = resolve_branch(_branch("high"), _obs("verified"))
    assert gate.requires_review is False


def test_low_risk_verified_proceeds() -> None:
    gate = resolve_branch(_branch("low"), _obs("verified"))
    assert gate.requires_review is False


def test_contested_env_observation_requires_review_even_when_low() -> None:
    gate = resolve_branch(_branch("low"), _obs("contested"))
    assert gate.requires_review is True


def test_medium_unverified_proceeds_false_positive_check() -> None:
    # A medium branch on an unverified (but not contested) observation is not
    # blocked: only high-risk or contested claims escalate. This is the
    # false-positive guard the spec calls equally important.
    gate = resolve_branch(_branch("medium"), _obs("unverified"))
    assert gate.requires_review is False


def test_events_are_appended_to_ledger() -> None:
    from continuum.storage.sqlite import SQLiteStorage

    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="r1", goal="g"))

    obs = verify_observation("Accept", "hash", dom_snapshot="x", storage=store, run_id="r1")
    gate = resolve_branch(_branch("high"), obs, storage=store, run_id="r1")

    types = {e.type for e in store.read_events("r1")}
    assert EventType.PERCEPTION_OBSERVED in types
    assert EventType.BRANCH_RESOLVED in types
    assert gate.event is not None
    assert gate.event.payload["requires_review"] is True
