"""Comparing environment snapshots.

The comparison is deliberately conservative. Three outcomes are distinguished
because they demand different responses:

``UNCHANGED``  the resource is verifiably identical
``CHANGED``    the resource is verifiably different
``UNKNOWN``    we could not tell

``UNKNOWN`` is not a softer ``UNCHANGED``. A resource that could not be
inspected (an API that timed out, a file that is now unreadable) must not be
treated as intact just because nothing contradicted it. Recovery downgrades on
uncertainty rather than assuming the best.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from continuum.environment.snapshot import UNKNOWN_VERSION
from continuum.models import EnvironmentSnapshot, EnvResource

__all__ = ["ResourceChange", "ResourceDelta", "EnvironmentDiff", "diff_environments"]


class ResourceChange(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"
    UNKNOWN = "unknown"


class ResourceDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource: str
    change: ResourceChange
    before: str | None = None
    after: str | None = None
    detail: str = ""

    @property
    def breaking(self) -> bool:
        """Whether this delta can invalidate work that depended on the resource."""
        return self.change in (
            ResourceChange.CHANGED,
            ResourceChange.REMOVED,
            ResourceChange.UNKNOWN,
        )

    def render(self) -> str:
        if self.change is ResourceChange.CHANGED:
            return f"{self.resource}: {self.before} -> {self.after}"
        if self.change is ResourceChange.REMOVED:
            return f"{self.resource}: removed (was {self.before})"
        if self.change is ResourceChange.ADDED:
            return f"{self.resource}: added ({self.after})"
        if self.change is ResourceChange.UNKNOWN:
            return f"{self.resource}: could not be verified ({self.detail})"
        return f"{self.resource}: unchanged"


class EnvironmentDiff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deltas: list[ResourceDelta] = Field(default_factory=list)

    @property
    def changed(self) -> tuple[ResourceDelta, ...]:
        return tuple(d for d in self.deltas if d.change is ResourceChange.CHANGED)

    @property
    def unknown(self) -> tuple[ResourceDelta, ...]:
        return tuple(d for d in self.deltas if d.change is ResourceChange.UNKNOWN)

    @property
    def breaking(self) -> tuple[ResourceDelta, ...]:
        """Deltas that may invalidate dependent state."""
        return tuple(d for d in self.deltas if d.breaking)

    @property
    def stable(self) -> bool:
        """True only when every resource was verified unchanged or newly added."""
        return not self.breaking

    def for_resource(self, name: str) -> ResourceDelta | None:
        return next((d for d in self.deltas if d.resource == name), None)

    def render(self) -> str:
        if not self.deltas:
            return "No environment data to compare."
        interesting = [d for d in self.deltas if d.change is not ResourceChange.UNCHANGED]
        if not interesting:
            return f"Environment unchanged ({len(self.deltas)} resources verified)."
        return "\n".join(f"  {d.render()}" for d in interesting)


def _version_of(resource: EnvResource) -> str | None:
    """Prefer the checksum: it is the strongest available identity."""
    if resource.checksum:
        return resource.checksum
    return resource.version


def _unverifiable(resource: EnvResource) -> bool:
    return resource.version == UNKNOWN_VERSION


def diff_environments(
    before: EnvironmentSnapshot | None,
    after: EnvironmentSnapshot | None,
) -> EnvironmentDiff:
    """Compare two snapshots resource by resource.

    A missing snapshot on either side yields an empty diff rather than a false
    "unchanged": absence of evidence is not evidence of stability, and callers
    check ``deltas`` before drawing conclusions.
    """
    if before is None or after is None:
        return EnvironmentDiff()

    old: Mapping[str, EnvResource] = before.resources
    new: Mapping[str, EnvResource] = after.resources
    deltas: list[ResourceDelta] = []

    for name in sorted(old.keys() | new.keys()):
        previous = old.get(name)
        current = new.get(name)

        if previous is None and current is not None:
            deltas.append(
                ResourceDelta(
                    resource=name,
                    change=ResourceChange.ADDED,
                    after=_version_of(current),
                )
            )
            continue

        if current is None and previous is not None:
            deltas.append(
                ResourceDelta(
                    resource=name,
                    change=ResourceChange.REMOVED,
                    before=_version_of(previous),
                    detail="resource is no longer present in the environment",
                )
            )
            continue

        assert previous is not None and current is not None
        was, now = _version_of(previous), _version_of(current)

        if _unverifiable(current) or _unverifiable(previous):
            reason = str(
                current.metadata.get("error")
                or previous.metadata.get("error")
                or current.metadata.get("skipped")
                or previous.metadata.get("skipped")
                or "resource could not be inspected"
            )
            deltas.append(
                ResourceDelta(
                    resource=name,
                    change=ResourceChange.UNKNOWN,
                    before=was,
                    after=now,
                    detail=reason,
                )
            )
            continue

        if was is None and now is None:
            deltas.append(
                ResourceDelta(
                    resource=name,
                    change=ResourceChange.UNKNOWN,
                    detail="no version or checksum recorded on either side",
                )
            )
            continue

        if was == now:
            deltas.append(
                ResourceDelta(resource=name, change=ResourceChange.UNCHANGED, before=was, after=now)
            )
        else:
            deltas.append(
                ResourceDelta(resource=name, change=ResourceChange.CHANGED, before=was, after=now)
            )

    return EnvironmentDiff(deltas=deltas)
