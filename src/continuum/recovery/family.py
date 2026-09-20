"""Multi-agent hierarchies: parents, children, aggregated contracts (#243).

A child run is an ordinary run whose ``parent_run_id`` points at its
supervisor. Children share nothing mutable with siblings - coordination
lives entirely in the ledger and contracts - but a parent may not RESUME
while any non-terminal child holds uncertainty or requires review: the
supervisor's contract aggregates the family's worst state.

Deliberately one level deep in v1 (supervisor/worker), matching how the
field actually deploys multi-agent systems. A2A task ids ride on Run.metadata
("a2a_task_id") so external handoffs keep durable identity without claiming
full protocol support.
"""

from __future__ import annotations

from dataclasses import dataclass

from continuum.models import RecoveryMode, Run, RunStatus
from continuum.recovery import RecoveryEngine
from continuum.storage.base import Storage

__all__ = ["ChildStatus", "children_of", "roll_up_children"]


@dataclass(frozen=True)
class ChildStatus:
    """One child's contribution to its parent's aggregate."""

    run_id: str
    status: str
    mode: str
    safe: bool
    uncertain_actions: int


def children_of(storage: Storage, run_id: str) -> list[Run]:
    """The runs parented by ``run_id``, including children recorded before the
    ``parent_run_id`` column existed.

    Those older rows carry the link in ``Run.metadata`` only, and a legacy
    child is still a child: the recovery verdict, the family view, and the
    CLI tree must all resolve it the same way, or the display would show a
    child the aggregate does not account for.
    """
    from continuum.models import Run as RunModel

    resolved: list[Run] = []
    for run in storage.list_runs(limit=None):
        if not isinstance(run, RunModel):
            continue
        if run.parent_run_id == run_id:
            resolved.append(run)
        elif run.parent_run_id is None and dict(run.metadata).get("parent_run_id") == run_id:
            resolved.append(run)  # the pre-column convention
    return resolved


def roll_up_children(
    storage: Storage,
    run_id: str,
    *,
    strict_unknown: bool = True,
) -> tuple[list[ChildStatus], bool]:
    """Assess every non-terminal child of ``run_id``.

    Returns ``(statuses, family_blocked)`` where ``family_blocked`` is True
    when any non-terminal child is not fully safe to resume.
    """
    engine = RecoveryEngine(storage, strict_unknown=strict_unknown)
    statuses: list[ChildStatus] = []
    blocked = False
    for child in children_of(storage, run_id):
        if child.status is RunStatus.COMPLETED:
            continue
        try:
            decision = engine.assess(child.run_id)
            entry = ChildStatus(
                run_id=child.run_id,
                status=child.status.value,
                mode=decision.mode.value,
                safe=decision.safe,
                uncertain_actions=len(decision.uncertain_actions),
            )
        except Exception as exc:
            entry = ChildStatus(child.run_id, child.status.value, f"error: {exc}", False, 0)
        statuses.append(entry)
        if entry.mode != RecoveryMode.RESUME.value or not entry.safe:
            blocked = True
    return statuses, blocked
