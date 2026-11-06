"""Round-trip and validation guard tests for the event-chain attestation primitives."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from continuum.security.attestation import (
    ATTESTATION_ALGORITHM,
    Attestation,
    generate_keypair,
    sign_chain,
    verify_attestation,
)
from continuum.security.workload_identity import (
    FakeWorkloadIdentityProvider,
    WorkloadIdentity,
)


def _generate_rsa_pems() -> tuple[str, str]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return priv_pem, pub_pem


def test_sign_and_verify_round_trip() -> None:
    priv_pem, pub_pem = generate_keypair()
    attest = sign_chain(
        priv_pem,
        run_id="run_abc",
        trusted_through_seq=42,
        chain_hash="abc123",
        signer="ci-bot",
    )
    assert isinstance(attest, Attestation)
    assert attest.algorithm == ATTESTATION_ALGORITHM
    assert attest.public_key == pub_pem
    assert verify_attestation(attest) is True
    assert verify_attestation(attest, expected_chain_hash="abc123") is True


def test_verify_rejects_wrong_chain_hash() -> None:
    priv_pem, _pub_pem = generate_keypair()
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123")
    assert verify_attestation(attest, expected_chain_hash="tampered") is False


def test_verify_rejects_tampered_payload() -> None:
    priv_pem, _pub_pem = generate_keypair()
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123")
    tampered = Attestation(**{**attest.to_dict(), "trusted_through_seq": 43})
    assert verify_attestation(tampered) is False


def test_verify_rejects_substituted_public_key() -> None:
    priv_pem, _pub_pem = generate_keypair()
    _other_priv, other_pub = generate_keypair()
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123")
    # An attacker swaps in a different public key but leaves the signature.
    swapped = Attestation(**{**attest.to_dict(), "public_key": other_pub})
    assert verify_attestation(swapped) is False


def test_sign_chain_rejects_non_ed25519_private_key() -> None:
    rsa_priv_pem, _rsa_pub_pem = _generate_rsa_pems()
    with pytest.raises(ValueError, match="attestation private key is not an Ed25519 key"):
        sign_chain(rsa_priv_pem, "run_abc", 42, "abc123")


def test_verify_rejects_altered_algorithm() -> None:
    import base64

    from continuum.security.attestation import _payload_bytes

    priv_pem, _pub_pem = generate_keypair()
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123")
    altered = {**attest.to_dict(), "algorithm": "rsa+sha256"}
    priv = serialization.load_pem_private_key(priv_pem.encode("ascii"), password=None)
    altered["signature"] = base64.b64encode(priv.sign(_payload_bytes(altered))).decode("ascii")
    assert verify_attestation(altered) is False


def test_verify_rejects_non_ed25519_public_key() -> None:
    priv_pem, _pub_pem = generate_keypair()
    _rsa_priv_pem, rsa_pub_pem = _generate_rsa_pems()
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123")
    swapped = {**attest.to_dict(), "public_key": rsa_pub_pem}
    assert verify_attestation(swapped) is False


def test_verify_invalid_workload_identity_dict_falls_back_to_none() -> None:
    priv_pem, _pub_pem = generate_keypair()
    attest = sign_chain(
        priv_pem,
        "run_abc",
        42,
        "abc123",
        workload_identity={"unrelated": True},
    )
    assert verify_attestation(attest, require_workload_identity=True) is False
    assert verify_attestation(attest, require_workload_identity=False) is True
    unbound_attest = sign_chain(priv_pem, "run_abc", 42, "abc123")
    assert verify_attestation(unbound_attest, require_workload_identity=True) is False


def test_verify_expected_workload_identity_object_and_string() -> None:
    priv_pem, _pub_pem = generate_keypair()
    wid = WorkloadIdentity(spiffe_id="spiffe://example.org/ns/prod/sa/worker")
    other_wid = WorkloadIdentity(spiffe_id="spiffe://example.org/ns/prod/sa/other")
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123", workload_identity=wid)

    assert verify_attestation(attest, expected_workload_identity=wid) is True
    assert verify_attestation(attest, expected_workload_identity=wid.spiffe_id) is True
    assert verify_attestation(attest, expected_workload_identity=other_wid) is False
    assert verify_attestation(attest, expected_workload_identity=other_wid.spiffe_id) is False


def test_verify_with_identity_provider() -> None:
    priv_pem, _pub_pem = generate_keypair()
    spiffe_id = "spiffe://example.org/ns/prod/sa/worker"
    wid = WorkloadIdentity(spiffe_id=spiffe_id)
    attest = sign_chain(priv_pem, "run_abc", 42, "abc123", workload_identity=wid)
    unbound_attest = sign_chain(priv_pem, "run_abc", 42, "abc123")

    valid_provider = FakeWorkloadIdentityProvider(spiffe_id=spiffe_id, valid=True)
    invalid_provider = FakeWorkloadIdentityProvider(spiffe_id=spiffe_id, valid=False)

    assert verify_attestation(attest, identity_provider=valid_provider) is True
    assert verify_attestation(attest, identity_provider=invalid_provider) is False
    assert verify_attestation(unbound_attest, identity_provider=valid_provider) is False
