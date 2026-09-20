"""Capturing what the world looked like when state was recorded.

A checkpoint is only meaningful relative to an environment. "3,421 documents
analysed" means nothing if the dataset was replaced afterwards. The snapshot is
the fingerprint recovery compares against.

Providers are pluggable because environments differ wildly: files, datasets,
git commits, API sessions, permissions. Each provider answers one question:
*what does this resource look like right now?* CONTINUUM ships providers that
need nothing but the standard library.

Capture failures are recorded, not raised. If a resource cannot be inspected at
recovery time (the API is down, the file is unreadable), that is itself a
finding: the resource becomes ``UNKNOWN`` rather than silently ``VALID``. An
environment check that fails open would defeat the purpose of checking.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from continuum.models import EnvironmentSnapshot, EnvResource, utcnow
from continuum.security.hashing import stable_hash

__all__ = [
    "EnvironmentProvider",
    "ConfigurableProvider",
    "StaticProvider",
    "FileProvider",
    "ValueProvider",
    "CallableProvider",
    "GitProvider",
    "capture",
    "process_fingerprint",
    "UNKNOWN_VERSION",
]

#: Marks a resource that could not be inspected. Never compares equal to a real
#: version, so an unreadable resource can never masquerade as unchanged.
UNKNOWN_VERSION = "__unknown__"


class EnvironmentProvider(ABC):
    """Answers what a set of resources currently looks like."""

    name: str = "provider"

    @abstractmethod
    def capture(self) -> Mapping[str, EnvResource]:
        """Inspect resources. Must not raise: report ``UNKNOWN_VERSION`` instead."""


class ConfigurableProvider(EnvironmentProvider):
    """A provider a run can configure by name, without code.

    ``from_config`` builds the provider from JSON-native parameters and
    ``scope_of`` names the resource keys it will report, both without executing
    the provider. That separation is what lets
    :func:`~continuum.environment.config.resolve_and_capture` fail closed: it
    knows what a provider owns before it asks the provider anything, so a
    provider that cannot answer still leaves its resources reported rather than
    absent.

    Both raise ``ValueError`` on parameters they will not accept, and the
    resolver treats that as a malformed configuration, reporting the declared
    resources as unknown instead of trusting an unvalidated construction.
    """

    @classmethod
    def from_config(cls, params: Mapping[str, Any]) -> ConfigurableProvider:
        """Construct this provider from JSON-native parameters."""
        raise NotImplementedError

    @classmethod
    def scope_of(cls, params: Mapping[str, Any]) -> frozenset[str]:
        """The resource keys this configuration reports, without capturing."""
        raise NotImplementedError


class StaticProvider(ConfigurableProvider):
    """Fixed resources, supplied by the caller. Useful for tests and for
    environments CONTINUUM cannot inspect itself."""

    name = "static"

    def __init__(self, resources: Mapping[str, EnvResource] | None = None, **versions: str) -> None:
        captured = dict(resources or {})
        for key, version in versions.items():
            captured[key] = EnvResource(name=key, version=version)
        self._resources = captured

    def capture(self) -> Mapping[str, EnvResource]:
        """Return a copy of the fixed resources supplied at initialization."""
        return dict(self._resources)

    @classmethod
    def from_config(cls, params: Mapping[str, Any]) -> StaticProvider:
        """Build a static provider from ``{"resources": {key: version}}``."""
        resources = params.get("resources")
        if not isinstance(resources, Mapping):
            raise ValueError("a static provider needs a 'resources' mapping of key to version")
        built: dict[str, EnvResource] = {}
        for key, version in resources.items():
            if not isinstance(key, str) or not isinstance(version, str) or not version:
                raise ValueError(
                    f"static resource {key!r} needs a non-empty string version, got {version!r}"
                )
            built[key] = EnvResource(name=key, version=version)
        if not built:
            raise ValueError("a static provider needs at least one resource")
        return cls(resources=built)

    @classmethod
    def scope_of(cls, params: Mapping[str, Any]) -> frozenset[str]:
        """The configured static resource keys."""
        resources = params.get("resources")
        if not isinstance(resources, Mapping):
            raise ValueError("a static provider needs a 'resources' mapping of key to version")
        return frozenset(resources)


class ValueProvider(ConfigurableProvider):
    """Hashes arbitrary in-memory values into resource fingerprints."""

    name = "value"

    def __init__(self, **values: Any) -> None:
        self._values = values

    def capture(self) -> Mapping[str, EnvResource]:
        """Compute content fingerprints for configured in-memory values.

        Calculates a deterministic hash for each value using :func:`stable_hash`.
        Values that fail serialization or hashing report :data:`UNKNOWN_VERSION`
        with the error in metadata rather than raising.
        """
        captured: dict[str, EnvResource] = {}
        for key, value in self._values.items():
            try:
                checksum = stable_hash(value)
                version = checksum[:16]
            except (TypeError, ValueError) as exc:
                checksum, version = None, UNKNOWN_VERSION
                captured[key] = EnvResource(
                    name=key,
                    kind="value",
                    version=version,
                    checksum=checksum,
                    metadata={"error": str(exc)},
                )
                continue
            captured[key] = EnvResource(name=key, kind="value", version=version, checksum=checksum)
        return captured

    @classmethod
    def from_config(cls, params: Mapping[str, Any]) -> ValueProvider:
        """Build a value provider whose fingerprints come from the parameters."""
        for key, value in params.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"a value parameter name must be a string, got {key!r}")
            if callable(value) or not _is_json_native(value):
                raise ValueError(
                    f"value parameter {key!r} must be JSON-native, got {type(value).__name__}"
                )
        if not params:
            raise ValueError("a value provider needs at least one parameter")
        return cls(**params)

    @classmethod
    def scope_of(cls, params: Mapping[str, Any]) -> frozenset[str]:
        """One resource per configured value, keyed by parameter name."""
        return frozenset(params)


class FileProvider(ConfigurableProvider):
    """Fingerprints files by content hash.

    Content, not mtime: a file restored from backup has a new mtime but the same
    meaning, and touching a file does not invalidate work.
    """

    name = "file"

    def __init__(
        self,
        paths: Iterable[str | Path],
        *,
        chunk_size: int = 1 << 20,
        max_bytes: int | None = None,
    ) -> None:
        self.paths = [Path(p) for p in paths]
        self.chunk_size = chunk_size
        self.max_bytes = max_bytes

    @classmethod
    def from_config(cls, params: Mapping[str, Any]) -> FileProvider:
        """Build a file provider from ``{"paths": [...], "max_bytes": N}``."""
        return cls(_config_paths(params), **_config_size_kwargs(params))

    @classmethod
    def scope_of(cls, params: Mapping[str, Any]) -> frozenset[str]:
        """One resource per path, normalized exactly as ``capture`` keys it."""
        return frozenset(str(Path(p)) for p in _config_paths(params))

    def capture(self) -> Mapping[str, EnvResource]:
        """Fingerprint tracked files by streaming SHA-256 content checksums.

        Files exceeding ``max_bytes`` or causing an :class:`OSError` report
        :data:`UNKNOWN_VERSION` with diagnostic metadata. Missing files are
        omitted so environment diffing classifies them as removed.
        """
        captured: dict[str, EnvResource] = {}
        for path in self.paths:
            key = str(path)
            try:
                stat = path.stat()
                if self.max_bytes is not None and stat.st_size > self.max_bytes:
                    # UNKNOWN_VERSION, not a synthetic "size:<n>" stamp: a size
                    # is not an identity, but diff_environments compared the old
                    # stamp as one, so a replaced file of the same byte size
                    # verified as unchanged and the environment check failed
                    # open (issue #738). The size stays in metadata.
                    captured[key] = EnvResource(
                        name=key,
                        kind="file",
                        version=UNKNOWN_VERSION,
                        metadata={"skipped": "larger than max_bytes", "size": stat.st_size},
                    )
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(self.chunk_size):
                        digest.update(chunk)
                checksum = digest.hexdigest()
                captured[key] = EnvResource(
                    name=key,
                    kind="file",
                    version=checksum[:16],
                    checksum=checksum,
                    metadata={"size": stat.st_size},
                )
            except FileNotFoundError:
                # The tracked file is gone. Report it as absent rather than as a
                # resource with ``version=None``: diff_environments classifies a
                # key present in the old snapshot but missing from the new one as
                # REMOVED, which is the correct reading of a deleted file.
                continue
            except OSError as exc:
                captured[key] = EnvResource(
                    name=key,
                    kind="file",
                    version=UNKNOWN_VERSION,
                    metadata={"error": str(exc)},
                )
        return captured


class CallableProvider(EnvironmentProvider):
    """Wraps caller-supplied probes, e.g. a dataset version or an API session check.

    A probe that raises yields ``UNKNOWN_VERSION`` rather than propagating: an
    unreachable API is a validation result, not a crash.

    Deliberately not a :class:`ConfigurableProvider`: a callable cannot be
    serialized to the event log, and a configuration that held one would mean
    different things before and after a restart. A run reaches a callable probe
    by registering it by name with a
    :class:`~continuum.environment.config.ProviderRegistry` and configuring that
    name, so the configuration stays inert data and the probe stays live code.
    """

    name = "callable"

    def __init__(self, probes: Mapping[str, Any], *, kind: str = "resource") -> None:
        self._probes = dict(probes)
        self._kind = kind

    def capture(self) -> Mapping[str, EnvResource]:
        """Execute registered probe callables and capture their results.

        Wraps return values into :class:`~continuum.models.EnvResource` records.
        Probes that raise an exception report :data:`UNKNOWN_VERSION` with the
        exception details in metadata rather than propagating the failure.
        """
        captured: dict[str, EnvResource] = {}
        for key, probe in self._probes.items():
            try:
                value = probe()
            except Exception as exc:
                captured[key] = EnvResource(
                    name=key,
                    kind=self._kind,
                    version=UNKNOWN_VERSION,
                    metadata={"error": f"{type(exc).__name__}: {exc}"},
                )
                continue
            if isinstance(value, EnvResource):
                captured[key] = value
            else:
                captured[key] = EnvResource(
                    name=key, kind=self._kind, version=None if value is None else str(value)
                )
        return captured


_JSON_NATIVE_TYPES: tuple[type, ...] = (str, int, float, bool, type(None), list, tuple, dict)


def _is_json_native(value: object) -> bool:
    """Whether a value survives a storage round-trip, without importing the
    event payload validator (which would make this module depend on it)."""
    return isinstance(value, _JSON_NATIVE_TYPES)


def _config_paths(params: Mapping[str, Any]) -> list[str]:
    """The ``paths`` parameter of a file provider, validated as strings."""
    paths = params.get("paths")
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("a file provider needs a non-empty 'paths' list")
    if not all(isinstance(path, str) for path in paths):
        raise ValueError(f"a file provider's paths must be strings, got {paths!r}")
    return list(paths)


def _config_size_kwargs(params: Mapping[str, Any]) -> dict[str, Any]:
    """The optional ``chunk_size`` / ``max_bytes`` parameters, validated."""
    kwargs: dict[str, Any] = {}
    for key in ("chunk_size", "max_bytes"):
        if key not in params:
            continue
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"a file provider's {key} must be an integer, got {value!r}")
        if value <= 0:
            raise ValueError(f"a file provider's {key} must be positive, got {value!r}")
        kwargs[key] = value if key != "max_bytes" else value
    return kwargs


def capture(
    run_id: str,
    providers: Sequence[EnvironmentProvider] | EnvironmentProvider = (),
    *,
    extra: Mapping[str, EnvResource] | None = None,
) -> EnvironmentSnapshot:
    """Capture an environment snapshot from one or more providers.

    Later providers override earlier ones on key collision, so a caller can
    layer a specific probe over a broad one.
    """
    if isinstance(providers, EnvironmentProvider):
        providers = (providers,)

    resources: dict[str, EnvResource] = {}
    for provider in providers:
        resources.update(provider.capture())
    if extra:
        resources.update(extra)

    snapshot = EnvironmentSnapshot(run_id=run_id, captured_at=utcnow(), resources=resources)
    return snapshot.model_copy(
        update={
            "integrity_hash": stable_hash(
                snapshot.model_dump(
                    mode="json", exclude={"integrity_hash", "env_id", "captured_at"}
                )
            )
        }
    )


def process_fingerprint() -> Mapping[str, EnvResource]:
    """A small, portable fingerprint of the executing environment."""
    import platform
    import sys

    return {
        "python": EnvResource(name="python", kind="runtime", version=platform.python_version()),
        "platform": EnvResource(name="platform", kind="runtime", version=sys.platform),
        "cwd": EnvResource(name="cwd", kind="runtime", version=os.getcwd()),
    }


class GitProvider(ConfigurableProvider):
    """Discovers the current commit of a git repository (git HEAD).

    A *discoverable* provider: rather than the agent asserting a version, this
    reads what the repository actually contains. Capture never raises; a
    directory that is not a git repository (or an unreachable one) reports
    ``UNKNOWN_VERSION`` so recovery treats it as a finding, not as unchanged.
    """

    name = "git"

    def __init__(self, path: str | Path = ".") -> None:
        self.path = Path(path)

    @classmethod
    def from_config(cls, params: Mapping[str, Any]) -> GitProvider:
        """Build a git provider from ``{"path": "repo"}``, defaulting to ``.``."""
        path = params.get("path", ".")
        if not isinstance(path, str):
            raise ValueError(f"a git provider 'path' must be a string, got {path!r}")
        return cls(path)

    @classmethod
    def scope_of(cls, params: Mapping[str, Any]) -> frozenset[str]:
        """The single ``git:<path>`` resource this provider reports."""
        path = params.get("path", ".")
        if not isinstance(path, str):
            raise ValueError(f"a git provider 'path' must be a string, got {path!r}")
        return frozenset({f"git:{Path(path)}"})

    def capture(self) -> Mapping[str, EnvResource]:
        """Inspect the current git commit HEAD for the configured repository.

        Executes ``git rev-parse HEAD`` under the repository path. If the command
        fails, times out, or reports a non-zero exit status, returns
        :data:`UNKNOWN_VERSION` with error details in metadata.
        """
        key = f"git:{self.path}"
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                key: EnvResource(
                    name=key, kind="git", version=UNKNOWN_VERSION, metadata={"error": str(exc)}
                )
            }
        if result.returncode != 0:
            reason = (result.stderr or "not a git repository").strip()
            return {
                key: EnvResource(
                    name=key, kind="git", version=UNKNOWN_VERSION, metadata={"error": reason}
                )
            }
        commit = result.stdout.strip()
        return {
            key: EnvResource(name=key, kind="git", version=commit[:16], metadata={"commit": commit})
        }
