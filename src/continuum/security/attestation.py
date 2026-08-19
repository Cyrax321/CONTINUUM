"""Optional cryptographic attestation for CONTINUUM event chains.

CONTINUUM already builds a hash-chained, tamper-evident event log:
``storage/sqlite.py`` rejects any append whose ``prev_hash`` does not match the
current head, and whose ``hash`` does not equal ``event.digest()``. That catches
*accidental* corruption and reordering.

This module adds an *authenticity* claim on top: a signer proves with an
Ed25519 key that "this run's chain, trusted through sequence ``N`` with root hash
``H``, had not been altered as of this signature." It composes with the existing
integrity layer, it does not replace it, and it is deliberately optional so the
core recovery path needs no crypto dependency.

Nothing here is imported by the core recovery path, which must need no crypto
dependency. The CLI surfaces ``continuum attest`` and ``continuum attest-verify``
(plus ``attest-keygen``) wire these primitives to a real run's live head and are
covered by ``tests/test_attestation.py`` and the attest cases in
``tests/test_cli.py`` (see the design doc ``references/attestation.md``).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from continuum.security.hashing import to_json

__all__ = [
    "ATTESTATION_ALGORITHM",
    "Attestation",
    "generate_keypair",
    "sign_chain",
    "verify_attestation",
]

ATTESTATION_ALGORITHM = "ed25519+sha256"


def _require_crypto() -> tuple[Any, Any, Any]:
    """Import the crypto primitives lazily so the core never depends on them."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:  # pragma: no cover - depends on install
        raise RuntimeError(
            "attestation requires the 'cryptography' package; install continuum-agent[attest]"
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey, serialization


@dataclass(frozen=True, slots=True)
class Attestation:
    """A signed claim that a run's chain was intact up to a point.

    ``chain_hash`` is the event-log root (the head event's ``hash``) and
    ``trusted_through_seq`` is the sequence number that hash covers, taken from
    the run's ``trusted_through`` record. ``signature`` is over the canonical
    JSON of every field except ``signature`` itself.
    """

    run_id: str
    trusted_through_seq: int
    chain_hash: str
    signer: str | None
    timestamp: str
    public_key: str
    signature: str
    algorithm: str = ATTESTATION_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trusted_through_seq": self.trusted_through_seq,
            "chain_hash": self.chain_hash,
            "signer": self.signer,
            "timestamp": self.timestamp,
            "public_key": self.public_key,
            "algorithm": self.algorithm,
            "signature": self.signature,
        }


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_pem, public_pem)`` as PEM text, for the signer to keep."""
    Ed25519PrivateKey, _Ed25519PublicKey, serialization = _require_crypto()
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    pub_pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return priv_pem, pub_pem


def _payload_bytes(attestation: Mapping[str, Any]) -> bytes:
    """Canonical bytes covered by the signature (every field but ``signature``)."""
    covered = {k: v for k, v in attestation.items() if k != "signature"}
    return to_json(covered).encode("utf-8")


def sign_chain(
    private_pem: str,
    run_id: str,
    trusted_through_seq: int,
    chain_hash: str,
    *,
    signer: str | None = None,
    timestamp: str | None = None,
) -> Attestation:
    """Sign a claim that ``run_id`` is intact through ``trusted_through_seq``."""
    Ed25519PrivateKey, _Ed25519PublicKey, serialization = _require_crypto()
    priv = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("attestation private key is not an Ed25519 key")

    from datetime import UTC, datetime

    ts = timestamp or datetime.now(UTC).isoformat()
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    draft: dict[str, Any] = {
        "run_id": run_id,
        "trusted_through_seq": trusted_through_seq,
        "chain_hash": chain_hash,
        "signer": signer,
        "timestamp": ts,
        "public_key": pub_pem,
        "algorithm": ATTESTATION_ALGORITHM,
    }
    signature = priv.sign(_payload_bytes(draft))
    draft["signature"] = base64.b64encode(signature).decode("ascii")
    return Attestation(
        run_id=run_id,
        trusted_through_seq=trusted_through_seq,
        chain_hash=chain_hash,
        signer=signer,
        timestamp=ts,
        public_key=pub_pem,
        signature=draft["signature"],
        algorithm=ATTESTATION_ALGORITHM,
    )


def verify_attestation(
    attestation: Attestation | Mapping[str, Any],
    *,
    expected_chain_hash: str | None = None,
) -> bool:
    """Return ``True`` iff the signature is valid and (optionally) the hash matches.

    Verification only proves the signature was made by the embedded public key;
    it does not by itself prove the chain is *currently* trusted. Callers should
    also compare ``chain_hash`` against the run's live head and, for a third
    party, check ``public_key`` against a known signer.
    """
    _Ed25519PrivateKey, Ed25519PublicKey, serialization = _require_crypto()
    data = attestation.to_dict() if isinstance(attestation, Attestation) else dict(attestation)
    if data.get("algorithm") != ATTESTATION_ALGORITHM:
        return False
    try:
        pub = serialization.load_pem_public_key(data["public_key"].encode("ascii"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        signature = base64.b64decode(data["signature"])
        pub.verify(signature, _payload_bytes(data))
    except Exception:  # noqa: BLE001 - any failure means "not valid"
        return False
    return expected_chain_hash is None or data.get("chain_hash") == expected_chain_hash
