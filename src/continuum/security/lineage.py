"""Portable lineage tokens for delegated work (issue #760).

``attestation.py`` lets a signer prove "this run's chain was intact as of this
signature". What it does not give a *downstream* system is a bounded, self-
contained handoff artifact: an attestation says a chain was intact, nothing
about who handed the work to whom or for what purpose, and a verifier with no
access to the source store still cannot relate the signature to a checkpoint or
a recovery contract.

This module defines that handoff artifact. A **lineage token** binds, under one
signature:

* the source run, the checkpoint version and the event-chain point
  (``trusted_through_seq`` / ``chain_hash``) the delegation covers;
* the *sealed* recovery contract, by integrity hash, so the terms the source
  reached are named exactly, not summarized;
* the embedded attestation, which carries its own independent signature over the
  chain point;
* the issuer's key identity, an optional audience, an explicit delegation
  purpose, and issuance / expiry times.

The token is evidence of origin and delegation. It is **not** a capability: a
holder cannot append to the source run, cannot resume it, and cannot claim its
contract's permissions. Downstream verification never writes state and never
implies authorization to mutate the source run; a holder who needs to act still
needs the gate, the ledger and a run that is actually theirs.

Fields are bounded by construction. The envelope carries no secrets (no private
keys, no credentials), no raw environment payloads, and no event history: the
chain is represented by one hash and one sequence number, and the contract by
one integrity hash. A token is therefore safe to hand to an untrusted
downstream system, which is the point.

The format is provider-neutral. Key-based attestations work today; the issuer
identity is a digest of the public key, so the workload-identity binding in #745
can later mint the same envelope by supplying its own key without a format
change, and a token whose attestation was signed by a different key than the
issuer's is valid: attesting a chain and delegating work are separate acts.

See ``references/attestation.md`` for the trust boundary and the design limits.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from continuum.models import RecoveryContract
from continuum.recovery.contract import verify_contract
from continuum.security.attestation import (
    ATTESTATION_ALGORITHM,
    Attestation,
    verify_attestation,
)
from continuum.security.hashing import stable_hash, to_json

__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "SUPPORTED_TOKEN_VERSIONS",
    "TOKEN_VERSION",
    "InvalidAttestationError",
    "LineageToken",
    "LineageTokenError",
    "TokenParseError",
    "TokenVerification",
    "TokenVerdict",
    "UnsealedContractError",
    "issue_token",
    "key_id",
    "parse_token",
    "verify_token",
]

#: The version this module issues. Bumping it is a format change: a verifier
#: that does not know a version must refuse it rather than guess its fields.
TOKEN_VERSION = "lineage-v1"
#: Every version ``verify_token`` accepts. Anything else fails closed.
SUPPORTED_TOKEN_VERSIONS = frozenset({TOKEN_VERSION})
#: Default lifetime when neither ``expires_at`` nor ``ttl_seconds`` is given.
#: A delegation that never expires is a standing credential, which this token
#: is explicitly not, so expiry is always present and always finite.
DEFAULT_TOKEN_TTL_SECONDS = 3600
SIGNATURE_ALGORITHM = ATTESTATION_ALGORITHM

# Fields a serialized token must carry. Optional human-facing fields (issuer,
# audience) are excluded: a token without an audience is legitimate, a token
# without a chain point or a signature is not.
_REQUIRED_FIELDS = (
    "run_id",
    "checkpoint_version",
    "trusted_through_seq",
    "chain_hash",
    "contract_integrity_hash",
    "attestation",
    "purpose",
    "issued_at",
    "expires_at",
    "public_key",
    "signature",
    "version",
)


class LineageTokenError(ValueError):
    """Base class for token failures. Never raised for a *verdict*; see below.

    Issuance is all-or-nothing: a token minted over an unsealed contract or an
    unverifiable attestation would lend its signature to inputs the issuer did
    not actually stand behind, so those are hard errors, not warnings.
    """


class UnsealedContractError(LineageTokenError):
    """The contract's integrity hash does not match its own terms."""


class InvalidAttestationError(LineageTokenError):
    """The attestation does not verify, or does not belong to this contract."""


class TokenParseError(LineageTokenError):
    """A serialized token is structurally invalid."""


