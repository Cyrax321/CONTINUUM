"""Closing a run as completed, shared by every surface (issue #1153).

Three places can close a run as completed from a human: the ``continuum
complete`` CLI verb, the TUI's ``complete_run``, and the dashboard's HITL
button. For a while only the CLI performed the whole verb. It appends
``REVIEW_CONFIRMED`` before ``RUN_COMPLETED`` (both ``Origin.HUMAN``, so they
clear the self-certification gates on goal and progress), flips the run row to
``COMPLETED``, and then clears the instant-resume file.

The TUI skipped the file, and the dashboard skipped both the file and the
confirmation. Two real consequences followed: a run closed from the dashboard
or the TUI left ``.continuum/resume.json`` pointing at a run that was already
finished, so the next session's instant-resume fast path landed the operator
back in the work they had just closed (the exact hijack ``cmd_complete``
exists to prevent); and a dashboard-closed externally-driven run stayed
self-certified, because the event that clears that marker never landed.

Every surface now funnels through :func:`close_run` so the three cannot drift
apart again. The resume delete stays conditional on the file naming this run:
closing one run must never clobber another run's resume pointer, and a resume
file the process cannot read must not block completing a run, so the read is
best-effort exactly as the CLI always had it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from continuum.checkpoint.manager import RESUME_JSON
from continuum.events import EventType
from continuum.models import Origin, Run, RunStatus
from continuum.storage.base import Storage

__all__ = ["close_run", "clear_resume_pointer"]

#: The components a full human confirmation seals; anything less leaves the
#: other component self-certified (issue #394's scoped confirm).
CONFIRMED_COMPONENTS = ("goal", "progress")


def close_run(
    storage: Storage,
    run_id: str,
    *,
    closed_by: str,
    summary: str = "",
) -> Run:
    """Close ``run_id`` as completed from a human, with the log to match.

    Appends ``REVIEW_CONFIRMED`` and then ``RUN_COMPLETED``, both
    ``Origin.HUMAN``, flips the run row to ``COMPLETED``, and clears the
    instant-resume file if it names this run. This is the tail of
    ``continuum complete``, shared with the TUI and the dashboard so all three
    surfaces leave identical state.

    ``closed_by`` records which surface closed the run and rides in the
    ``RUN_COMPLETED`` payload for the audit trail; ``summary`` is embedded when
    non-empty and omitted entirely when absent, so the log never carries a
    ``""`` placeholder that reads as a truncated note. A missing run raises
    from ``get_run`` before anything is written. Returns the updated run row.
    """
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": list(CONFIRMED_COMPONENTS)},
        source=Origin.HUMAN,
    )
    completed: dict[str, Any] = {"closed_by": closed_by}
    if summary:
        completed["summary"] = summary
    storage.append_event(run_id, EventType.RUN_COMPLETED, completed, source=Origin.HUMAN)
    updated = storage.get_run(run_id).touch(status=RunStatus.COMPLETED)
    storage.update_run(updated)
    clear_resume_pointer(run_id)
    return updated


def clear_resume_pointer(run_id: str) -> None:
    """Remove the instant-resume file, but only if it names ``run_id``.

    ``CheckpointManager`` rewrites ``.continuum/resume.json`` on every
    checkpoint, and the SessionStart fast path prefers whatever run it names.
    A completed run is no longer interrupted, so the file is deleted when it
    refers to this run and left alone otherwise: closing one run must never
    clobber another run's resume pointer. The read is best-effort, matching
    the behaviour the CLI has always had: a resume file the process cannot
    read or parse does not block completing a run.
    """
    path = Path(RESUME_JSON)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("run_id") == run_id:
                path.unlink()
    except Exception:
        pass
