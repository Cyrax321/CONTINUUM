"""Operator-authored constraint pin registry (issue #1412).

Governance constraints decay across compaction windows (arXiv:2606.22528):
prompt-level reminders are attenuated exactly when they matter most. This
module keeps them out of the context entirely, in a file the operator owns
and the agent cannot write.

The registry lives at ``.continuum/constraints.json`` and is read at the
boundary where a run is validated, never trusted from the agent. Its design
rules follow the rest of the security surface:

- Fail closed. A corrupt or unreadable registry raises, it does not fall back
  to "no constraints", because the absence of a constraint is exactly what a
  broken file would silently grant.
- Operator-only provenance. The loader rejects any write path whose origin is
  self-certified (``EXTRA_AGENT``, ``LLM``, ``IMPORTED``); an agent may not
  pin or retract the constraints that govern it.
- Canonical, content-addressed. The active set has one deterministic digest,
  so two independent readers of the same file agree on whether the governing
  set changed between two points in a run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from continuum.models import Frozen, Origin
from continuum.security.hashing import stable_hash

__all__ = [
    "DEFAULT_CONSTRAINTS_PATH",
    "ConstraintLevel",
    "ConstraintSpec",
    "ConstraintRegistry",
    "load_constraints",
    "constraints_digest",
    "ConstraintRegistryError",
]

#: Where the registry lives relative to the project root. JSON, matching
#: gate.json, reconcilers.json and risk-policy.json.
DEFAULT_CONSTRAINTS_PATH = Path(".continuum/constraints.json")

#: Ids share the label charset of CONSTRAINT_PINNED (models.py): deliberately
#: narrow so a constraint id can never carry a path, a newline, or anything
#: that could survive a round trip through a log line or a filename.
_CONSTRAINT_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")

#: Scope entries name action types, tool prefixes or environment keys. The
#: same charset as ids, plus ``*`` so a constraint can be run-global.
_SCOPE_ENTRY_PATTERN = re.compile(r"[A-Za-z0-9._:*-]{1,128}")

#: Predicates are operator text, not code. They are bounded so a file cannot
#: wedge the validator on a multi-megabyte blob, and they are never evaluated
#: as an expression anywhere in this module.
_MAX_PREDICATE_CHARS = 2048
_MAX_CONSTRAINTS = 256
_MAX_SCOPE_ENTRIES = 64


class ConstraintRegistryError(ValueError):
    """Raised when the constraint registry is malformed or unreadable.

    A distinct type so callers can distinguish "the operator's constraint file
    is wrong" from any other ValueError, and fail closed with a message that
    names the file rather than the offending JSON fragment.
    """


class ConstraintLevel(StrEnum):
    """Severity of a constraint.

    ``hard`` must halt or escalate if the run drops it; ``soft`` is advisory.
    """

    HARD = "hard"
    SOFT = "soft"


class ConstraintSpec(BaseModel):
    """One operator-authored constraint.

    The predicate is stored as text and digested, never parsed as code. An
    invariant validator in a downstream component matches it against run state
    by the ids and scope it names, not by executing it.
    """

    model_config = Frozen

    id: str
    level: ConstraintLevel = ConstraintLevel.HARD
    predicate: str
    scope: tuple[str, ...] = ()

    def matches_scope(self, name: str) -> bool:
        """Whether ``name`` (an action type, tool prefix or env key) is in scope.

        ``*`` matches everything. A trailing-dot entry such as ``db.`` is a
        namespace prefix: it covers ``db.write`` and ``db.read``, and also the
        bare ``db`` that names the namespace itself, so a constraint scoped to
        ``db.`` is not silently bypassed by a call that uses no subtool.
        """
        if not self.scope:
            return True
        for entry in self.scope:
            if entry == "*":
                return True
            if name == entry or name.startswith(entry):
                return True
            if entry.endswith(".") and name == entry[:-1]:
                return True
        return False


class ConstraintRegistry:
    """Validated set of operator constraints, loaded from disk.

    Instances are immutable in practice: construction validates everything,
    so a caller holding one has a set that cannot change underneath it. The
    digest is computed once, over the canonical form, and is stable across
    processes.
    """

    __slots__ = ("_constraints", "_digest", "_source")

    def __init__(
        self,
        constraints: list[ConstraintSpec],
        *,
        digest: str | None = None,
        source: str = "memory",
    ) -> None:
        self._constraints = list(constraints)
        self._source = source
        self._digest = digest if digest is not None else constraints_digest(self._constraints)

    def __len__(self) -> int:
        return len(self._constraints)

    def __iter__(self) -> Iterator[ConstraintSpec]:
        return iter(self._constraints)

    @property
    def source(self) -> str:
        """Where this registry was loaded from, for error messages."""
        return self._source

    @property
    def digest(self) -> str:
        """Deterministic SHA-256 of the canonical constraint set.

        Two readers of the same file compute the same digest, so a validator
        can detect that the governing set changed between two points in a run
        by comparing digests rather than re-serialising prose.
        """
        return self._digest

    def ids(self) -> list[str]:
        return [c.id for c in self._constraints]

    def hard(self) -> list[ConstraintSpec]:
        """Only the constraints whose loss must halt or escalate."""
        return [c for c in self._constraints if c.level is ConstraintLevel.HARD]

    def soft(self) -> list[ConstraintSpec]:
        return [c for c in self._constraints if c.level is ConstraintLevel.SOFT]

    def get(self, constraint_id: str) -> ConstraintSpec | None:
        for c in self._constraints:
            if c.id == constraint_id:
                return c
        return None

    def in_scope(self, name: str, *, level: ConstraintLevel | None = None) -> list[ConstraintSpec]:
        """Constraints governing ``name``, optionally filtered to one level."""
        return [
            c
            for c in self._constraints
            if (level is None or c.level is level) and c.matches_scope(name)
        ]


def _canonical(constraints: list[ConstraintSpec]) -> list[dict[str, Any]]:
    """Serialise to a stable order: sorted by id, keys fixed and sorted.

    Order matters because the digest is over the serialised form. Sorting by
    id makes the digest independent of the file's own ordering, so an operator
    reordering entries does not look like a policy change.
    """
    return [
        {
            "id": c.id,
            "level": c.level.value,
            "predicate": c.predicate,
            "scope": sorted(set(c.scope)),
        }
        for c in sorted(constraints, key=lambda c: c.id)
    ]


def constraints_digest(constraints: list[ConstraintSpec]) -> str:
    """Deterministic digest of a constraint set.

    Computed over the canonical form, not the raw file bytes, so whitespace and
    key order in the operator's JSON do not perturb it.
    """
    return stable_hash(_canonical(list(constraints)))


def _spec_from_mapping(idx: int, raw: Any) -> ConstraintSpec:
    """Validate one entry, failing closed with its position in the file."""
    if not isinstance(raw, dict):
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} must be a JSON object, got {type(raw).__name__}"
        )
    cid = raw.get("id")
    if not isinstance(cid, str) or not _CONSTRAINT_ID_PATTERN.fullmatch(cid):
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} id must be 1-128 chars from ASCII letters, digits and . _ : -"
        )
    level = raw.get("level", "hard")
    if level not in ("hard", "soft"):
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} ({cid}) level must be 'hard' or 'soft', got {level!r}"
        )
    predicate = raw.get("predicate")
    if not isinstance(predicate, str) or not predicate.strip():
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} ({cid}) predicate must be a non-empty string"
        )
    if len(predicate) > _MAX_PREDICATE_CHARS:
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} ({cid}) predicate exceeds {_MAX_PREDICATE_CHARS} characters"
        )
    scope_raw = raw.get("scope", [])
    if scope_raw is None:
        scope_raw = []
    if isinstance(scope_raw, str):
        scope_raw = [scope_raw]
    if not isinstance(scope_raw, list):
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} ({cid}) scope must be a string or a list of strings"
        )
    if len(scope_raw) > _MAX_SCOPE_ENTRIES:
        raise ConstraintRegistryError(
            f"constraint #{idx + 1} ({cid}) scope lists at most {_MAX_SCOPE_ENTRIES} entries"
        )
    scope: list[str] = []
    for entry in scope_raw:
        if not isinstance(entry, str) or not _SCOPE_ENTRY_PATTERN.fullmatch(entry):
            raise ConstraintRegistryError(
                f"constraint #{idx + 1} ({cid}) scope entry {entry!r} is not a valid "
                f"action type, tool prefix or environment key"
            )
        if entry not in scope:
            scope.append(entry)
    return ConstraintSpec(
        id=cid,
        level=ConstraintLevel(level),
        predicate=predicate.strip(),
        scope=tuple(scope),
    )


def load_constraints(
    path: Path | None = None,
    *,
    asserted_by: Origin = Origin.HUMAN,
) -> ConstraintRegistry:
    """Load and validate the constraint registry, failing closed.

    Args:
        path: Registry location. Defaults to ``.continuum/constraints.json``.
        asserted_by: The origin of whoever is loading this registry. The
            operator paths are ``HUMAN`` (a person) and ``DETERMINISTIC`` (the
            CLI or CONTINUUM's own orchestration). Anything self-certified
            (``EXTERNAL_AGENT``, ``LLM``, ``IMPORTED``) is refused: an agent
            cannot install the constraints that govern it, and the refusal is
            here rather than at every call site so no future caller can bypass
            it.

    Raises:
        ConstraintRegistryError: The file is missing, unreadable, or its
            contents are not a valid constraint registry. A missing file is an
            error, not an empty registry, because a deployment that meant to
            ship constraints and did not should be loud about it.
    """
    if asserted_by.self_certified:
        raise ConstraintRegistryError(
            f"constraints may only be loaded by an operator, not by "
            f"{asserted_by.value} (an agent cannot pin the constraints that "
            f"govern it)"
        )
    target = Path(path) if path is not None else DEFAULT_CONSTRAINTS_PATH
    if not target.exists():
        raise ConstraintRegistryError(f"constraint registry not found: {target}")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConstraintRegistryError(f"constraint registry {target} is unreadable: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConstraintRegistryError(
            f"constraint registry {target} is not valid JSON: {exc}"
        ) from exc
    return _from_mapping(data, source=str(target))


def _from_mapping(data: Any, *, source: str) -> ConstraintRegistry:
    if not isinstance(data, dict):
        raise ConstraintRegistryError(
            f"constraint registry {source} must be a JSON object with a 'constraints' list"
        )
    raw_list = data.get("constraints")
    if not isinstance(raw_list, list):
        raise ConstraintRegistryError(
            f"constraint registry {source} must have a 'constraints' list"
        )
    if not raw_list:
        raise ConstraintRegistryError(
            f"constraint registry {source} lists no constraints; an empty "
            f"registry is almost certainly a misconfiguration, so it is "
            f"refused rather than treated as 'no constraints'"
        )
    if len(raw_list) > _MAX_CONSTRAINTS:
        raise ConstraintRegistryError(
            f"constraint registry {source} lists {len(raw_list)} constraints, "
            f"above the {_MAX_CONSTRAINTS} cap"
        )
    specs = [_spec_from_mapping(i, raw) for i, raw in enumerate(raw_list)]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ConstraintRegistryError(
                f"constraint registry {source} declares {spec.id!r} more than once"
            )
        seen.add(spec.id)
    return ConstraintRegistry(specs, source=source)


def load_constraints_or_none(
    path: Path | None = None,
    *,
    asserted_by: Origin = Origin.HUMAN,
) -> ConstraintRegistry | None:
    """Load the registry, returning ``None`` when the file is absent.

    For callers that treat "no registry shipped" as a legitimate state (a
    development checkout) rather than a misconfiguration. Every other failure
    mode still raises, so this is not a silent fallback: only the missing-file
    case is relaxed.
    """
    target = Path(path) if path is not None else DEFAULT_CONSTRAINTS_PATH
    if not target.exists():
        return None
    return load_constraints(target, asserted_by=asserted_by)