class TokenVerdict(StrEnum):
    """What ``verify_token`` concluded. Only ``VALID`` authorizes trust."""

    VALID = "VALID"
    MALFORMED = "MALFORMED"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    TAMPERED = "TAMPERED"
    EXPIRED = "EXPIRED"
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    UNKNOWN_ISSUER = "UNKNOWN_ISSUER"
    BROKEN_REFERENCE = "BROKEN_REFERENCE"


@dataclass(frozen=True, slots=True)
class LineageToken:
    """A signed, versioned lineage handoff artifact.

    ``signature`` is an Ed25519 signature by the issuer's key over the canonical
    JSON of every other field. The embedded ``attestation`` carries its own
    signature over the chain point, usually by the same key but not required to
    be: attesting and delegating are separate acts (see the module docstring).
    """

    run_id: str
    checkpoint_version: int
    trusted_through_seq: int
    chain_hash: str
    contract_integrity_hash: str
    attestation: dict[str, Any]
    purpose: str
    issued_at: str
    expires_at: str
    public_key: str
    signature: str
    issuer: str | None = None
    audience: str | None = None
    version: str = TOKEN_VERSION
    algorithm: str = SIGNATURE_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "algorithm": self.algorithm,
            "run_id": self.run_id,
            "checkpoint_version": self.checkpoint_version,
            "trusted_through_seq": self.trusted_through_seq,
            "chain_hash": self.chain_hash,
            "contract_integrity_hash": self.contract_integrity_hash,
            "attestation": self.attestation,
            "issuer": self.issuer,
            "audience": self.audience,
            "purpose": self.purpose,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "public_key": self.public_key,
            "signature": self.signature,
        }

    def to_canonical_json(self) -> str:
        """Byte-stable serialization, independent of field or key order.

        Two tokens with identical values serialize identically, which is what
        makes a token diffable, cacheable and reproducible in tests.
        """
        return to_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class TokenVerification:
    """The outcome of :func:`verify_token`, with every reason it reached it.

    ``reasons`` lists blocking problems in precedence order; ``advisories``
    lists checks that were *not* performed, so a caller never mistakes "no
    blocking reason found" for "everything that could be checked was".
    """

    verdict: TokenVerdict
    reasons: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    token: LineageToken | None = None


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
            "lineage tokens require the 'cryptography' package; install continuum-agent[attest]"
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey, serialization


def key_id(public_key: str) -> str:
    """Deterministic identity for a public key, in the form ``sha256:<digest>``.

    Two tokens from the same key share it, so a verifier's trusted-issuer set is
    a set of these, not a set of PEM blobs to compare byte for byte. PEM spelling
    is normalized first: a keyring file with or without a trailing newline, or
    with CRLF line endings, is the same key, and an operator's trusted-issuer
    match must not depend on how their editor saved the file.
    """
    normalized = "\n".join(line.strip() for line in public_key.strip().splitlines())
    return "sha256:" + stable_hash(normalized)


def _parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 instant, accepting the trailing ``Z`` spelling.

    A naive timestamp is read as UTC rather than rejected: the attestation
    module writes ``datetime.now(UTC).isoformat()`` and the two must interoperate.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _signed_payload(doc: Mapping[str, Any]) -> bytes:
    """Canonical bytes the signature covers: everything except the signature."""
    return to_json({k: v for k, v in doc.items() if k != "signature"}).encode("utf-8")


def _issuer_public_key(private_pem: str) -> str:
    _Ed25519PrivateKey, _Ed25519PublicKey, serialization = _require_crypto()
    priv = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(priv, _Ed25519PrivateKey):
        raise ValueError("lineage token issuer key is not an Ed25519 key")
    public_pem: str = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return public_pem


def _sign(private_pem: str, doc: Mapping[str, Any]) -> str:
    Ed25519PrivateKey, _Ed25519PublicKey, serialization = _require_crypto()
    priv = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("lineage token issuer key is not an Ed25519 key")
    return base64.b64encode(priv.sign(_signed_payload(doc))).decode("ascii")


