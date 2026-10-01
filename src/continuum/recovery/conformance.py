"""A conformance suite for the recovery-contract compatibility corpus (issue #764).

``verify_contract`` is the production check; this module is the *independent*
one. It recomputes a fixture's digest from the published rules in
``continuum.recovery.contract`` and compares, and it treats a fixture's own
declared expectations as ground truth rather than asking the production code
whether it agrees with itself. That separation is the point: a second
implementation of the same published rules (another framework's verifier, a
Rust or Go port) runs the same corpus and gets the same answers without ever
importing Python.

The corpus lives as checked-in JSON so it is reviewable, diffable and
citable. Each fixture states:

* ``contract`` -- the payload as it travels on the wire;
* ``contract_version`` -- the compatibility version it claims;
* ``digest_input`` -- the canonical serialization the hash covers, or the
  expected failure if the payload cannot be canonicalized;
* ``expected`` -- the verification outcome and, where relevant, why.

Canonicalization rules a reproducer must match are in
``docs/contract_compatibility.md`` and ``canonical_digest_input``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from continuum.models import RecoveryContract
from continuum.recovery.contract import (
    SUPPORTED_CONTRACT_VERSIONS,
    _liveness_digest_value,
)

__all__ = [
    "ConformanceFailure",
    "ConformanceResult",
    "ContractFixture",
    "run_conformance_suite",
]


@dataclass(frozen=True)
class ContractFixture:
    """One checked-in corpus case, exactly as the JSON states it."""

    name: str
    category: str
    #: The raw wire payload. Kept unparsed because a forward-extension case
    #: carries a field the current model forbids, and that rejection is the
    #: outcome the case exists to pin.
    contract: dict[str, Any]
    contract_version: int
    #: The canonical serialization the fixture's hash covers. ``None`` marks a
    #: case whose payload *cannot* be canonicalized (unknown version, unknown
    #: hash-covered field): the expectation is rejection, not a digest.
    digest_input: str | None
    expected_verified: bool
    expected_reason: str
    #: A short note on what the case exercises, carried into failure messages.
    note: str = ""

    @property
    def expects_rejection(self) -> bool:
        """A case whose payload must fail, not merely hash to something else."""
        return not self.expected_verified

    def parsed(self) -> RecoveryContract | None:
        """The contract as the current model sees it, or None if it cannot load.

        ``None`` is a first-class result, not an error: a payload from a newer
        version carrying an unknown field is *expected* not to load, and the
        fixture's expectation says so.
        """
        try:
            return RecoveryContract.model_validate(self.contract)
        except ValidationError:
            return None


@dataclass
class ConformanceResult:
    """The outcome of one fixture under the independent checker."""

    fixture: str
    category: str
    passed: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed


class ConformanceFailure(Exception):
    """A fixture does not match the published compatibility rules."""


def _load_fixture(path: Path) -> ContractFixture:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ContractFixture(
        name=raw["name"],
        category=raw["category"],
        contract=raw["contract"],
        contract_version=raw["contract_version"],
        digest_input=raw.get("digest_input"),
        expected_verified=bool(raw["expected"]["verified"]),
        expected_reason=raw["expected"].get("reason", ""),
        note=raw.get("note", ""),
    )


def _independent_digest(contract: RecoveryContract, version: int) -> str:
    """Recompute the canonical digest input without touching ``verify_contract``.

    Reimplements the published canonicalization rules rather than calling
    ``canonical_digest_input``, so a divergence between the two is a finding
    the suite reports, not a comparison of a function against itself.
    """
    dumped = contract.model_dump(mode="json")
    payload = {k: v for k, v in dumped.items() if k in _covered_fields(version)}
    if "liveness" in payload:
        payload["liveness"] = _liveness_digest_value(payload["liveness"])
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _covered_fields(version: int) -> frozenset[str]:
    from continuum.recovery.contract import CONTRACT_VERSIONS

    for spec in CONTRACT_VERSIONS:
        if spec.version == version:
            return spec.covered
    raise ConformanceFailure(f"unknown contract version {version}")


def check_fixture(fixture: ContractFixture) -> ConformanceResult:
    """Verify one fixture against the published rules, independently.

    A fixture passes when the recomputed digest equals the digest the fixture
    declares *and* the declared expectation matches what the rules imply. The
    production ``verify_contract`` is never called.
    """
    if fixture.contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        # An unknown version must be rejected by the corpus's own rules, not
        # hashed anyway. The fixture's expectation records that rejection.
        if fixture.expected_verified:
            return ConformanceResult(
                fixture.name,
                fixture.category,
                passed=False,
                detail=(
                    f"unsupported version {fixture.contract_version} is expected to "
                    "verify, but the published rules reject it rather than guess"
                ),
            )
        return ConformanceResult(
            fixture.name,
            fixture.category,
            passed=True,
            detail=f"unsupported version {fixture.contract_version} rejected, as expected",
        )

    # A payload the current model cannot load cannot be canonicalized either.
    # That is the forward-extension case: an unknown hash-covered field, which
    # must fail closed. A supported version that cannot parse is a malformed
    # fixture, which is a corpus bug rather than a compatibility outcome.
    contract = fixture.parsed()
    if contract is None:
        if fixture.expected_verified:
            return ConformanceResult(
                fixture.name,
                fixture.category,
                passed=False,
                detail="payload does not load under the current model but is expected to verify",
            )
        return ConformanceResult(
            fixture.name,
            fixture.category,
            passed=True,
            detail="payload rejected by the model, as the fixture expects",
        )

    recomputed = _independent_digest(contract, fixture.contract_version)
    if fixture.digest_input is None:
        return ConformanceResult(
            fixture.name,
            fixture.category,
            passed=False,
            detail=(
                f"version {fixture.contract_version} is supported, so the fixture must "
                "declare the canonical digest input it hashes to, not expect rejection"
            ),
        )

    if recomputed != fixture.digest_input:
        return ConformanceResult(
            fixture.name,
            fixture.category,
            passed=False,
            detail=(
                "recomputed digest input does not match the fixture's declared input; "
                "the published rules and the fixture disagree"
            ),
        )

    # The digest matches the payload; the expectation must now match the digest.
    # A tampering fixture is one whose stored hash disagrees with the payload it
    # carries, and it must be expected to fail.
    stored = contract.integrity_hash
    from continuum.security.hashing import hash_content

    actually_verifies = stored is not None and hash_content(recomputed.encode("utf-8")) == stored
    if actually_verifies != fixture.expected_verified:
        return ConformanceResult(
            fixture.name,
            fixture.category,
            passed=False,
            detail=(
                f"fixture expects verified={fixture.expected_verified} but the digest "
                f"of its own payload {'does' if actually_verifies else 'does not'} "
                f"match its stored hash"
            ),
        )
    return ConformanceResult(
        fixture.name,
        fixture.category,
        passed=True,
        detail=f"digest matches; verified={actually_verifies}",
    )


def load_corpus(corpus_dir: Path | None = None) -> list[ContractFixture]:
    """Load every fixture in ``corpus_dir``, sorted by name for stable output.

    The default directory is the checked-in corpus that ships beside the
    conformance module. Pointing elsewhere is how an external verifier runs its
    own extended corpus against the same checker.
    """
    if corpus_dir is None:
        corpus_dir = Path(__file__).resolve().parent / "corpus"
    paths = sorted(corpus_dir.glob("*.json"))
    if not paths:
        raise ConformanceFailure(f"no fixtures found in {corpus_dir}")
    return [_load_fixture(p) for p in paths]


def run_conformance_suite(
    corpus_dir: Path | None = None,
) -> tuple[list[ConformanceResult], list[ContractFixture]]:
    """Run the independent checker over the whole corpus.

    Returns the per-fixture results alongside the fixtures, so a caller can
    report which category failed rather than only that something did.
    """
    fixtures = load_corpus(corpus_dir)
    results = [check_fixture(fixture) for fixture in fixtures]
    return results, fixtures


def corpus_summary(results: list[ConformanceResult]) -> dict[str, Any]:
    """Pass/fail counts by category, for a human-facing report."""
    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_category.setdefault(result.category, {"passed": 0, "failed": 0})
        bucket["passed" if result.passed else "failed"] += 1
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "by_category": by_category,
    }
