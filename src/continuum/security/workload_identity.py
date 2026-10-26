"""Workload identity bindings and SPIFFE integration for event-chain attestations (issue #745).

Key-based attestation proves an authorized signer verified a chain's integrity,
but key custody alone does not establish which executing workload produced it.
This module defines a provider-agnostic ``WorkloadIdentity`` protocol and SVID
binding helpers so attestation signatures can cryptographically bind workload
identity claims (e.g. SPIFFE IDs).

Core installs remain zero-dependency; SPIRE and the ``spiffe`` package
(Hewlett Packard's py-spiffe, published on PyPI as ``spiffe``) are optional
and imported lazily. A deterministic ``FakeWorkloadIdentityProvider`` allows
full testability without a running SPIRE agent.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "WorkloadIdentity",
    "WorkloadIdentityError",
    "WorkloadIdentityMismatchError",
    "WorkloadIdentityProvider",
    "WorkloadIdentityUnavailableError",
    "FakeWorkloadIdentityProvider",
    "SpiffeIdentityProvider",
    "resolve_workload_provider",
]


class WorkloadIdentityError(Exception):
    """Base error for workload identity operations."""


class WorkloadIdentityUnavailableError(WorkloadIdentityError):
    """Raised when a required workload identity cannot be acquired from the provider."""


class WorkloadIdentityMismatchError(WorkloadIdentityError):
    """Raised when an attestation's bound identity disagrees with the executing workload."""


_TRUST_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _parse_trust_domain(spiffe_id: str) -> str:
    """Extract trust domain from a SPIFFE ID (spiffe://<trust-domain>/...)."""
    if not spiffe_id.startswith("spiffe://"):
        raise ValueError(f"malformed SPIFFE ID: must begin with 'spiffe://', got {spiffe_id!r}")
    remainder = spiffe_id[len("spiffe://") :]
    parts = remainder.split("/", 1)
    if not parts[0]:
        raise ValueError(f"malformed SPIFFE ID: missing trust domain in {spiffe_id!r}")
    return parts[0]


def _validate_spiffe_id(spiffe_id: str) -> None:
    """Validate a canonical SPIFFE ID end to end.

    Mirrors the checks a real SPIFFE implementation (py-spiffe) applies, so a
    caller cannot mint an identity other implementations reject: the trust
    domain is a dot-separated host name, the path has no empty, ``.`` or ``..``
    segments, and whitespace, query and fragment components are not allowed.
    """
    if any(ch.isspace() for ch in spiffe_id):
        raise ValueError(f"malformed SPIFFE ID: whitespace is not allowed in {spiffe_id!r}")
    if "?" in spiffe_id or "#" in spiffe_id:
        raise ValueError(f"malformed SPIFFE ID: query or fragment is not allowed in {spiffe_id!r}")
    # Scheme and empty-domain checks stay with _parse_trust_domain so the
    # diagnostics callers already match on ('must begin with spiffe://',
    # 'missing trust domain') survive the stricter walk below.
    trust_domain = _parse_trust_domain(spiffe_id)
    if not _TRUST_DOMAIN_RE.match(trust_domain):
        raise ValueError(
            f"malformed SPIFFE ID: invalid trust domain {trust_domain!r} in {spiffe_id!r}"
        )
    parts = spiffe_id[len("spiffe://") :].split("/", 1)
    path = parts[1] if len(parts) > 1 else ""
    for segment in path.split("/"):
        if not segment:
            raise ValueError(f"malformed SPIFFE ID: empty path segment in {spiffe_id!r}")
        if segment in (".", ".."):
            raise ValueError(
                f"malformed SPIFFE ID: path segments '.' and '..' are not allowed in {spiffe_id!r}"
            )
        if not _PATH_SEGMENT_RE.match(segment):
            raise ValueError(
                f"malformed SPIFFE ID: invalid character in path segment {segment!r} "
                f"of {spiffe_id!r}"
            )


