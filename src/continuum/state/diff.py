"""Semantic diff between two states.

The diff is *semantic*, not textual: it compares identified components by their
IDs, so reordering a list produces no diff while invalidating a decision does.

Output is deterministic — entries are emitted in a fixed component order and
sorted by ID within each component — because the diff feeds `continuum diff`,
recovery contracts and benchmark scoring, all of which need stable output.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, TypeVar

from continuum.models import (
    Component,
    DiffEntry,
    DiffKind,
    SemanticState,
    StateDiff,
    StateStatus,
)

__all__ = ["diff_states", "render_diff"]

T = TypeVar("T")

_TERMINAL_STATUSES = frozenset({StateStatus.INVALID, StateStatus.STALE, StateStatus.CONFLICTED})


def _index(items: Iterable[T], key: Callable[[T], str]) -> dict[str, T]:
    return {key(item): item for item in items}


def _compare_collection(
    before: Sequence[Any],
    after: Sequence[Any],
    *,
    component: Component,
    key: Callable[[Any], str],
    describe: Callable[[Any], str],
    fields: Sequence[str],
) -> list[DiffEntry]:
    old = _index(before, key)
    new = _index(after, key)
    entries: list[DiffEntry] = []

    for identifier in sorted(new.keys() - old.keys()):
        entries.append(
            DiffEntry(
                kind=DiffKind.ADDED,
                component=component,
                component_id=identifier,
                detail=describe(new[identifier]),
                after=describe(new[identifier]),
            )
        )

    for identifier in sorted(old.keys() - new.keys()):
        entries.append(
            DiffEntry(
                kind=DiffKind.REMOVED,
                component=component,
                component_id=identifier,
                detail=describe(old[identifier]),
                before=describe(old[identifier]),
            )
        )

    for identifier in sorted(old.keys() & new.keys()):
        previous, current = old[identifier], new[identifier]
        for name in fields:
            was, now = getattr(previous, name), getattr(current, name)
            if was == now:
                continue
            invalidating = name == "status" and now in _TERMINAL_STATUSES
            entries.append(
                DiffEntry(
                    kind=DiffKind.INVALIDATED if invalidating else DiffKind.CHANGED,
                    component=component,
                    component_id=identifier,
                    detail=f"{name}: {was} → {now}",
                    before=was
                    if isinstance(was, (str, int, float, bool, type(None)))
                    else str(was),
                    after=now if isinstance(now, (str, int, float, bool, type(None))) else str(now),
                )
            )

    return entries


def diff_states(before: SemanticState, after: SemanticState) -> StateDiff:
    """Compute the semantic difference between two states of the same run."""
    if before.run_id != after.run_id:
        raise ValueError(f"cannot diff across runs: {before.run_id!r} vs {after.run_id!r}")

    entries: list[DiffEntry] = []

    if before.goal != after.goal:
        if before.goal.description != after.goal.description:
            entries.append(
                DiffEntry(
                    kind=DiffKind.CHANGED,
                    component=Component.GOAL,
                    detail=f"description: {before.goal.description} → {after.goal.description}",
                    before=before.goal.description,
                    after=after.goal.description,
                )
            )
        if before.goal.version != after.goal.version:
            entries.append(
                DiffEntry(
                    kind=DiffKind.CHANGED,
                    component=Component.GOAL,
                    detail=f"version: v{before.goal.version} → v{after.goal.version}",
                    before=before.goal.version,
                    after=after.goal.version,
                )
            )
        if before.goal.constraints != after.goal.constraints:
            entries.append(
                DiffEntry(
                    kind=DiffKind.CHANGED,
                    component=Component.GOAL,
                    detail=(
                        f"constraints: {len(before.goal.constraints)} → "
                        f"{len(after.goal.constraints)}"
                    ),
                    before=", ".join(before.goal.constraints),
                    after=", ".join(after.goal.constraints),
                )
            )

    for name in ("total", "completed", "pending", "failed"):
        was, now = getattr(before.progress, name), getattr(after.progress, name)
        if was != now:
            entries.append(
                DiffEntry(
                    kind=DiffKind.CHANGED,
                    component=Component.PROGRESS,
                    component_id=name,
                    detail=f"{name}: {was} → {now}",
                    before=was,
                    after=now,
                )
            )

    entries += _compare_collection(
        before.decisions,
        after.decisions,
        component=Component.DECISION,
        key=lambda d: d.decision_id,
        describe=lambda d: str(d.decision),
        fields=("decision", "reason", "status", "evidence"),
    )
    entries += _compare_collection(
        before.findings,
        after.findings,
        component=Component.FINDING,
        key=lambda f: f.finding_id,
        describe=lambda f: str(f.claim),
        fields=("claim", "confidence", "status", "evidence"),
    )
    entries += _compare_collection(
        before.evidence,
        after.evidence,
        component=Component.EVIDENCE,
        key=lambda e: e.evidence_id,
        describe=lambda e: e.summary or e.evidence_id,
        fields=("summary", "source", "checksum", "status"),
    )
    entries += _compare_collection(
        before.pending_work,
        after.pending_work,
        component=Component.PENDING_WORK,
        key=lambda w: w.task_id,
        describe=lambda w: str(w.description),
        fields=("description", "status", "prerequisite"),
    )
    entries += _compare_collection(
        before.plan,
        after.plan,
        component=Component.PLAN,
        key=lambda p: p.step_id,
        describe=lambda p: str(p.description),
        fields=("description", "status", "depends_on"),
    )
    entries += _compare_collection(
        before.approvals,
        after.approvals,
        component=Component.APPROVAL,
        key=lambda a: a.approval_id,
        describe=lambda a: str(a.subject),
        fields=("subject", "status", "expires_at"),
    )
    entries += _compare_collection(
        before.external_dependencies,
        after.external_dependencies,
        component=Component.EXTERNAL_DEPENDENCY,
        key=lambda d: d.resource,
        describe=lambda d: f"{d.resource} {d.version or ''}".strip(),
        fields=("version", "checksum", "status", "kind"),
    )

    before_model = before.model.model if before.model else None
    after_model = after.model.model if after.model else None
    if before_model != after_model:
        entries.append(
            DiffEntry(
                kind=DiffKind.CHANGED,
                component=Component.MODEL,
                detail=f"model: {before_model} → {after_model}",
                before=before_model,
                after=after_model,
            )
        )

    return StateDiff(
        run_id=after.run_id,
        from_version=before.version,
        to_version=after.version,
        entries=entries,
    )


_SIGILS: Mapping[DiffKind, str] = {
    DiffKind.ADDED: "+",
    DiffKind.REMOVED: "-",
    DiffKind.CHANGED: "~",
    DiffKind.INVALIDATED: "!",
}


def render_diff(diff: StateDiff) -> str:
    """Render a diff as plain text for the CLI."""
    if not diff.entries:
        return f"No semantic change between v{diff.from_version} and v{diff.to_version}."

    count = len(diff.entries)
    noun = "change" if count == 1 else "changes"
    lines = [f"v{diff.from_version} → v{diff.to_version}  ({count} {noun})", ""]
    for entry in diff.entries:
        label = entry.component.value.replace("_", " ")
        identifier = f" {entry.component_id}" if entry.component_id else ""
        # The detail already names the field for entries whose id *is* the
        # field (progress counters), so avoid "completed: completed: 1 → 50".
        detail = entry.detail
        if entry.component_id and detail.startswith(f"{entry.component_id}: "):
            detail = detail[len(entry.component_id) + 2 :]
        lines.append(f"{_SIGILS[entry.kind]} {label}{identifier}: {detail}")
    return "\n".join(lines)
