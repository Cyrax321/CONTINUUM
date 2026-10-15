"""Workload identity bindings and SPIFFE integration for event-chain attestations (issue #745).

Key-based attestation proves an authorized signer verified a chain's integrity,
but key custody alone does not establish which executing workload produced it.
This module defines a provider-agnostic ``WorkloadIdentity`` protocol and SVID
binding helpers so attestation signatures can cryptographically bind workload
identity claims (e.g. SPIFFE IDs).

Core installs remain zero-dependency; SPIRE and ``pyspiffe`` are optional and
imported lazily. A deterministic ``FakeWorkloadIdentityProvider`` allows full
testability without a running SPIRE agent.
"""

from __future__ import annotations

import os
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


def _parse_trust_domain(spiffe_id: str) -> str:
    """Extract trust domain from a SPIFFE ID (spiffe://<trust-domain>/...)."""
    if not spiffe_id.startswith("spiffe://"):
        raise ValueError(f"malformed SPIFFE ID: must begin with 'spiffe://', got {spiffe_id!r}")
    remainder = spiffe_id[len("spiffe://") :]
    parts = remainder.split("/", 1)
    if not parts[0]:
        raise ValueError(f"malformed SPIFFE ID: missing trust domain in {spiffe_id!r}")
    return parts[0]


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
        import re
        if not re.match(r"^spiffe://[a-zA-Z0-9.\-_]+(/[a-zA-Z0-9.\-_/]*)*$", self.spiffe_id):
            raise ValueError(f"Invalid SPIFFE ID: {self.spiffe_id}")
        td = _parse_trust_domain(self.spiffe_id)
        if self.trust_domain is not None and self.trust_domain != td:
            raise ValueError(f"trust_domain mismatch: {self.trust_domain} vs {td}")
        if self.trust_domain is None:
            object.__setattr__(self, "trust_domain", td)

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
        """Verify that an identity matches expected format and live SVID."""
        try:
            td = _parse_trust_domain(identity.spiffe_id)
        except ValueError:
            return False
        if self.trust_domain and td != self.trust_domain:
            return False
        try:
            current = self.get_identity()
        except WorkloadIdentityUnavailableError:
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
