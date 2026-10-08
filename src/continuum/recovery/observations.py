"""Post-checkpoint tool observations surfaced in the recovery contract (#208).

The observation hooks (#210) record ``TOOL_COMPLETED`` events with the path,
byte count and SHA-256 of every file a hooked agent writes. Those facts are
durable but, until now, invisible to the recovery contract: a resumed session
saw self-reported progress alone and had to know to inspect the raw log.

This module projects those observations into contract-visible evidence:

- Only observations *after* the latest state version's ``source_sequence`` are
  included, because everything up to that sequence is already baked into the
  checkpointed state.
- Each observation is checked against disk right now: the digest matching is
  reported as ``verified``, a mismatch as ``changed``, an absent file as
  ``missing``. The absence or drift is itself evidence, honestly labelled
  rather than silently dropped.

Drift is a verdict signal, not decoration. The hooks run outside model control
precisely so a model cannot hide a change (#210); an observation records what a
hooked tool wrote, so a file that no longer matches it moved *after* the
recorded write, by something the event log does not describe. That is the exact
threat the observations exist to catch, so :func:`drifted_observations` lifts
those rows out for the planner to turn into a repair step and the engine to let
escalate the verdict (#208). ``recorded`` and ``unresolvable`` rows carry no
digest to compare against and stay informational, consistent with the
provenance rules established in issue #207.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from continuum.events import Event, EventType
from continuum.storage.base import Storage

__all__ = [
    "MAX_CONTRACT_OBSERVATIONS",
    "DRIFT_STATUSES",
    "ObservationDrift",
    "collect_observations",
    "drifted_observations",
    "observation_status",
]

#: Upper bound on entries embedded in one contract, newest last kept first.
#: A run that wrote ten thousand files should not produce a ten-thousand-line
#: contract; the marker row says so explicitly.
MAX_CONTRACT_OBSERVATIONS = 50

#: Statuses that prove the file moved after the hook recorded it. ``verified``
#: matched the recorded digest; ``recorded`` and ``unresolvable`` carry no
#: digest to compare against, so none of the three is evidence of tampering.
DRIFT_STATUSES: frozenset[str] = frozenset({"changed", "missing"})


@dataclass(frozen=True, slots=True)
class ObservationDrift:
    """A post-checkpoint observation that no longer matches disk.

    Carries only what the planner needs to name the repair; the full row stays
    in the contract's ``post_checkpoint_observations`` for the resuming agent.
    """

    path: str
    status: str
    tool: str
    sequence: int


def drifted_observations(observations: Iterable[dict[str, Any]]) -> list[ObservationDrift]:
    """Lift the ``changed``/``missing`` rows out of a collected observation set.

    Order follows :func:`collect_observations` (newest first), matching the
    contract section a reader compares them against; the plan applies its own
    kind-then-target sort afterwards. The trailing truncation marker row
    carries no ``status`` and is skipped.
    """
    return [
        ObservationDrift(
            path=str(entry["path"]),
            status=str(entry["status"]),
            tool=str(entry["tool"]),
            sequence=int(entry["sequence"]),
        )
        for entry in observations
        if entry.get("status") in DRIFT_STATUSES
    ]


def observation_status(path: str, expected_sha: str | None, expected_bytes: int | None) -> str:
    """Compare one observed file against disk, right now."""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError:
        return "missing"
    if expected_bytes is not None and len(data) != expected_bytes:
        return "changed"
    if expected_sha is None:
        return "recorded"
    actual = hashlib.sha256(data).hexdigest()
    return "verified" if actual == expected_sha else "changed"


def _entry_from_event(event: Event, root: Path) -> dict[str, Any] | None:
    payload = dict(event.payload)
    tool = payload.get("tool")
    path = payload.get("path")
    if not isinstance(tool, str) or not isinstance(path, str) or not path:
        return None
    sha = payload.get("sha256") if isinstance(payload.get("sha256"), str) else None
    size = payload.get("bytes") if type(payload.get("bytes")) is int else None
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = root / resolved
    status = (
        "unresolvable"
        if sha is None and size is None
        else observation_status(str(resolved), sha, size)
    )
    return {
        "sequence": event.sequence,
        "tool": tool,
        "path": path,
        "status": status,
    }


def collect_observations(
    storage: Storage,
    run_id: str,
    *,
    after_sequence: int,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Project post-checkpoint ``TOOL_COMPLETED`` events into contract rows.

    Newest first (a resuming agent cares most about recent work), capped at
    :data:`MAX_CONTRACT_OBSERVATIONS`. When the cap bites, a trailing
    ``truncated`` row states how many older rows were omitted.
    """
    events = storage.read_events(run_id, after_sequence=after_sequence)
    base = root or Path.cwd()
    entries: list[dict[str, Any]] = []
    omitted = 0
    for event in reversed(list(events)):
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        entry = _entry_from_event(event, base)
        if entry is None:
            continue
        if len(entries) < MAX_CONTRACT_OBSERVATIONS:
            entries.append(entry)
        else:
            # Every qualifying event past the cap is a genuine omission. Count
            # them rather than appending the marker the moment the cap is
            # reached: at exactly MAX_CONTRACT_OBSERVATIONS nothing has been
            # dropped, so a marker there would be a spurious ``omitted: 0`` row
            # and a cap+1-length result (issue #1363).
            omitted += 1
    if omitted:
        entries.append({"truncated": True, "omitted": omitted})
    return entries
