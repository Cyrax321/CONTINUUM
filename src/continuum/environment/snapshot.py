"""Capturing what the world looked like when state was recorded.

A checkpoint is only meaningful relative to an environment. "3,421 documents
analysed" means nothing if the dataset was replaced afterwards. The snapshot is
the fingerprint recovery compares against.

Providers are pluggable because environments differ wildly — files, datasets,
git commits, API sessions, permissions. Each provider answers one question:
*what does this resource look like right now?* CONTINUUM ships providers that
need nothing but the standard library.

Capture failures are recorded, not raised. If a resource cannot be inspected at
recovery time — the API is down, the file is unreadable — that is itself a
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
    "StaticProvider",
    "FileProvider",
    "ValueProvider",
    "CallableProvider",
    "GitProvider",
    "capture",
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


class StaticProvider(EnvironmentProvider):
    """Fixed resources, supplied by the caller. Useful for tests and for
    environments CONTINUUM cannot inspect itself."""

    name = "static"

    def __init__(self, resources: Mapping[str, EnvResource] | None = None, **versions: str) -> None:
        captured = dict(resources or {})
        for key, version in versions.items():
            captured[key] = EnvResource(name=key, version=version)
        self._resources = captured

    def capture(self) -> Mapping[str, EnvResource]:
        return dict(self._resources)


class ValueProvider(EnvironmentProvider):
    """Hashes arbitrary in-memory values into resource fingerprints."""

    name = "value"

    def __init__(self, **values: Any) -> None:
        self._values = values

    def capture(self) -> Mapping[str, EnvResource]:
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


class FileProvider(EnvironmentProvider):
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

    def capture(self) -> Mapping[str, EnvResource]:
        captured: dict[str, EnvResource] = {}
        for path in self.paths:
            key = str(path)
            try:
                stat = path.stat()
                if self.max_bytes is not None and stat.st_size > self.max_bytes:
                    captured[key] = EnvResource(
                        name=key,
                        kind="file",
                        version=f"size:{stat.st_size}",
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
    """

    name = "callable"

    def __init__(self, probes: Mapping[str, Any], *, kind: str = "resource") -> None:
        self._probes = dict(probes)
        self._kind = kind

    def capture(self) -> Mapping[str, EnvResource]:
        captured: dict[str, EnvResource] = {}
        for key, probe in self._probes.items():
            try:
                value = probe()
            except Exception as exc:  # noqa: BLE001 - a failed probe is a finding
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


class GitProvider(EnvironmentProvider):
    """Discovers the current commit of a git repository (git HEAD).

    A *discoverable* provider: rather than the agent asserting a version, this
    reads what the repository actually contains. Capture never raises; a
    directory that is not a git repository (or an unreachable one) reports
    ``UNKNOWN_VERSION`` so recovery treats it as a finding, not as unchanged.
    """

    name = "git"

    def __init__(self, path: str | Path = ".") -> None:
        self.path = Path(path)

    def capture(self) -> Mapping[str, EnvResource]:
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
