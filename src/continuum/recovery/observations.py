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
threat the observations exist to catch, so :func:`collect_observations` lifts
those rows into ``Observations.drift`` for the planner to turn into a repair
step and the engine to let escalate the verdict (#208). ``recorded`` and
``unresolvable`` rows carry no digest to compare against and stay informational,
consistent with the provenance rules established in issue #207.

Two rules govern the projection, both review points on the original change:

- Each path is evaluated against its **newest** observation only. A file the
  hooks recorded twice was written twice, so differing from the *first* record
  is what the second record explains; faulting that as drift would flag the
  very rewrite the log describes. Older observations of an already-seen path
  are dropped before the disk read, so a file rewritten a hundred times costs
  one ``open``, not a hundred.
- The contract cap bounds what is **rendered**, never what is compared. Drift
  is collected uncapped: a display limit that also limited the comparison would
  let a run hide arbitrary drift behind a large enough backlog of writes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from continuum.events import Event, EventType
from continuum.storage.base import Storage

__all__ = [
    "MAX_CONTRACT_OBSERVATIONS",
    "DRIFT_STATUSES",
    "ObservationDrift",
    "Observations",
    "collect_observations",
    "observation_status",
]

#: Upper bound on entries embedded in one contract, newest last kept first.
#: A run that wrote ten thousand files should not produce a ten-thousand-line
#: contract; the marker row says so explicitly. Bounds ``rows`` only -- never
#: the drift comparison (see :class:`Observations`).
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


@dataclass(frozen=True, slots=True)
class Observations:
    """One pass over the post-checkpoint events: ``rows`` for the contract,
    ``drift`` for the verdict.

    ``rows`` is the contract section -- newest first, at most
    :data:`MAX_CONTRACT_OBSERVATIONS` entries, with a trailing ``truncated``
    marker when the cap bites. ``drift`` is the changed-or-missing subset of the
    *same* pass, uncapped and deduplicated to the newest observation per path.
    Collecting once is what keeps the two from disagreeing: two separate
    collections would re-read the files and could each see a different disk, so
    a contract could show ``verified`` over a verdict of repair, or vice versa.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    drift: list[ObservationDrift] = field(default_factory=list)


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


def _resolved_path(event: Event, root: Path) -> str | None:
    """Where an event's payload points on disk, or ``None`` if it does not.

    Absolute identity for the deduplication: the same file can be recorded
    relative in one event and absolute in another, and both are the one file.
    """
    path = dict(event.payload).get("path")
    if not isinstance(path, str) or not path:
        return None
    resolved = Path(path)
    return str(resolved if resolved.is_absolute() else root / resolved)


def collect_observations(
    storage: Storage,
    run_id: str,
    *,
    after_sequence: int,
    root: Path | None = None,
) -> Observations:
    """Project post-checkpoint ``TOOL_COMPLETED`` events into contract rows and
    drift, in a single pass.

    Newest first (a resuming agent cares most about recent work). A path
    appears at most once, at its newest observation: an older observation of a
    path already seen describes content a later logged write replaced, so it is
    neither contract evidence nor a drift candidate, and it is skipped before
    the file is ever opened. When the cap bites ``rows``, a trailing
    ``truncated`` row states how many paths were omitted; ``drift`` is never
    capped, because the cap is a display bound, not a correctness one.
    """
    events = storage.read_events(run_id, after_sequence=after_sequence)
    base = root or Path.cwd()
    entries: list[dict[str, Any]] = []
    drift: list[ObservationDrift] = []
    seen: set[str] = set()
    omitted = 0
    for event in reversed(list(events)):
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        resolved = _resolved_path(event, base)
        if resolved is None or resolved in seen:
            continue
        entry = _entry_from_event(event, base)
        if entry is None:
            # Malformed but not yet superseded: leave the path unclaimed so a
            # valid older observation of it can still report.
            continue
        seen.add(resolved)
        if entry["status"] in DRIFT_STATUSES:
            drift.append(
                ObservationDrift(
                    path=str(entry["path"]),
                    status=str(entry["status"]),
                    tool=str(entry["tool"]),
                    sequence=int(entry["sequence"]),
                )
            )
        if len(entries) < MAX_CONTRACT_OBSERVATIONS:
            entries.append(entry)
        else:
            # Every qualifying path past the cap is a genuine omission. Count
            # them rather than appending the marker the moment the cap is
            # reached: at exactly MAX_CONTRACT_OBSERVATIONS nothing has been
            # dropped, so a marker there would be a spurious ``omitted: 0`` row
            # and a cap+1-length result (issue #1363).
            omitted += 1
    if omitted:
        entries.append({"truncated": True, "omitted": omitted})
    return Observations(rows=entries, drift=drift)
