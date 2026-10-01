"""Tests for portable lineage tokens (issue #760).

The token is a *handoff* artifact: it crosses a process or service boundary and
must stand on its own there, so the suite treats the boundary as real — the
cross-process test serializes a token to a file and verifies it in a separate
interpreter with no shared state — and it treats every documented rejection path
as a separate failure, because a delegation token that fails open is worse than
none at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuum.models import RecoveryContract, RecoverySafety
from continuum.recovery.contract import seal_contract
from continuum.security.attestation import generate_keypair, sign_chain
from continuum.security.hashing import to_json
from continuum.security.lineage import (
    DEFAULT_TOKEN_TTL_SECONDS,
    SUPPORTED_TOKEN_VERSIONS,
    TOKEN_VERSION,
    InvalidAttestationError,
    LineageToken,
    TokenParseError,
    TokenVerdict,
    UnsealedContractError,
    _sign,
    issue_token,
    key_id,
    parse_token,
    verify_token,
)

FIXED_NOW = "2026-09-18T12:00:00+00:00"
FIXED_LATER = "2026-09-18T13:00:00+00:00"
ENVELOPE_FIELDS = {
    "version",
    "algorithm",
    "run_id",
    "checkpoint_version",
    "trusted_through_seq",
    "chain_hash",
    "contract_integrity_hash",
    "attestation",
    "issuer",
    "audience",
    "purpose",
    "issued_at",
    "expires_at",
    "public_key",
    "signature",
}
SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _keypair() -> tuple[str, str]:
    return generate_keypair()


def _contract(
    run_id: str = "run_abc",
    checkpoint_version: int = 3,
    *,
    integrity_hash: str | None = "__unset__",
) -> RecoveryContract:
    """A sealed contract with terms a token can bind."""
    contract = RecoveryContract(
        run_id=run_id,
        checkpoint_version=checkpoint_version,
        recovery_status=RecoverySafety.SAFE_TO_RESUME,
        verified=["state"],
        invalidated=[],
        required_actions=[],
        next_allowed_action="continue",
        evidence=["state: all components valid"],
        reason="all components valid",
        created_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
    )
    if integrity_hash == "__unset__":
        return seal_contract(contract)
    # An explicit (possibly stale) hash, for the unsealed-contract tests.
    return contract.model_copy(update={"integrity_hash": integrity_hash})


def _attestation(
    private_pem: str,
    run_id: str = "run_abc",
    seq: int = 17,
    chain_hash: str = "headhash_abc",
) -> dict[str, str | int | None]:
    return sign_chain(
        private_pem, run_id, seq, chain_hash, signer="ci-bot", timestamp=FIXED_NOW
    ).to_dict()


def _issue(
    private_pem: str | None = None,
    *,
    purpose: str = "delegated code review",
    audience: str | None = None,
    contract: RecoveryContract | None = None,
    attestation: dict | None = None,
    expires_at: str | None = FIXED_LATER,
) -> LineageToken:
    private_pem = private_pem or _keypair()[0]
    return issue_token(
        contract or _contract(),
        attestation if attestation is not None else _attestation(private_pem),
        private_pem,
        purpose=purpose,
        audience=audience,
        issuer="ci-bot",
        issued_at=FIXED_NOW,
        expires_at=expires_at,
    )


def _verify(
    token: LineageToken | str | dict,
    *,
    expected_audience: str | None = None,
    trusted_issuers: set[str] | None = None,
    contract: RecoveryContract | None = None,
    expected_run_id: str | None = "run_abc",
    now: str = FIXED_NOW,
) -> TokenVerdict:
    return verify_token(
        token,
        expected_audience=expected_audience,
        trusted_issuers=trusted_issuers,
        contract=contract,
        expected_run_id=expected_run_id,
        now=now,
    ).verdict


def _subprocess_env() -> dict[str, str]:
    """Point a child interpreter at this tree's source, not an installed copy."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


# --- happy paths ------------------------------------------------------------- #


def test_round_trip_verifies() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    result = verify_token(
        token,
        trusted_issuers={pub},
        contract=_contract(),
        expected_run_id="run_abc",
        now=FIXED_NOW,
    )
    assert result.verdict is TokenVerdict.VALID, result.reasons
    assert result.reasons == []
    assert result.token is not None
    assert result.token.purpose == "delegated code review"


def test_trusted_issuers_accept_key_id_and_pem_spellings() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    for trusted in ({pub}, {key_id(pub)}):
        result = verify_token(token, trusted_issuers=trusted, now=FIXED_NOW)
        assert result.verdict is TokenVerdict.VALID, (trusted, result.reasons)


