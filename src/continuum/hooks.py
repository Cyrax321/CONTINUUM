"""Hooks that make checkpoints independent of LLM discipline (issue 86).

The model may batch work and skip explicit checkpoint calls. A hook called
after each assistant turn or file write ensures a checkpoint is taken when
the policy says it is warranted, without relying on the model to remember the
tool call. Issue 187 adds an async variant that does not block the agent turn.
Issue 191 wires both into every adapter so durability works without a single
word about CONTINUUM in the task prompt.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import threading
from collections.abc import Callable
from pathlib import Path

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import EnvironmentSnapshot, Origin
from continuum.state.semantic import ProjectionError, project

#: One shared background executor for every asynchronous checkpoint write.
#: A per-hook executor leaked a thread each time a hook was constructed and
#: never shut down; a module-global pool with an atexit shutdown cannot leak,
#: and serializing writes through one worker keeps SQLite contention bounded
#: no matter how many harnesses fire hooks concurrently.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="continuum-checkpoint"
)
atexit.register(_EXECUTOR.shutdown, wait=False)

#: Guards submission so two threads evaluating the policy at once cannot both
#: decide "yes" against the same stale state and queue duplicate writes. The
#: write itself is serialized by the executor; this only narrows the race
#: between evaluate and submit.
_SUBMIT_LOCK = threading.Lock()


def make_auto_checkpoint_hook(
    manager: CheckpointManager,
    run_id: str,
    *,
    environment: EnvironmentSnapshot | None = None,
) -> Callable[[], bool]:
    """Return a callable that checkpoints when the policy says to.

    The hook is meant to be called from an agent framework hook point, for
    example after each assistant turn or after each file write. It delegates
    to ``CheckpointManager.maybe_checkpoint`` and returns True when a checkpoint
    was created, False otherwise. The hook never raises on policy no.
    """

    def hook() -> bool:
        result = manager.maybe_checkpoint(run_id, environment=environment)
        return result is not None

    return hook


def submit_auto_checkpoint(
    manager: CheckpointManager,
    run_id: str,
    *,
    environment: EnvironmentSnapshot | None = None,
) -> bool:
    """Evaluate the policy now, write the checkpoint in the background if due.

    The policy decision is cheap and runs synchronously on the caller's
    thread, so the answer reflects the state as of this turn. Only the SQLite
    write is submitted to the shared background executor. Returns True when a
    checkpoint was submitted, False when the policy declined.
    """
    with _SUBMIT_LOCK:
        decision = manager.evaluate(run_id)
        if not decision.should:
            return False
        _EXECUTOR.submit(
            manager.checkpoint,
            run_id,
            trigger=decision.trigger,
            reason=decision.reason,
            environment=environment,
        )
        return True


def make_async_auto_checkpoint_hook(
    manager: CheckpointManager,
    run_id: str,
    *,
    environment: EnvironmentSnapshot | None = None,
) -> Callable[[], bool]:
    """Return a hook that checkpoints in the background without blocking.

    The hook evaluates the policy synchronously, which is cheap, and only
    submits the actual checkpoint write to the shared background executor when
    the policy says a checkpoint is warranted. It returns True only when a
    write was submitted, matching the sync hook's contract instead of the old
    always-True that ignored the policy. An agent turn is never blocked on
    SQLite I/O. This is the path that makes the five part guide stay at one
    model turn with a single end of task checkpoint, as measured for issue 187.
    """

    def hook() -> bool:
        return submit_auto_checkpoint(manager, run_id, environment=environment)

    return hook


def count_sections(file_path: str | Path) -> int:
    """Count markdown sections by ^## headings, the file as ground truth."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("## "))


def get_tail_section(file_path: str | Path) -> str:
    """Return the last ^## section, about 180 words, for style matching."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    parts = text.split("## ")
    if len(parts) <= 1:
        return ""
    tail = "## " + parts[-1]
    return tail[:2000]


def record_file_progress(
    manager: CheckpointManager,
    run_id: str,
    file_path: str | Path,
    total: int,
) -> int:
    """Mirror file-derived progress into the event log, gated on change.

    Counts ^## headings in file_path and appends TASK_UPDATED plus an
    EVIDENCE_ADDED tail only when the derived count differs from what the log
    already records, so repeated calls over an unchanged file add nothing.
    The file is ground truth, the ledger just mirrors it, so the counter can
    never run ahead as it did in issue 188. Returns the derived count when
    events were appended, -1 when nothing changed (or the log is not yet
    projectable).
    """
    completed = count_sections(file_path)
    try:
        last = project(run_id, manager.storage.read_events(run_id)).progress
        unchanged = last.completed == completed and last.total == total
    except ProjectionError:
        unchanged = False
    if unchanged:
        return -1
    manager.storage.append_event(
        run_id,
        EventType.TASK_UPDATED,
        {"completed": completed, "total": total},
        source=Origin.EXTERNAL_AGENT,
    )
    tail = get_tail_section(file_path)
    if tail:
        manager.storage.append_event(
            run_id,
            EventType.EVIDENCE_ADDED,
            {
                "evidence_id": f"tail_{completed}",
                "summary": tail[:500],
                "source": str(file_path),
            },
            source=Origin.EXTERNAL_AGENT,
        )
    return completed


def make_file_derived_progress_hook(
    manager: CheckpointManager,
    run_id: str,
    file_path: str | Path,
    total: int,
    *,
    environment: EnvironmentSnapshot | None = None,
) -> Callable[[], bool]:
    """Derive completed from the file and record it atomically with a checkpoint.

    After each file write, count ^## headings in file_path, append the
    progress events when the count changed, then maybe checkpoint. Returns
    True when anything durable was written.
    """

    def hook() -> bool:
        recorded = record_file_progress(manager, run_id, file_path, total)
        result = manager.maybe_checkpoint(run_id, environment=environment)
        return recorded >= 0 or result is not None

    return hook


def make_async_file_derived_progress_hook(
    manager: CheckpointManager,
    run_id: str,
    file_path: str | Path,
    total: int,
    *,
    environment: EnvironmentSnapshot | None = None,
) -> Callable[[], bool]:
    """Derive progress synchronously, checkpoint in the background.

    The derivation (one small file read plus an event-log comparison) runs on
    the caller's thread so the appended events are visible immediately, but
    the checkpoint write is policy-gated and submitted to the shared executor,
    so the agent turn never blocks on I/O. Returns True when any event was
    appended or a checkpoint was submitted.
    """

    def hook() -> bool:
        recorded = record_file_progress(manager, run_id, file_path, total)
        if recorded < 0:
            return False
        # Progress changed, so a checkpoint is due; whether the policy agrees
        # with writing it right now is its call, the caller's turn still did
        # not block.
        submit_auto_checkpoint(manager, run_id, environment=environment)
        return True

    return hook
