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

Deliberately informational: these entries never change ``recovery_status``,
``next_allowed_action`` or any repair step. Observations are asserted by the
client harness, so they inform a resuming agent but do not certify anything,
consistent with the provenance rules established in issue #207.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from continuum.events import Event, EventType
from continuum.storage.base import Storage

__all__ = ["MAX_CONTRACT_OBSERVATIONS", "collect_observations", "observation_status"]

#: Upper bound on entries embedded in one contract, newest last kept first.
#: A run that wrote ten thousand files should not produce a ten-thousand-line
#: contract; the marker row says so explicitly.
MAX_CONTRACT_OBSERVATIONS = 50


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
    for event in reversed(list(events)):
        if event.type is not EventType.TOOL_COMPLETED:
            continue
        entry = _entry_from_event(event, base)
        if entry is None:
            continue
        entries.append(entry)
        if len(entries) >= MAX_CONTRACT_OBSERVATIONS:
            omitted = sum(
                1
                for e in events
                if e.type is EventType.TOOL_COMPLETED and e.sequence < entry["sequence"]
            )
            entries.append({"truncated": True, "omitted": omitted})
            break
    return entries


def observations_evidence_lines(observations: list[dict[str, Any]]) -> list[str]:
    """Render observation rows as contract evidence strings."""
    lines: list[str] = []
    for entry in observations:
        if entry.get("truncated"):
            lines.append(
                f"files-changed-since-checkpoint: ... {entry['omitted']} earlier row(s) omitted"
            )
            continue
        lines.append(
            f"files-changed-since-checkpoint: {entry['status']} "
            f"{entry['path']} ({entry['tool']}, seq {entry['sequence']})"
        )
    return lines
