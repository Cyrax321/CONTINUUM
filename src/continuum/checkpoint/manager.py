"""Creating and restoring semantic checkpoints.

A checkpoint bundles the projected state, the event cursor it was projected
from, and (later) the environment it was verified against. Sealing it with an
integrity hash makes it the unit recovery trusts.

Ordering matters here. The manager writes the version, then the checkpoint,
then records ``STATE_CHECKPOINTED``. If the process dies partway:

* died before the version was written — nothing is lost; the state is still
  derivable from the events.
* died after the version but before the checkpoint — a version exists with no
  checkpoint. Harmless: the next checkpoint reuses it.
* died after the checkpoint but before the event — the checkpoint exists and is
  valid; the log simply lacks the annotation. ``restore`` reads checkpoints, not
  the annotation, so recovery is unaffected.

No ordering leaves a checkpoint that claims to cover events it does not, which
is the failure that would actually cause data loss.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from continuum.checkpoint.policy import (
    CheckpointDecision,
    CheckpointPolicy,
    CheckpointTrigger,
    PolicyContext,
    default_policy,
)
from continuum.events import EventType
from continuum.models import (
    EnvironmentSnapshot,
    SemanticState,
    StateCheckpoint,
    utcnow,
)
from continuum.state.semantic import project
from continuum.storage.base import Storage

# Resume banner persistence (issue #394): written on every checkpoint so a
# SessionStart hook can inject a banner without opening the database.
_RESUME_JSON = ".continuum/resume.json"

__all__ = ["CheckpointManager", "RestoredRun", "CheckpointError", "rearm_resume_sentinel"]


class CheckpointError(RuntimeError):
    """A checkpoint could not be created or restored."""


@dataclass(frozen=True, slots=True)
class RestoredRun:
    """What recovery gets back: verified state plus how stale it is.

    ``pending_events`` is the gap between the checkpoint and the end of the
    log — work that happened after the last checkpoint. It is replayed onto the
    checkpoint rather than ignored, so a crash between checkpoints does not
    discard the work in between.
    """

    run_id: str
    state: SemanticState
    checkpoint: StateCheckpoint | None
    pending_events: int
    replayed: bool

    @property
    def from_checkpoint(self) -> bool:
        return self.checkpoint is not None


class CheckpointManager:
    """Decides when to checkpoint, writes them, and restores from them."""

    def __init__(
        self,
        storage: Storage,
        *,
        policy: CheckpointPolicy | None = None,
    ) -> None:
        self.storage = storage
        self.policy = policy or default_policy()
        self._last_checkpoint_at: dict[str, datetime] = {}
        self._last_state: dict[str, SemanticState] = {}
        self._annotation_sequence: dict[str, int] = {}

    # -- deciding --------------------------------------------------------- #

    def evaluate(
        self,
        run_id: str,
        *,
        state: SemanticState | None = None,
        explicit: bool = False,
        context_tokens: int | None = None,
        now: datetime | None = None,
    ) -> CheckpointDecision:
        """Ask the policy whether a checkpoint is warranted right now."""
        current = state if state is not None else self.project_current(run_id)
        previous = self._last_state.get(run_id)
        last_at = self._last_checkpoint_at.get(run_id)

        if last_at is None:
            existing = self.storage.latest_checkpoint(run_id)
            if existing is not None:
                last_at = existing.created_at
                self._last_checkpoint_at[run_id] = last_at

        cursor = previous.source_sequence if previous else 0
        new_events = self.storage.read_events(run_id, after_sequence=cursor)

        return self.policy.should_checkpoint(
            PolicyContext(
                state=current,
                previous_state=previous,
                new_events=new_events,
                last_checkpoint_at=last_at,
                now=now or utcnow(),
                explicit=explicit,
                context_tokens=context_tokens,
            )
        )

    def maybe_checkpoint(
        self,
        run_id: str,
        *,
        state: SemanticState | None = None,
        explicit: bool = False,
        context_tokens: int | None = None,
        environment: EnvironmentSnapshot | None = None,
        now: datetime | None = None,
    ) -> StateCheckpoint | None:
        """Checkpoint if the policy agrees. Returns ``None`` when it declines."""
        current = state if state is not None else self.project_current(run_id)
        decision = self.evaluate(
            run_id,
            state=current,
            explicit=explicit,
            context_tokens=context_tokens,
            now=now,
        )
        if not decision.should:
            return None
        return self.checkpoint(
            run_id,
            state=current,
            trigger=decision.trigger,
            reason=decision.reason,
            environment=environment,
        )

    # -- writing ---------------------------------------------------------- #

    def project_current(self, run_id: str) -> SemanticState:
        """Fold the run's full event history into state."""
        return project(run_id, self.storage.read_events(run_id))

    def checkpoint(
        self,
        run_id: str,
        *,
        state: SemanticState | None = None,
        trigger: str = CheckpointTrigger.MANUAL,
        reason: str = "",
        environment: EnvironmentSnapshot | None = None,
        force_version: bool = False,
    ) -> StateCheckpoint:
        """Create, seal and persist a checkpoint unconditionally."""
        current = state if state is not None else self.project_current(run_id)
        if current.run_id != run_id:
            raise CheckpointError(f"state belongs to run {current.run_id!r}, not {run_id!r}")

        version = self.storage.put_version(current, reason=reason or trigger, force=force_version)

        checkpoint = StateCheckpoint(
            run_id=run_id,
            version=version,
            trigger=trigger,
            reason=reason,
            state=current.model_copy(update={"version": version}),
            environment=environment,
        ).sealed()
        stored = self.storage.put_checkpoint(checkpoint)

        annotation = self.storage.append_event(
            run_id,
            EventType.STATE_CHECKPOINTED,
            {
                "checkpoint_id": stored.checkpoint_id,
                "version": version,
                "trigger": trigger,
                "reason": reason,
                "source_sequence": current.source_sequence,
                "integrity_hash": stored.integrity_hash,
            },
        )

        self._last_checkpoint_at[run_id] = stored.created_at
        self._last_state[run_id] = stored.state
        self._annotation_sequence[run_id] = annotation.sequence
        # Instant resume detection (issue #394): persist a tiny file the
        # SessionStart hook can read without touching SQLite. Best effort;
        # a failure here must not break checkpointing itself.
        with contextlib.suppress(Exception):
            _write_resume_json(run_id, stored)
        return stored

    def _cursor_for(self, checkpoint: StateCheckpoint) -> int:
        """How far into the log a checkpoint really covers.

        The ``STATE_CHECKPOINTED`` annotation is written *after* the state was
        projected, so it always sits one past the projected cursor. Counting it
        as unreplayed work would make every freshly-checkpointed run look stale
        and would replay a no-op event on every restore. The annotation carries
        no state, so the cursor advances past it.
        """
        cursor = checkpoint.state.source_sequence
        for event in self.storage.read_events(
            checkpoint.run_id, after_sequence=cursor, upto=cursor + 1
        ):
            if (
                event.type is EventType.STATE_CHECKPOINTED
                and event.payload.get("checkpoint_id") == checkpoint.checkpoint_id
            ):
                return event.sequence
        return cursor

    # -- restoring -------------------------------------------------------- #

    def restore(
        self,
        run_id: str,
        *,
        replay: bool = True,
        on_unprojectable: Literal["raise", "degrade"] = "raise",
    ) -> RestoredRun:
        """Load the newest checkpoint and catch it up to the log.

        With ``replay=False`` the checkpoint is returned as-is, which is what a
        validator wants when it must judge the checkpoint on its own terms
        before trusting anything newer.

        ``on_unprojectable="degrade"`` lets a caller whose job is diagnosis
        (the recovery engine) get the last-good prefix marked INVALID instead
        of a ProjectionError, so a poisoned log produces a verdict rather than
        ending the assessment. Defaults to ``"raise"``: ``restore`` also feeds
        write paths that must never mistake a partial fold for state.
        """
        checkpoint = self.storage.latest_checkpoint(run_id)

        if checkpoint is None:
            events = self.storage.read_events(run_id)
            if not events:
                raise CheckpointError(f"run {run_id!r} has no checkpoint and no events")
            return RestoredRun(
                run_id=run_id,
                state=project(run_id, events, on_unprojectable=on_unprojectable),
                checkpoint=None,
                pending_events=len(events),
                replayed=True,
            )

        if not checkpoint.verify():  # pragma: no cover - storage refuses these on read
            raise CheckpointError(
                f"checkpoint {checkpoint.checkpoint_id!r} failed its integrity check"
            )

        cursor = self._cursor_for(checkpoint)
        pending = self.storage.read_events(run_id, after_sequence=cursor)

        if not replay or not pending:
            return RestoredRun(
                run_id=run_id,
                state=checkpoint.state,
                checkpoint=checkpoint,
                pending_events=len(pending),
                replayed=False,
            )

        from continuum.state.semantic import project_incremental

        state, _ = project_incremental(
            run_id, pending, base=checkpoint.state, on_unprojectable=on_unprojectable
        )
        return RestoredRun(
            run_id=run_id,
            state=state,
            checkpoint=checkpoint,
            pending_events=len(pending),
            replayed=True,
        )

    def history(self, run_id: str) -> Sequence[StateCheckpoint]:
        return self.storage.list_checkpoints(run_id)

    # -- recovery anchors ------------------------------------------------- #

    def last_recovery_anchor(
        self, run_id: str, *, before_version: int | None = None
    ) -> StateCheckpoint | None:
        """Return the most recent checkpoint taken for a recovery decision.

        A recovery anchor is a checkpoint whose trigger is ``RECOVERY``: it marks
        the exact state a non-resume decision judged unsafe to continue from, so
        it is the right place to roll back to. When ``before_version`` is given,
        only anchors at or before that version are considered, which lets a
        caller ask "where would I have rolled back to right before event X?"
        """
        anchors = [
            c
            for c in self.history(run_id)
            if c.trigger == CheckpointTrigger.RECOVERY
            and (before_version is None or c.version <= before_version)
        ]
        if not anchors:
            return None
        return max(anchors, key=lambda c: c.version)

    def checkpoint_on_recovery(
        self, run_id: str, *, environment: EnvironmentSnapshot | None = None, reason: str = ""
    ) -> StateCheckpoint:
        """Take an unconditional recovery anchor.

        This is the hook the agent loop (or CLI) calls right after a recovery
        decision that is not ``RESUME``: it pins the pre-failure state so the
        run can later roll back to it. It does not touch the read-only
        ``RecoveryEngine.assess`` path; callers decide when a decision is
        recovery-worthy and invoke this explicitly.
        """
        return self.checkpoint(
            run_id,
            trigger=CheckpointTrigger.RECOVERY,
            reason=reason or "recovery anchor",
            environment=environment,
        )

    def prune(self, run_id: str, *, keep: int = 5, keep_anchors: bool = True) -> list[str]:
        """Drop old checkpoints while keeping recent history and recovery anchors.

        The ``keep`` newest checkpoints (by version) are always retained. Of the
        older checkpoints, those whose trigger is ``RECOVERY`` are preserved when
        ``keep_anchors`` is true, since deleting a known rollback point defeats
        the purpose of checkpointing. Returns the ids that were removed.
        """
        if keep < 1:
            keep = 1
        checkpoints = list(self.history(run_id))
        if len(checkpoints) <= keep:
            return []
        ordered = sorted(checkpoints, key=lambda c: c.version)
        keepers = {c.checkpoint_id for c in ordered[-keep:]}
        deleted: list[str] = []
        for c in ordered[:-keep]:
            if c.checkpoint_id in keepers:
                continue
            if keep_anchors and c.trigger == CheckpointTrigger.RECOVERY:
                continue
            self.storage.delete_checkpoint(c.checkpoint_id)
            deleted.append(c.checkpoint_id)
        return deleted