def _signature_is_valid(token: LineageToken) -> bool:
    _Ed25519PrivateKey, Ed25519PublicKey, serialization = _require_crypto()
    try:
        pub = serialization.load_pem_public_key(token.public_key.encode("ascii"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        pub.verify(base64.b64decode(token.signature), _signed_payload(token.to_dict()))
    except Exception:
        return False
    return True


def parse_token(doc: str | Mapping[str, Any]) -> LineageToken:
    """Rebuild a token from JSON text or a mapping, validating its shape.

    This checks structure only: fields are present and well-typed, timestamps
    parse, and the embedded attestation is a mapping. Authenticity is a separate
    check (``verify_token``), because a structurally perfect forgery is still a
    forgery, and the two failures deserve different diagnostics.
    """
    if isinstance(doc, str):
        try:
            data = json.loads(doc)
        except json.JSONDecodeError as exc:
            raise TokenParseError(f"token is not valid JSON: {exc.msg}") from exc
    else:
        data = dict(doc)
    if not isinstance(data, Mapping):
        raise TokenParseError("a token must be a JSON object")

    missing = [name for name in _REQUIRED_FIELDS if data.get(name) in (None, "")]
    if missing:
        raise TokenParseError(f"token is missing required field(s): {', '.join(sorted(missing))}")

    if not isinstance(data.get("attestation"), Mapping):
        raise TokenParseError("the 'attestation' field must be a JSON object")

    for name in ("run_id", "chain_hash", "contract_integrity_hash", "purpose", "public_key"):
        if not isinstance(data[name], str):
            raise TokenParseError(f"field {name!r} must be a string")
    for name in ("checkpoint_version", "trusted_through_seq"):
        # bool is an int subclass, and a boolean checkpoint version is a bug,
        # not a quirky encoding.
        if isinstance(data[name], bool) or not isinstance(data[name], int):
            raise TokenParseError(f"field {name!r} must be an integer")
    if isinstance(data.get("issuer"), str) is False and data.get("issuer") is not None:
        raise TokenParseError("field 'issuer' must be a string when present")
    if isinstance(data.get("audience"), str) is False and data.get("audience") is not None:
        raise TokenParseError("field 'audience' must be a string when present")

    for name in ("issued_at", "expires_at"):
        try:
            _parse_instant(data[name])
        except ValueError as exc:
            raise TokenParseError(f"field {name!r} is not a valid ISO-8601 instant: {exc}") from exc

    return LineageToken(
        run_id=data["run_id"],
        checkpoint_version=data["checkpoint_version"],
        trusted_through_seq=data["trusted_through_seq"],
        chain_hash=data["chain_hash"],
        contract_integrity_hash=data["contract_integrity_hash"],
        attestation=dict(data["attestation"]),
        issuer=data.get("issuer"),
        audience=data.get("audience"),
        purpose=data["purpose"],
        issued_at=data["issued_at"],
        expires_at=data["expires_at"],
        public_key=data["public_key"],
        signature=data["signature"],
        version=data["version"],
        algorithm=data.get("algorithm", SIGNATURE_ALGORITHM),
    )


def issue_token(
    contract: RecoveryContract,
    attestation: Attestation | Mapping[str, Any],
    private_pem: str,
    *,
    purpose: str,
    audience: str | None = None,
    issuer: str | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
    ttl_seconds: float | None = None,
) -> LineageToken:
    """Mint a lineage token binding a sealed contract and a verified attestation.

    ``contract`` and ``attestation`` describe the source run; ``private_pem`` is
    the issuer's key, which may or may not be the key that signed the
    attestation. The token records the contract's *seal*, not its verdict: a
    token over a run that needs repair is still honest evidence of lineage, and a
    downstream system that wants the verdict reads it from the contract it
    already has, comparing seals.

    Raises:
        UnsealedContractError: the contract has no integrity hash, or its hash
            does not match its own terms (it was edited after sealing).
        InvalidAttestationError: the attestation's signature does not verify, or
            it attests a different run than the contract describes.
        ValueError: the purpose is empty, or the expiry is not after issuance.
    """
    if contract.integrity_hash is None or not verify_contract(contract):
        raise UnsealedContractError(
            "the contract's integrity hash does not match its own terms; a token over an "
            "unsealed contract would lend its signature to terms anyone could edit afterward"
        )
    attest_doc = (
        attestation.to_dict() if isinstance(attestation, Attestation) else dict(attestation)
    )
    if not verify_attestation(attest_doc):
        raise InvalidAttestationError(
            "the attestation's own signature does not verify against its embedded public key"
        )
    if attest_doc.get("run_id") != contract.run_id:
        raise InvalidAttestationError(
            f"the attestation is for run {attest_doc.get('run_id')!r} but the contract is for "
            f"{contract.run_id!r}; a token must not bind two different runs"
        )
    if not purpose or not purpose.strip():
        raise ValueError(
            "a delegation purpose is required: a token with no purpose is a standing, "
            "unbounded credential rather than a bounded handoff"
        )
    if expires_at is not None and ttl_seconds is not None:
        raise ValueError("pass either expires_at or ttl_seconds, not both")

    start = _parse_instant(issued_at) if issued_at else datetime.now(UTC)
    if expires_at is None:
        end = start + timedelta(
            seconds=DEFAULT_TOKEN_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        )
    else:
        end = _parse_instant(expires_at)
    if end <= start:
        raise ValueError(
            f"expiry {end.isoformat()} is not after issuance {start.isoformat()}; "
            "a token must be valid at the moment it is minted"
        )

    public_pem = _issuer_public_key(private_pem)
    draft: dict[str, Any] = {
        "version": TOKEN_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "run_id": contract.run_id,
        "checkpoint_version": contract.checkpoint_version,
        "trusted_through_seq": attest_doc["trusted_through_seq"],
        "chain_hash": attest_doc["chain_hash"],
        "contract_integrity_hash": contract.integrity_hash,
        "attestation": attest_doc,
        "issuer": issuer,
        "audience": audience,
        "purpose": purpose,
        "issued_at": start.isoformat(),
        "expires_at": end.isoformat(),
        "public_key": public_pem,
    }
    return LineageToken(
        run_id=contract.run_id,
        checkpoint_version=contract.checkpoint_version,
        trusted_through_seq=attest_doc["trusted_through_seq"],
        chain_hash=attest_doc["chain_hash"],
        contract_integrity_hash=contract.integrity_hash,
        attestation=attest_doc,
        issuer=issuer,
        audience=audience,
        purpose=purpose,
        issued_at=start.isoformat(),
        expires_at=end.isoformat(),
        public_key=public_pem,
        signature=_sign(private_pem, draft),
    )


def verify_token(
    token: LineageToken | str | Mapping[str, Any],
    *,
    expected_audience: str | None = None,
    trusted_issuers: Iterable[str] | None = None,
    contract: RecoveryContract | None = None,
    expected_run_id: str | None = None,
    now: str | None = None,
) -> TokenVerification:
    """Verify a lineage token without writing any state.

    Checks, in precedence order (the first blocking problem sets the verdict):

    1. **Structure** (``MALFORMED``): the envelope parses and its fields are
       well-typed.
    2. **Version** (``UNSUPPORTED_VERSION``): the version is one this verifier
       knows. An unknown version is refused rather than best-effort parsed,
       because guessing at a future format's semantics is how a token from a
       stricter scheme gets read as permissive.
    3. **Signature** (``TAMPERED``): the issuer's signature verifies against the
       embedded public key.
    4. **Expiry** (``EXPIRED``): ``expires_at`` is in the future.
    5. **Audience** (``WRONG_AUDIENCE``): if the token binds an audience, the
       caller must supply a matching one. A token minted for service A is then
       not replayable against service B simply because B declined to check.
    6. **Issuer** (``UNKNOWN_ISSUER``): if the caller named trusted issuers, the
       token's key is among them.
    7. **References** (``BROKEN_REFERENCE``): the embedded attestation verifies,
       it describes this token's run and chain point, and, when the caller
       supplies the read-only contract, that contract is sealed and matches the
       token's ``contract_integrity_hash``, run and checkpoint version.

    A caller with no access to the source store at all can omit ``contract`` and
    still establish authenticity, expiry, audience and issuer. The contract
    check is then simply not performed and is reported in ``advisories``, so a
    ``VALID`` verdict never overstates what was actually checked.

    The verdict is authoritative; ``reasons`` past the blocking one are
    diagnostic. A token whose signature failed also reports its audience
    mismatch, but the audience of a forged token is not evidence of anything.
    """
    if isinstance(token, LineageToken):
        parsed = token
    else:
        try:
            parsed = parse_token(token)
        except TokenParseError as exc:
            return TokenVerification(TokenVerdict.MALFORMED, [str(exc)])

    blocking: list[tuple[TokenVerdict, str]] = []
    advisories: list[str] = []

    def block(verdict: TokenVerdict, reason: str) -> None:
        blocking.append((verdict, reason))

    if parsed.version not in SUPPORTED_TOKEN_VERSIONS:
        block(
            TokenVerdict.UNSUPPORTED_VERSION,
            f"token version {parsed.version!r} is unsupported "
            f"(this verifier accepts: {sorted(SUPPORTED_TOKEN_VERSIONS)})",
        )
    if not _signature_is_valid(parsed):
        block(
            TokenVerdict.TAMPERED,
            "the token signature does not verify against the embedded public key",
        )

    now_dt = _parse_instant(now) if now else datetime.now(UTC)
    if _parse_instant(parsed.expires_at) <= now_dt:
        block(
            TokenVerdict.EXPIRED,
            f"expired at {parsed.expires_at}, which is not after now ({now_dt.isoformat()})",
        )

    # Audience binding is optional at issuance and mandatory at verification
    # when present: a bound token replayed to a verifier that does not check
    # would be exactly the confusion the field exists to prevent.
    if parsed.audience is None:
        if expected_audience is not None:
            block(
                TokenVerdict.WRONG_AUDIENCE,
                f"the token binds no audience, but {expected_audience!r} was expected",
            )
    elif expected_audience is None:
        block(
            TokenVerdict.WRONG_AUDIENCE,
            f"the token is bound to audience {parsed.audience!r} but no expected audience was "
            "supplied; supply it to confirm this token was minted for this recipient",
        )
    elif parsed.audience != expected_audience:
        block(
            TokenVerdict.WRONG_AUDIENCE,
            f"audience mismatch: the token is for {parsed.audience!r}, expected "
            f"{expected_audience!r}",
        )

    if trusted_issuers is not None:
        accepted = {
            identity
            for issuer_key in trusted_issuers
            for identity in (issuer_key, key_id(issuer_key))
        }
        if parsed.public_key not in accepted and key_id(parsed.public_key) not in accepted:
            block(
                TokenVerdict.UNKNOWN_ISSUER,
                f"issuer {key_id(parsed.public_key)} is not in the trusted-issuer set",
            )

    if expected_run_id is not None and parsed.run_id != expected_run_id:
        block(
            TokenVerdict.BROKEN_REFERENCE,
            f"the token names run {parsed.run_id!r}, expected {expected_run_id!r}",
        )

    if not verify_attestation(parsed.attestation):
        block(
            TokenVerdict.BROKEN_REFERENCE,
            "the embedded attestation's signature does not verify against its own public key",
        )
    elif (
        parsed.attestation.get("run_id") != parsed.run_id
        or parsed.attestation.get("chain_hash") != parsed.chain_hash
        or parsed.attestation.get("trusted_through_seq") != parsed.trusted_through_seq
    ):
        block(
            TokenVerdict.BROKEN_REFERENCE,
            "the embedded attestation describes a different run or chain point than the token",
        )

    if contract is None:
        advisories.append(
            "no contract supplied, so the sealed terms were not re-checked against the "
            "token's contract_integrity_hash; supply the read-only contract to bind them"
        )
    else:
        if contract.integrity_hash is None or not verify_contract(contract):
            block(
                TokenVerdict.BROKEN_REFERENCE,
                "the supplied contract's integrity hash does not match its own terms",
            )
        elif contract.integrity_hash != parsed.contract_integrity_hash:
            block(
                TokenVerdict.BROKEN_REFERENCE,
                "the supplied contract's seal does not match the token's "
                "contract_integrity_hash, so the token was not issued over these terms",
            )
        if contract.run_id != parsed.run_id:
            block(
                TokenVerdict.BROKEN_REFERENCE,
                f"the supplied contract is for run {contract.run_id!r}, the token for "
                f"{parsed.run_id!r}",
            )
        if contract.checkpoint_version != parsed.checkpoint_version:
            block(
                TokenVerdict.BROKEN_REFERENCE,
                f"checkpoint mismatch: the contract is at v{contract.checkpoint_version}, "
                f"the token at v{parsed.checkpoint_version}",
            )

    verdict = blocking[0][0] if blocking else TokenVerdict.VALID
    return TokenVerification(
        verdict=verdict,
        reasons=[reason for _verdict, reason in blocking],
        advisories=advisories,
        token=parsed,
    )
