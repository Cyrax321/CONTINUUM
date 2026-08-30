"""Atomic dual-state rewind (issue #292)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from continuum.environment.file_snapshot import file_digest, restore_file, snapshot_path
from continuum.events import EventType
from continuum.models import StateCheckpoint
from continuum.recovery.engine import RecoveryEngine
from continuum.state.semantic import project
from continuum.storage.base import Storage

__all__ = ["RewindResult", "RewindError", "rewind_to_checkpoint", "resolve_checkpoint"]


class RewindError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RewindResult:
    run_id: str
    target_checkpoint: StateCheckpoint
    reverted_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unrecoverable: tuple[str, ...] = ()
    state_version: int = 0
    resume_mode: str = ""
    resume_safe: bool = False

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.unrecoverable


def resolve_checkpoint(storage: Storage, run_id: str, to: str) -> StateCheckpoint:
    try:
        cp = storage.get_checkpoint(to)
        if cp.run_id == run_id:
            return cp
    except Exception:
        pass
    try:
        version = int(to)
        for cp in storage.list_checkpoints(run_id):
            if cp.version == version:
                return cp
        for cp in storage.list_checkpoints(run_id):
            if cp.state.source_sequence == version:
                return cp
    except ValueError:
        pass
    raise RewindError(f"no checkpoint {to!r} for run {run_id!r}")


def _collect_tool_completed(storage: Storage, run_id: str) -> list[Any]:
    return [e for e in storage.read_events(run_id) if e.type is EventType.TOOL_COMPLETED]


def rewind_to_checkpoint(
    storage: Storage,
    run_id: str,
    to: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    carry_forward: Collection[str] | None = None,
) -> RewindResult:
    target = resolve_checkpoint(storage, run_id, to)
    # Gate wiring (#408): restore must pass the shared precondition gate
    # before it discards (anchor, head]. The rewind discards history, so the
    # same unsettled-authorization and uncertain-slot checks that block fork
    # must block rewind, with per-edit filtering for depended_results.
    # Carry-forward (#493): legitimate audited rewind may explicitly carry
    # unsettled authorizations or uncertain slots, so the flag is threaded
    # through to the gate. Omit it and the gate refuses as before (fail-closed).
    from continuum.recovery.gate import EditPreconditionError, check_preconditions

    try:
        _derivation, _carry_set, _summary = check_preconditions(
            storage,
            run_id,
            target.state.source_sequence,
            edit_type="restore",
            carry_forward=carry_forward,
        )
    except EditPreconditionError:
        raise
    except Exception as exc:
        raise RewindError(str(exc)) from exc
    _ = project(run_id, storage.read_events(run_id), upto=target.state.source_sequence)
    all_tool_events = _collect_tool_completed(storage, run_id)
    checkpoint_seq = target.state.source_sequence
    after = [e for e in all_tool_events if e.sequence > checkpoint_seq]
    before = [e for e in all_tool_events if e.sequence <= checkpoint_seq]
    before_by_path: dict[str, dict[str, Any]] = {}
    for e in before:
        payload = dict(e.payload)
        path = payload.get("path")
        if isinstance(path, str) and path:
            before_by_path[path] = payload
    after_by_path: dict[str, dict[str, Any]] = {}
    for e in after:
        payload = dict(e.payload)
        path = payload.get("path")
        if isinstance(path, str) and path:
            after_by_path[path] = payload
    reverted: list[str] = []
    deleted: list[str] = []
    conflicts: list[str] = []
    unrecoverable: list[str] = []
    for path, after_payload in after_by_path.items():
        after_digest = after_payload.get("sha256")
        before_payload = before_by_path.get(path)
        before_digest = before_payload.get("sha256") if isinstance(before_payload, dict) else None
        current_digest = file_digest(path)
        expected_after = after_digest if isinstance(after_digest, str) else None
        if (
            current_digest is not None
            and expected_after is not None
            and current_digest != expected_after
        ):
            conflicts.append(
                f"{path}: current digest {current_digest[:12] if current_digest else 'missing'} != last observed {expected_after[:12]}"
            )
            continue
        if current_digest is None and expected_after is not None:
            if before_digest is None:
                deleted.append(path)
                continue
            conflicts.append(
                f"{path}: file missing but last observed digest {expected_after[:12]} exists"
            )
            continue
        if before_digest is None:
            try:
                p = Path(path)
                if p.exists():
                    if not dry_run:
                        if file_digest(path) != expected_after:
                            conflicts.append(f"{path}: digest changed before delete")
                            continue
                        p.unlink()
                        if p.exists():
                            conflicts.append(f"{path}: failed to delete")
                            continue
                    deleted.append(path)
                else:
                    deleted.append(path)
            except OSError as exc:
                conflicts.append(f"{path}: delete failed: {exc}")
        else:
            snapshot = snapshot_path(before_digest)
            if not snapshot.exists():
                unrecoverable.append(
                    f"{path}: no snapshot for digest {before_digest[:12]} (file may have been too large or unreadable at checkpoint)"
                )
                continue
            if not dry_run:
                if file_digest(path) != expected_after:
                    conflicts.append(f"{path}: digest changed before restore")
                    continue
                if not restore_file(path, before_digest):
                    unrecoverable.append(f"{path}: restore failed for {before_digest[:12]}")
                    continue
                new_digest = file_digest(path)
                if new_digest != before_digest:
                    conflicts.append(
                        f"{path}: after restore digest {new_digest[:12] if new_digest else 'missing'} != expected {before_digest[:12]}"
                    )
                    continue
            reverted.append(path)
    if not force and not dry_run and (conflicts or unrecoverable):
        raise RewindError(
            f"rewind to {to!r} has {len(conflicts)} conflict(s) and {len(unrecoverable)} unrecoverable file(s); use --force to proceed anyway or resolve conflicts. Conflicts: {conflicts[:3]} Unrecoverable: {unrecoverable[:3]}"
        )
    if not dry_run:
        from continuum.recovery.gate import stamp_lineage

        stamp_lineage(
            storage,
            run_id,
            edit_type="restore",
            anchor=target.state.source_sequence,
            summary=_summary,
            carry_set=_carry_set,
            extra={"target": target.checkpoint_id, "reason": f"rewind to {to}"},
        )
    try:
        engine = RecoveryEngine(storage)
        decision = engine.assess(run_id)
        mode = decision.mode.value
        safe = decision.safe
    except Exception:
        mode = "unknown"
        safe = False
    return RewindResult(
        run_id=run_id,
        target_checkpoint=target,
        reverted_files=tuple(sorted(reverted)),
        deleted_files=tuple(sorted(deleted)),
        conflicts=tuple(sorted(conflicts)),
        unrecoverable=tuple(sorted(unrecoverable)),
        state_version=target.version,
        resume_mode=mode,
        resume_safe=safe,
    )