@dataclass(frozen=True, slots=True)
class WorkloadIdentity:
    """A verified workload identity claim to bind into an attestation.

    ``spiffe_id`` is the canonical SPIFFE ID URI (e.g. 'spiffe://example.org/ns/prod/sa/agent').
    ``provider`` names the provider ('spiffe', 'fake', or a custom adapter).
    ``attributes`` holds optional provider-specific claims (e.g. federated trust domain).
    """

    spiffe_id: str
    provider: str = "spiffe"
    trust_domain: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.spiffe_id or not isinstance(self.spiffe_id, str):
            raise ValueError("spiffe_id must be a non-empty string")
        _validate_spiffe_id(self.spiffe_id)
        td = _parse_trust_domain(self.spiffe_id)
        if self.trust_domain is None:
            object.__setattr__(self, "trust_domain", td)
        elif self.trust_domain != td:
            raise ValueError(
                f"trust_domain {self.trust_domain!r} disagrees with the trust domain "
                f"in spiffe_id {self.spiffe_id!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical JSON-serializable dictionary."""
        doc: dict[str, Any] = {
            "spiffe_id": self.spiffe_id,
            "provider": self.provider,
            "trust_domain": self.trust_domain,
        }
        if self.attributes:
            doc["attributes"] = dict(self.attributes)
        return doc

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkloadIdentity:
        """Construct from a dictionary, validating required fields."""
        if not isinstance(data, Mapping):
            raise ValueError("workload identity data must be a mapping")
        spiffe_id = data.get("spiffe_id")
        if not spiffe_id or not isinstance(spiffe_id, str):
            raise ValueError("missing or invalid 'spiffe_id' in workload identity mapping")
        provider = str(data.get("provider", "spiffe"))
        trust_domain = data.get("trust_domain")
        attributes = data.get("attributes")
        attrs = dict(attributes) if isinstance(attributes, Mapping) else {}
        return cls(
            spiffe_id=spiffe_id,
            provider=provider,
            trust_domain=trust_domain,
            attributes=attrs,
        )


@runtime_checkable
class WorkloadIdentityProvider(Protocol):
    """Provider-agnostic interface for acquiring and verifying workload identities."""

    def get_identity(self) -> WorkloadIdentity:
        """Acquire the current workload identity, or raise WorkloadIdentityUnavailableError."""
        ...

    def verify_identity(self, identity: WorkloadIdentity) -> bool:
        """Verify whether an identity claim is valid under this provider."""
        ...


class FakeWorkloadIdentityProvider:
    """Deterministic workload identity provider for unit tests and local mock verification."""

    def __init__(
        self,
        spiffe_id: str = "spiffe://example.org/ns/default/sa/test-agent",
        *,
        valid: bool = True,
        available: bool = True,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.spiffe_id = spiffe_id
        self.valid = valid
        self.available = available
        self.attributes = dict(attributes or {})

    def get_identity(self) -> WorkloadIdentity:
        if not self.available:
            raise WorkloadIdentityUnavailableError(
                "fake workload identity provider is configured as unavailable"
            )
        return WorkloadIdentity(
            spiffe_id=self.spiffe_id,
            provider="fake",
            attributes=dict(self.attributes),
        )

    def verify_identity(self, identity: WorkloadIdentity) -> bool:
        if not self.valid:
            return False
        return identity.spiffe_id == self.spiffe_id


class SpiffeIdentityProvider:
    """SPIFFE/SPIRE adapter connecting to the SPIFFE Workload API."""

    def __init__(
        self,
        socket_path: str | None = None,
        *,
        trust_domain: str | None = None,
    ) -> None:
        self.socket_path = socket_path or os.environ.get("SPIFFE_ENDPOINT_SOCKET")
        self.trust_domain = trust_domain or os.environ.get("SPIFFE_TRUST_DOMAIN")

    def _spiffe_client_cls(self) -> Any:
        """Import the Workload API client lazily (the [spiffe] extra is optional)."""
        try:
            from spiffe.workloadapi.workload_api_client import WorkloadApiClient
        except ImportError as exc:
            raise WorkloadIdentityUnavailableError(
                "SPIFFE workload identity requires the 'spiffe' package; "
                "install continuum-agent[spiffe]"
            ) from exc
        return WorkloadApiClient

    def get_identity(self) -> WorkloadIdentity:
        """Fetch the current X.509 SVID from the SPIFFE Workload API socket."""
        if not self.socket_path:
            raise WorkloadIdentityUnavailableError(
                "no SPIFFE Workload API socket configured: pass socket_path or set "
                "SPIFFE_ENDPOINT_SOCKET environment variable"
            )

        WorkloadApiClient = self._spiffe_client_cls()
        try:
            with WorkloadApiClient(socket_path=self.socket_path) as client:
                svid = client.fetch_x509_svid()
                spiffe_id_str = str(svid.spiffe_id)
                td = str(svid.spiffe_id.trust_domain)
                return WorkloadIdentity(
                    spiffe_id=spiffe_id_str,
                    provider="spiffe",
                    trust_domain=td,
                )
        except Exception as exc:
            if isinstance(exc, WorkloadIdentityUnavailableError):
                raise
            raise WorkloadIdentityUnavailableError(
                f"failed to fetch SPIFFE SVID from socket {self.socket_path!r}: {exc}"
            ) from exc

    def verify_identity(self, identity: WorkloadIdentity) -> bool:
        """Verify a claimed identity against the identity issued to this workload.

        A claim is accepted only when it is well-formed, matches the configured
        trust domain (when one is set), and equals the SVID the Workload API
        currently issued to this workload. A well-formed claim naming a
        *different* workload of the same trust domain is not authorized, and a
        current identity that cannot be fetched (missing socket, missing
        package) rejects the claim rather than trusting the trust-domain check
        alone: verification fails closed.
        """
        try:
            _validate_spiffe_id(identity.spiffe_id)
        except ValueError:
            return False
        td = _parse_trust_domain(identity.spiffe_id)
        if self.trust_domain is not None and td != self.trust_domain:
            return False
        try:
            current = self.get_identity()
        except WorkloadIdentityError:
            return False
        return current.spiffe_id == identity.spiffe_id


def resolve_workload_provider(
    provider: str | WorkloadIdentityProvider | None = None,
    **kwargs: Any,
) -> WorkloadIdentityProvider:
    """Resolve a provider instance from name or interface."""
    if isinstance(provider, WorkloadIdentityProvider):
        return provider
    name = (provider or os.environ.get("CONTINUUM_WORKLOAD_PROVIDER") or "spiffe").lower()
    if name == "fake":
        return FakeWorkloadIdentityProvider(**kwargs)
    if name == "spiffe":
        return SpiffeIdentityProvider(**kwargs)
    raise ValueError(f"unknown workload identity provider {name!r}; expected 'spiffe' or 'fake'")