def _write_resume_json(run_id: str, checkpoint: StateCheckpoint) -> None:
    """Persist a tiny file for SessionStart instant detection (issue #394)."""
    path = Path(_RESUME_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "checkpoint_id": checkpoint.checkpoint_id,
        "version": checkpoint.version,
        "goal": checkpoint.state.goal.description,
        "progress": {
            "completed": checkpoint.state.progress.completed,
            "total": checkpoint.state.progress.total,
            "pending": checkpoint.state.progress.pending,
            "failed": checkpoint.state.progress.failed,
        },
        "updated_at": checkpoint.created_at.isoformat()
        if hasattr(checkpoint, "created_at")
        else utcnow().isoformat(),
    }
    # Atomic write via temp file then replace to avoid torn reads.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def rearm_resume_sentinel(storage: Storage) -> str | None:
    """Point the SessionStart sentinel at whatever is still in flight.

    There is one sentinel file per working directory but many runs in a
    database, so a run finishing cannot simply delete it: doing so disarmed the
    fast path for every run that was still live. Observed on a real database
    (2026-09-01): the all-features tour completed its own run, which removed the
    file, and the session's actual work was left with no sentinel at all, so a
    fresh session was told nothing was interrupted.

    Returns the run the sentinel now names, or ``None`` when there is nothing
    live to point at, in which case the file is removed. A live run that has not
    checkpointed yet has nothing to write, so it also clears the file; briefing
    falls back to the active-run query and still reports it.
    """
    path = Path(_RESUME_JSON)
    active = storage.get_active_run()
    if active is not None:
        checkpoint = storage.latest_checkpoint(active.run_id)
        if checkpoint is not None:
            with contextlib.suppress(Exception):
                _write_resume_json(active.run_id, checkpoint)
                return active.run_id
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    return None
