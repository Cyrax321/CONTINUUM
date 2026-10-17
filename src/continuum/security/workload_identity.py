from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "FakeWorkloadIdentityProvider",
    "SpiffeIdentityProvider",
    "WorkloadIdentity",
    "WorkloadIdentityError",
    "WorkloadIdentityProvider",
    "WorkloadIdentityUnavailableError",
    "resolve_workload_provider",
]


class WorkloadIdentityError(Exception):
    """Base exception for workload identity errors."""


class WorkloadIdentityUnavailableError(WorkloadIdentityError):
    """Raised when workload identity cannot be acquired or is unavailable."""


_TRUST_DOMAIN_RE = re.compile(r"^[a-z0-9.\-_]+$")
_PATH_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9.\-_]+$")


def _parse_trust_domain(spiffe_id: str) -> str:
    if not isinstance(spiffe_id, str) or not spiffe_id.startswith("spiffe://"):
        raise ValueError("must begin with 'spiffe://'")
    remainder = spiffe_id[len("spiffe://") :]
    if not remainder or remainder.startswith("/"):
        raise ValueError("missing trust domain")
    parts = remainder.split("/", 1)
    td = parts[0]
    if not td or not _TRUST_DOMAIN_RE.fullmatch(td):
        raise ValueError(f"Invalid SPIFFE ID: invalid trust domain '{td}'")
    return td


@dataclass
class WorkloadIdentity:
    spiffe_id: str
    provider: str = "spiffe"
    trust_domain: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.spiffe_id or not isinstance(self.spiffe_id, str):
            raise ValueError("spiffe_id must be a non-empty string")

        if "\n" in self.spiffe_id or "\r" in self.spiffe_id:
            raise ValueError(f"Invalid SPIFFE ID: {self.spiffe_id}")

        td = _parse_trust_domain(self.spiffe_id)

        remainder = self.spiffe_id[len("spiffe://") :]
        parts = remainder.split("/")
        if len(parts) > 1:
            for seg in parts[1:]:
                if not seg or not _PATH_SEGMENT_RE.fullmatch(seg):
                    raise ValueError(f"Invalid SPIFFE ID: invalid segment '{seg}'")

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
            raise ValueError("data must be a mapping")
        spiffe_id = data.get("spiffe_id")
        if not spiffe_id or not isinstance(spiffe_id, str):
            raise ValueError("spiffe_id must be a non-empty string")
        return cls(
            spiffe_id=spiffe_id,
            provider=data.get("provider", "spiffe"),
            trust_domain=data.get("trust_domain"),
            attributes=dict(data.get("attributes") or {}),
        )


@runtime_checkable
class WorkloadIdentityProvider(Protocol):
    """Provider-agnostic interface for acquiring and verifying workload identities."""

    def get_identity(self) -> WorkloadIdentity:
        """Acquire the current workload identity, or raise WorkloadIdentityError."""
        ...

    def verify_identity(self, identity: WorkloadIdentity | str) -> bool:
        """Verify whether the supplied workload identity is valid."""
        ...


class SpiffeIdentityProvider:
    """Default SPIFFE workload identity provider."""

    def __init__(self, trust_domain: str | None = None, socket_path: str | None = None) -> None:
        self.trust_domain = trust_domain
        self.socket_path = socket_path or os.environ.get("SPIFFE_ENDPOINT_SOCKET")

    def get_identity(self) -> WorkloadIdentity:
        if not self.socket_path or not os.path.exists(self.socket_path):
            raise WorkloadIdentityUnavailableError("no SPIFFE Workload API socket")
        td = self.trust_domain or "example.org"
        return WorkloadIdentity(
            spiffe_id=f"spiffe://{td}/workload/default", provider="spiffe", trust_domain=td
        )

    def verify_identity(self, identity: WorkloadIdentity | str) -> bool:
        sid = identity.spiffe_id if isinstance(identity, WorkloadIdentity) else str(identity)
        if self.trust_domain:
            return (
                sid.startswith(f"spiffe://{self.trust_domain}/")
                or sid == f"spiffe://{self.trust_domain}"
            )
        return True


class FakeWorkloadIdentityProvider:
    """Mock workload identity provider for tests."""

    def __init__(
        self,
        identity: WorkloadIdentity | str | None = None,
        spiffe_id: str | None = None,
        available: bool = True,
        valid: bool = True,
        fail: bool = False,
        **kwargs: Any,
    ) -> None:
        target = spiffe_id or identity
        if isinstance(target, str):
            self._identity = WorkloadIdentity(spiffe_id=target, provider="fake")
        elif isinstance(target, WorkloadIdentity):
            self._identity = target
        else:
            self._identity = WorkloadIdentity(
                spiffe_id="spiffe://example.org/ns/default/sa/test", provider="fake"
            )
        self._available = available and not fail
        self._valid = valid and not fail

    def get_identity(self) -> WorkloadIdentity:
        if not self._available:
            raise WorkloadIdentityUnavailableError("Workload identity unavailable")
        return self._identity

    def verify_identity(self, identity: WorkloadIdentity | str) -> bool:
        if not self._valid:
            return False
        sid = identity.spiffe_id if isinstance(identity, WorkloadIdentity) else str(identity)
        return sid == self._identity.spiffe_id


def resolve_workload_provider(
    provider_type: str | None = "spiffe", **kwargs: Any
) -> WorkloadIdentityProvider:
    """Resolve an identity provider by type."""
    norm = str(provider_type or "spiffe").strip().lower()
    if norm == "fake":
        return FakeWorkloadIdentityProvider(**kwargs)
    elif norm == "spiffe":
        return SpiffeIdentityProvider(**kwargs)
    else:
        raise ValueError(f"unknown workload identity provider: {provider_type}")