def test_attester_and_issuer_may_be_different_keys() -> None:
    """Attesting a chain and delegating work are separate acts (module docstring)."""
    attester_priv, _ = _keypair()
    issuer_priv, issuer_pub = _keypair()
    token = issue_token(
        _contract(),
        _attestation(attester_priv),
        issuer_priv,
        purpose="delegated code review",
        issuer="delegator",
        issued_at=FIXED_NOW,
        expires_at=FIXED_LATER,
    )
    result = verify_token(token, trusted_issuers={issuer_pub}, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.VALID, result.reasons
    assert result.token is not None
    assert key_id(result.token.public_key) == key_id(issuer_pub)


def test_verification_without_a_contract_is_valid_but_flags_it() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    result = verify_token(token, trusted_issuers={pub}, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.VALID, result.reasons
    assert len(result.advisories) == 1
    assert "contract_integrity_hash" in result.advisories[0]


# --- deterministic serialization -------------------------------------------- #


def test_serialization_is_byte_stable() -> None:
    priv, _ = _keypair()
    left, right = _issue(priv), _issue(priv)
    assert left.to_canonical_json() == right.to_canonical_json()
    # Key order in the source mapping cannot change the canonical form.
    shuffled = dict(reversed(list(json.loads(left.to_canonical_json()).items())))
    assert to_json(shuffled) == left.to_canonical_json()


def test_round_trip_through_json_preserves_the_token() -> None:
    priv, _ = _keypair()
    token = _issue(priv, audience="reviewer-svc")
    parsed = parse_token(token.to_canonical_json())
    assert parsed.to_dict() == token.to_dict()
    assert (
        verify_token(parsed, expected_audience="reviewer-svc", now=FIXED_NOW).verdict
        is TokenVerdict.VALID
    )


def test_envelope_fields_are_bounded() -> None:
    """No secrets, no raw environment payloads, no event history: the point of a
    bounded handoff artifact is that it can be handed to an untrusted system."""
    priv, _ = _keypair()
    doc = _issue(priv, audience="reviewer-svc").to_dict()
    assert set(doc) == ENVELOPE_FIELDS
    # The chain is one hash and one sequence number, never the events, and the
    # contract is one hash, never the terms themselves.
    for name, value in doc.items():
        if name == "attestation":
            assert set(value) <= {
                "run_id",
                "trusted_through_seq",
                "chain_hash",
                "signer",
                "timestamp",
                "public_key",
                "algorithm",
                "signature",
            }
        else:
            assert not isinstance(value, (list, dict)), name


def test_default_ttl_is_finite() -> None:
    priv, _ = _keypair()
    token = issue_token(
        _contract(),
        _attestation(priv),
        priv,
        purpose="delegated code review",
        issued_at=FIXED_NOW,
    )
    expected = datetime.fromisoformat(FIXED_NOW) + timedelta(seconds=DEFAULT_TOKEN_TTL_SECONDS)
    assert datetime.fromisoformat(token.expires_at) == expected


def test_supported_versions_are_an_explicit_set() -> None:
    assert TOKEN_VERSION in SUPPORTED_TOKEN_VERSIONS
    assert TOKEN_VERSION == "lineage-v1"


# --- cross-process verification --------------------------------------------- #


def test_cross_process_verification_succeeds(tmp_path: Path) -> None:
    """A token verified by a process that never saw it issued."""
    issuer_priv, issuer_pub = _keypair()
    token = _issue(issuer_priv, audience="reviewer-svc")

    token_file = tmp_path / "lineage.json"
    token_file.write_text(token.to_canonical_json(), encoding="utf-8")
    contract_file = tmp_path / "contract.json"
    contract_file.write_text(json.dumps(_contract().model_dump(mode="json")), encoding="utf-8")

    verifier = (
        "import json, sys;"
        "from continuum.models import RecoveryContract;"
        "from continuum.security.lineage import verify_token;"
        "token = open(sys.argv[1]).read();"
        "contract = RecoveryContract.model_validate(json.load(open(sys.argv[2])));"
        "result = verify_token(token, expected_audience='reviewer-svc',"
        "trusted_issuers={sys.argv[3]}, contract=contract, expected_run_id='run_abc',"
        "now='2026-09-18T12:30:00+00:00');"
        "print(result.verdict.value, result.reasons)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", verifier, str(token_file), str(contract_file), issuer_pub],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "VALID []"


def test_cross_process_verification_rejects_the_wrong_audience(tmp_path: Path) -> None:
    """The rejection path crosses the boundary too, not just the happy one."""
    priv, pub = _keypair()
    token_file = tmp_path / "lineage.json"
    token_file.write_text(
        _issue(priv, audience="reviewer-svc").to_canonical_json(), encoding="utf-8"
    )

    verifier = (
        "import sys;"
        "from continuum.security.lineage import verify_token;"
        "result = verify_token(open(sys.argv[1]).read(), expected_audience='other-svc',"
        "trusted_issuers={sys.argv[2]}, now='2026-09-18T12:30:00+00:00');"
        "print(result.verdict.value)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", verifier, str(token_file), pub],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == TokenVerdict.WRONG_AUDIENCE.value


# --- rejection paths -------------------------------------------------------- #


def test_tampered_payload_is_rejected() -> None:
    priv, pub = _keypair()
    doc = _issue(priv).to_dict() | {"purpose": "escalated privileges"}
    assert _verify(doc, trusted_issuers={pub}) is TokenVerdict.TAMPERED


def test_substituted_issuer_key_is_rejected() -> None:
    priv, _ = _keypair()
    _other_priv, other_pub = _keypair()
    doc = _issue(priv).to_dict() | {"public_key": other_pub}
    assert _verify(doc) is TokenVerdict.TAMPERED


def test_expired_token_is_rejected() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    result = verify_token(token, trusted_issuers={pub}, now="2030-01-01T00:00:00+00:00")
    assert result.verdict is TokenVerdict.EXPIRED
    assert "expired at" in result.reasons[0]


def test_wrong_audience_is_rejected() -> None:
    priv, pub = _keypair()
    token = _issue(priv, audience="reviewer-svc")
    assert _verify(token, expected_audience="deploy-svc", trusted_issuers={pub}) is (
        TokenVerdict.WRONG_AUDIENCE
    )


def test_audience_bound_token_needs_an_expected_audience() -> None:
    """Without this check a token minted for A replays to any B that skips the check."""
    priv, pub = _keypair()
    token = _issue(priv, audience="reviewer-svc")
    assert _verify(token, trusted_issuers={pub}) is TokenVerdict.WRONG_AUDIENCE


def test_unbound_token_rejects_an_expected_audience() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    assert _verify(token, expected_audience="reviewer-svc", trusted_issuers={pub}) is (
        TokenVerdict.WRONG_AUDIENCE
    )


def test_unknown_issuer_is_rejected() -> None:
    priv, _ = _keypair()
    _other_priv, other_pub = _keypair()
    token = _issue(priv)
    assert _verify(token, trusted_issuers={other_pub}) is TokenVerdict.UNKNOWN_ISSUER


@pytest.mark.parametrize("version", ["lineage-v0", "lineage-v2", "jwt"])
def test_unsupported_version_is_refused(version: str) -> None:
    priv, pub = _keypair()
    doc = _issue(priv).to_dict() | {"version": version}
    result = verify_token(doc, trusted_issuers={pub}, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.UNSUPPORTED_VERSION, result.reasons
    assert "unsupported" in result.reasons[0]


def test_version_precedence_beats_signature_failure() -> None:
    """A future-version token is refused on its version, not diagnosed as a forgery."""
    priv, pub = _keypair()
    doc = _issue(priv).to_dict() | {"version": "lineage-v2", "purpose": "escalated privileges"}
    assert _verify(doc, trusted_issuers={pub}) is TokenVerdict.UNSUPPORTED_VERSION


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: {k: v for k, v in doc.items() if k != "purpose"},
        lambda doc: doc | {"checkpoint_version": "three"},
        lambda doc: doc | {"expires_at": "not-a-timestamp"},
        lambda doc: doc | {"attestation": "not-an-object"},
        lambda doc: doc | {"chain_hash": ""},
        lambda doc: doc | {"version": None},
    ],
)
def test_malformed_tokens_are_rejected(mutation) -> None:
    priv, pub = _keypair()
    doc = mutation(_issue(priv).to_dict())
    result = verify_token(doc, trusted_issuers={pub}, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.MALFORMED, result.reasons


def test_unparsable_json_is_rejected() -> None:
    assert _verify("{this is not json", trusted_issuers=set()) is TokenVerdict.MALFORMED


def test_wrong_run_is_rejected() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    assert _verify(token, expected_run_id="run_xyz", trusted_issuers={pub}) is (
        TokenVerdict.BROKEN_REFERENCE
    )


def test_attestation_describing_another_chain_point_is_rejected() -> None:
    """The token must bind the attestation it carries, not just any valid one."""
    priv, pub = _keypair()
    doc = _issue(priv).to_dict()
    doc["attestation"] = _attestation(priv, seq=99)
    # Re-sign so the envelope is self-consistent; the *reference* is what is wrong.
    doc["signature"] = _sign(priv, doc)
    result = verify_token(doc, trusted_issuers={pub}, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.BROKEN_REFERENCE, result.reasons
    assert "different run or chain point" in result.reasons[0]


def test_contract_with_unmatched_terms_is_rejected() -> None:
    """The token's seal must be the seal of the contract the verifier holds."""
    priv, pub = _keypair()
    token = _issue(priv)
    other = seal_contract(_contract().model_copy(update={"verified": ["state", "dataset:dataset"]}))
    result = verify_token(token, trusted_issuers={pub}, contract=other, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.BROKEN_REFERENCE, result.reasons
    assert "contract_integrity_hash" in result.reasons[0]


def test_contract_whose_own_seal_is_broken_is_rejected() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    edited = _contract().model_copy(update={"reason": "nothing was wrong"})
    result = verify_token(token, trusted_issuers={pub}, contract=edited, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.BROKEN_REFERENCE, result.reasons
    assert "integrity hash" in result.reasons[0]


def test_contract_checkpoint_mismatch_is_rejected() -> None:
    """A different checkpoint moves the seal first; both are reported."""
    priv, pub = _keypair()
    token = _issue(priv)
    other = _contract(checkpoint_version=99)
    result = verify_token(token, trusted_issuers={pub}, contract=other, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.BROKEN_REFERENCE, result.reasons
    assert "contract_integrity_hash" in result.reasons[0]
    assert "checkpoint" in " ".join(result.reasons)


def test_contract_for_another_run_is_rejected() -> None:
    priv, pub = _keypair()
    token = _issue(priv)
    other = _contract(run_id="run_xyz")
    result = verify_token(token, trusted_issuers={pub}, contract=other, now=FIXED_NOW)
    assert result.verdict is TokenVerdict.BROKEN_REFERENCE, result.reasons


def test_reasons_are_reported_in_precedence_order() -> None:
    """Only the first blocking reason sets the verdict; the rest are diagnostic."""
    priv, pub = _keypair()
    doc = _issue(priv, audience="reviewer-svc").to_dict() | {"purpose": "escalated privileges"}
    result = verify_token(
        doc, expected_audience="other-svc", trusted_issuers={pub}, now="2030-01-01T00:00:00+00:00"
    )
    assert result.verdict is TokenVerdict.TAMPERED
    assert len(result.reasons) == 3
    assert "signature" in result.reasons[0]
    assert "expired" in result.reasons[1]
    assert "audience" in result.reasons[2]


# --- issuance rejects bad inputs ------------------------------------------- #


def test_issue_rejects_an_unsealed_contract() -> None:
    priv, _ = _keypair()
    with pytest.raises(UnsealedContractError):
        _issue(priv, contract=_contract(integrity_hash=None))


def test_issue_rejects_a_contract_with_a_stale_seal() -> None:
    priv, _ = _keypair()
    with pytest.raises(UnsealedContractError):
        _issue(priv, contract=_contract(integrity_hash="0" * 64))


def test_issue_rejects_an_attestation_that_does_not_verify() -> None:
    priv, _ = _keypair()
    broken = _attestation(priv)
    broken["trusted_through_seq"] = 18  # breaks the attestation's own signature
    with pytest.raises(InvalidAttestationError):
        _issue(priv, attestation=broken)


def test_issue_rejects_an_attestation_for_another_run() -> None:
    priv, _ = _keypair()
    with pytest.raises(InvalidAttestationError, match="two different runs"):
        _issue(priv, attestation=_attestation(priv, run_id="run_xyz"))


def test_issue_requires_a_purpose() -> None:
    priv, _ = _keypair()
    with pytest.raises(ValueError, match="purpose"):
        _issue(priv, purpose="   ")


def test_issue_rejects_an_expiry_at_or_before_issuance() -> None:
    priv, _ = _keypair()
    with pytest.raises(ValueError, match="not after issuance"):
        _issue(priv, expires_at="2026-09-18T11:59:59+00:00")
    with pytest.raises(ValueError, match="not after issuance"):
        _issue(priv, expires_at=FIXED_NOW)


def test_issue_rejects_both_expiry_spellings() -> None:
    priv, _ = _keypair()
    with pytest.raises(ValueError, match="not both"):
        issue_token(
            _contract(),
            _attestation(priv),
            priv,
            purpose="delegated code review",
            issued_at=FIXED_NOW,
            expires_at=FIXED_LATER,
            ttl_seconds=60,
        )


def test_issue_accepts_a_z_suffixed_timestamp() -> None:
    priv, pub = _keypair()
    token = _issue(priv, expires_at="2026-09-18T13:00:00Z")
    assert token.expires_at.endswith("+00:00")
    assert _verify(token, trusted_issuers={pub}) is TokenVerdict.VALID


def test_parse_rejects_non_object_json() -> None:
    with pytest.raises(TokenParseError):
        parse_token("[1, 2, 3]")
    with pytest.raises(TokenParseError):
        parse_token("null")
