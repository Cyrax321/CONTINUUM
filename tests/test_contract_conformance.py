"""Conformance over the checked-in recovery-contract compatibility corpus (issue #764).

Two checks run here and they are deliberately separate:

* ``run_conformance_suite`` is the *independent* checker. It recomputes every
  fixture's digest from the published rules and never touches
  ``verify_contract``, so it would catch a production verifier that drifted
  from its own published spec.
* The cross-check below runs the production ``verify_contract`` over the same
  corpus and asserts it agrees with each fixture's declared expectation. The
  independent checker cannot substitute for this: a second implementation of
  the rules can be just as wrong as the first, in the same direction.

The corpus also pins the legacy ``evidence``/``reason`` behavior (#289-era) as
regression coverage, so the compatibility path stays measurable instead of
becoming a comment in ``verify_contract``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.models import RecoveryContract
from continuum.recovery.conformance import (
    check_fixture,
    corpus_summary,
    load_corpus,
    run_conformance_suite,
)
from continuum.recovery.contract import (
    SUPPORTED_CONTRACT_VERSIONS,
    canonical_digest_input,
    contract_digest,
    seal_contract,
    verify_contract,
    verify_contract_detailed,
)
from continuum.security.hashing import hash_content

CORPUS = Path(__file__).resolve().parent.parent / "src" / "continuum" / "recovery" / "corpus"

#: Every category the issue names. A missing category would mean the corpus
#: silently stopped covering a case the compatibility policy depends on.
REQUIRED_CATEGORIES = {
    "current",
    "legacy",
    "forward-extension",
    "malformed",
    "tampering",
}


@pytest.fixture(scope="module")
def fixtures() -> list:
    return load_corpus(CORPUS)


def test_corpus_covers_every_required_category(fixtures: list) -> None:
    found = {f.category for f in fixtures}
    assert found >= REQUIRED_CATEGORIES, (
        f"corpus is missing categories: {sorted(REQUIRED_CATEGORIES - found)}"
    )


def test_corpus_has_no_duplicate_names(fixtures: list) -> None:
    names = [f.name for f in fixtures]
    assert len(names) == len(set(names)), f"duplicate fixture names: {names}"


def test_every_fixture_carries_a_version_and_expectation(fixtures: list) -> None:
    """A fixture without these is inert: it cannot pin any behavior."""
    for fixture in fixtures:
        assert isinstance(fixture.contract_version, int)
        assert isinstance(fixture.expected_verified, bool)
        assert fixture.expected_reason, f"{fixture.name} states no expected reason"
        assert fixture.note, f"{fixture.name} states no note explaining what it exercises"


def test_independent_checker_accepts_the_whole_corpus(fixtures: list) -> None:
    results, _ = run_conformance_suite(CORPUS)
    failed = [f"{r.fixture}: {r.detail}" for r in results if not r.passed]
    assert not failed, "corpus fails the independent checker:\n" + "\n".join(failed)
    summary = corpus_summary(results)
    assert summary["failed"] == 0
    assert summary["total"] == len(fixtures)


def test_supported_fixtures_declare_the_exact_digest_input(fixtures: list) -> None:
    """The declared bytes must be what the published rules actually produce.

    This is the check that keeps the corpus honest: a fixture whose declared
    digest input drifted from the implementation would still pass the
    independent checker if the checker shared the drift, so the recomputation
    here is done from the model, not from the fixture's own declaration.
    """
    for fixture in fixtures:
        if fixture.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
            continue
        parsed = fixture.parsed()
        if parsed is None:
            continue
        assert fixture.digest_input is not None, (
            f"{fixture.name} declares no digest input but its version is supported"
        )
        assert canonical_digest_input(parsed, fixture.contract_version) == fixture.digest_input, (
            f"{fixture.name}: declared digest input does not match the published rules"
        )


def test_production_verifier_agrees_with_every_fixture(fixtures: list) -> None:
    """``verify_contract`` must match the corpus, fixture by fixture.

    Where the two disagree the production verifier is the one under suspicion:
    the corpus is the published promise, and a tampering fixture that the
    production code accepts is a forgery bug, not a stale fixture.
    """
    mismatches = []
    for fixture in fixtures:
        parsed = fixture.parsed()
        production = verify_contract(parsed) if parsed is not None else False
        if production != fixture.expected_verified:
            mismatches.append(
                f"{fixture.name}: verify_contract={production}, expected={fixture.expected_verified}"
            )
    assert not mismatches, "\n".join(mismatches)


def test_detailed_verification_reports_the_version_that_accepted() -> None:
    """The detailed result names the version, which the boolean cannot."""
    contract = RecoveryContract.model_validate(
        {
            "run_id": "run_diag",
            "checkpoint_version": 1,
            "contract_version": 1,
            "recovery_status": "safe_to_resume",
            "verified": ["goal"],
            "integrity_hash": None,
        }
    )
    result = verify_contract_detailed(contract)
    assert not result.verified
    assert "never sealed" in result.reason


def test_unsupported_version_fails_closed_with_actionable_diagnostics() -> None:
    """A future version must be rejected with the versions this build knows.

    The alternative, hashing whatever fields happen to be present, is how a
    verifier silently accepts a contract whose terms it cannot interpret.
    """
    contract = RecoveryContract.model_validate(
        {
            "run_id": "run_future",
            "checkpoint_version": 1,
            "contract_version": 99,
            "recovery_status": "safe_to_resume",
            "verified": ["goal"],
            "integrity_hash": "0" * 64,
        }
    )
    result = verify_contract_detailed(contract)
    assert not result.verified
    assert result.version == 99
    assert "99" in result.reason
    assert str(sorted(SUPPORTED_CONTRACT_VERSIONS)) in result.reason


def test_legacy_contract_without_evidence_still_verifies() -> None:
    """The regression the corpus pins: a pre-evidence contract still loads."""
    contract = seal_contract(
        RecoveryContract.model_validate(
            {
                "run_id": "run_legacy",
                "checkpoint_version": 2,
                "recovery_status": "requires_repair",
                "verified": ["goal"],
                "invalidated": ["external_dependency:dataset (CONFLICTED)"],
                "required_actions": ["revalidate_dependency:dataset"],
                "next_allowed_action": "revalidate_dependency:dataset",
            }
        )
    )
    # A contract sealed under the current version verifies against it directly.
    assert verify_contract(contract)
    assert verify_contract_detailed(contract).version == contract.contract_version


def test_digest_is_hash_of_canonical_bytes() -> None:
    """The digest is sha256 over the published canonical bytes, nothing more.

    An external verifier in another language must be able to reproduce it from
    the digest input alone; if the digest depended on Python object identity or
    on ``stable_hash``'s own canonicalization of an already-canonical string,
    it would not be reproducible.
    """
    contract = seal_contract(
        RecoveryContract.model_validate(
            {
                "run_id": "run_repro",
                "checkpoint_version": 1,
                "recovery_status": "safe_to_resume",
                "verified": ["goal"],
            }
        )
    )
    digest_input = canonical_digest_input(contract, contract.contract_version)
    assert contract_digest(contract, contract.contract_version) == hash_content(
        digest_input.encode("utf-8")
    )
    assert contract.integrity_hash == contract_digest(contract, contract.contract_version)


def test_liveness_wall_clock_reading_does_not_change_the_digest() -> None:
    """Two assessments of one run must seal the same hash.

    ``last_append_age`` is seconds-since-last-append at assessment time, so it
    changes between two assessments of an otherwise identical run. The verdict
    fields stay covered; this one reading is stripped.
    """
    base = seal_contract(
        RecoveryContract.model_validate(
            {
                "run_id": "run_live",
                "checkpoint_version": 1,
                "recovery_status": "safe_to_resume",
                "verified": ["goal"],
                "liveness": {"breached": False, "threshold_seconds": 60, "breaches": 0},
            }
        )
    )
    older = base.model_copy(
        update={
            "liveness": {
                "breached": False,
                "threshold_seconds": 60,
                "breaches": 0,
                "last_append_age": 5,
            }
        }
    )
    newer = base.model_copy(
        update={
            "liveness": {
                "breached": False,
                "threshold_seconds": 60,
                "breaches": 0,
                "last_append_age": 9001,
            }
        }
    )
    assert contract_digest(older, 1) == contract_digest(newer, 1)
    assert verify_contract(older) and verify_contract(newer)


def test_checker_reports_a_fixture_that_disagrees(tmp_path: Path) -> None:
    """A fixture whose expectation contradicts its payload must fail loudly."""
    contract = seal_contract(
        RecoveryContract.model_validate(
            {
                "run_id": "run_bad_fixture",
                "checkpoint_version": 1,
                "recovery_status": "safe_to_resume",
                "verified": ["goal"],
            }
        )
    )
    import json

    payload = contract.model_dump(mode="json")
    payload["verified"] = ["goal", "secret extra claim"]
    fixture_path = tmp_path / "contradictory.json"
    fixture_path.write_text(
        json.dumps(
            {
                "name": "contradictory",
                "category": "tampering",
                "note": "declares verified but the payload was edited",
                "contract_version": 1,
                "contract": payload,
                "digest_input": canonical_digest_input(RecoveryContract.model_validate(payload), 1),
                "expected": {"verified": True, "reason": "claims to verify"},
            }
        ),
        encoding="utf-8",
    )
    (fixture,) = load_corpus(tmp_path)
    result = check_fixture(fixture)
    assert not result.passed
    assert "does not match its stored hash" in result.detail
