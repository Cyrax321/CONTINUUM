"""Pure view models for the terminal dashboard (issue #782).

Everything here is plain data built from the same Storage, CheckpointManager
and RecoveryEngine the CLI and the web dashboard use. There is deliberately
no curses import: the driver in ``continuum.tui.app`` renders these rows, and
the tests drive them without a terminal. The mutating verbs mirror the human
CLI commands one for one, landing the same event types with the same
Origin.HUMAN provenance, exactly like the dashboard HITL buttons (#242).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from continuum.actions import ActionLedger
from continuum.budgets import (
    DEFAULT_BUDGETS_PATH,
    attempts_for_type,
    evaluate_budget,
    load_budgets,
)
from continuum.checkpoint import CheckpointManager
from continuum.events import Event, EventType
from continuum.models import ActionStatus, Origin, Run, RunStatus
from continuum.recovery import RecoveryEngine
from continuum.storage.base import Storage


def _all_events(storage: Storage, run_id: str) -> list[Event]:
    """Read full history when an archive-capable store provides it."""
    reader = getattr(storage, "read_all_events", None)
    if callable(reader):
        read_all_events = cast(Callable[[str], Sequence[Event]], reader)
        return list(read_all_events(run_id))
    return list(storage.read_events(run_id))


def _children(storage: Storage, run_id: str) -> list[Run]:
    """Find child runs by the run row's parent_run_id field, falling back to
    the older metadata-based convention so records written before the column
    existed still show their children."""
    children: list[Run] = []
    for run in storage.list_runs(limit=None):
        legacy_parent = dict(run.metadata).get("parent_run_id")
        if run.parent_run_id == run_id or (run.parent_run_id is None and legacy_parent == run_id):
            children.append(run)
    return children


__all__ = [
    "ActionRow",
    "BudgetRow",
    "CheckpointRow",
    "EventRow",
    "RunRow",
    "action_rows",
    "budget_rows",
    "checkpoint_rows",
    "complete_run",
    "confirm_state",
    "event_rows",
    "family_lines",
    "force_checkpoint",
    "overview_lines",
    "reconcile_action",
    "recovery_lines",
    "run_rows",
]


#: The statuses an operator may still settle from the keyboard, matching the
#: set `continuum actions` flags and `pending_actions_with_keys` offers.
UNCERTAIN_STATUSES = frozenset(
    {ActionStatus.UNKNOWN, ActionStatus.STARTED, ActionStatus.REQUIRES_REVIEW}
)


@dataclass(frozen=True)
class RunRow:
    """One row of the runs list: what `runs` shows plus the recovery verdict."""

    run_id: str
    status: str
    events: int
    mode: str
    safe: str
    goal: str


@dataclass(frozen=True)
class CheckpointRow:
    """One row of the checkpoint lineage, matching `history`."""

    checkpoint_id: str
    version: int
    trigger: str
    completed: int


@dataclass(frozen=True)
class ActionRow:
    """One ledger action with the key a reconciliation would need."""

    key: str
    status: str
    action_type: str
    external_id: str
    uncertain: bool


@dataclass(frozen=True)
class EventRow:
    """One event of the log, archived prefix included after compaction."""

    sequence: int
    type: str
    summary: str


@dataclass(frozen=True)
class BudgetRow:
    """Retry-budget usage for one action type, matching `budget`."""

    action_type: str
    attempts: int
    max_attempts: int
    remaining: int


def run_rows(storage: Storage) -> list[RunRow]:
    """Assess every run for the index, mirroring the dashboard run table.

    Each row assesses recovery independently and an assessment that raises is
    shown as ``error: ...`` in that row rather than dropping the run: an
    unreadable run must not hide from the operator watching the index.
    """
    rows: list[RunRow] = []
    for run in storage.list_runs():
        try:
            decision = RecoveryEngine(storage).assess(run.run_id)
            mode: str = decision.mode.value
            safe: str = "yes" if decision.safe else "no"
        except Exception as exc:  # presentation must not drop the run
            mode, safe = f"error: {exc}", "unknown"
        rows.append(
            RunRow(
                run_id=run.run_id,
                status=run.status.value,
                events=storage.last_sequence(run.run_id),
                mode=mode,
                safe=safe,
                goal=run.goal,
            )
        )
    return rows


def overview_lines(storage: Storage, run_id: str) -> list[str]:
    """The `inspect` view: semantic state restored from the current event model."""
    state = CheckpointManager(storage).restore(run_id).state
    lines = [
        f"run:         {state.run_id}",
        f"goal:        {state.goal.description} (v{state.goal.version})",
        f"version:     v{state.version}  (events 1..{state.source_sequence})",
        f"progress:    {state.progress.completed} completed, "
        f"{state.progress.pending} pending, {state.progress.failed} failed",
        f"decisions:   {len(state.decisions)} ({len(state.valid_decisions())} valid)",
        f"findings:    {len(state.findings)}",
        f"evidence:    {len(state.evidence)}",
        f"pending:     {len(state.open_work())} task(s)",
    ]
    if state.plan:
        lines.append(f"plan:        {len(state.plan)} unit(s)")
        lines += [f"  - {p.step_id}: {p.description} [{p.status.value}]" for p in state.plan]
    else:
        lines.append("plan:        (none)")
    if state.external_dependencies:
        lines.append("dependencies:")
        lines += [
            f"  - {d.resource}: {d.version or 'unversioned'} [{d.status}]"
            for d in state.external_dependencies
        ]
    return lines


def recovery_lines(storage: Storage, run_id: str) -> list[str]:
    """The `resume` view without --repair: verdict and family roll-up.

    Read-only. A parent may not RESUME while any non-terminal child is
    unsafe, and the most cautious signal wins — the same rule `cmd_resume`
    applies (#243): a clean parent with an unsafe child is presented as
    request_human, not as permission to continue. Completed children are
    terminal and never block, matching ``roll_up_children``.
    """
    decision = RecoveryEngine(storage).assess(run_id)
    lines = decision.render().split("\n")
    child_statuses: list[tuple[Run, str, bool, int]] = []
    for child in _children(storage, run_id):
        if child.status is RunStatus.COMPLETED:
            continue  # terminal: nothing left to block a resume
        try:
            child_decision = RecoveryEngine(storage).assess(child.run_id)
            child_statuses.append(
                (
                    child,
                    child_decision.mode.value,
                    child_decision.safe,
                    len(child_decision.uncertain_actions),
                )
            )
        except Exception as exc:  # presentation must not drop the child
            child_statuses.append((child, f"error: {exc}", False, 0))

    blocked = any(not safe or mode != "resume" for _, mode, safe, _ in child_statuses)
    if blocked:
        # House rule (#243, as in cmd_resume): a resumable parent with an
        # unsafe child must not read as permission to continue.
        lines = [
            line.replace("Recovery decision: RESUME", "Recovery decision: REQUEST_HUMAN").replace(
                "Next permitted action: continue",
                "Next permitted action: none (settle the children below first)",
            )
            for line in lines
        ]
        lines += [
            "",
            "FAMILY BLOCKED: children of this run are not resumable.",
        ] + [
            f"  !! child run {child.run_id} is {mode} (uncertain={uncertain})"
            for child, mode, safe, uncertain in child_statuses
            if not safe or mode != "resume"
        ]
    return lines


def checkpoint_rows(storage: Storage, run_id: str) -> list[CheckpointRow]:
    """The `history` view: one row per checkpoint, multiple per version kept."""
    storage.get_run(run_id)  # raises RunNotFound rather than reading as "none yet"
    rows: list[CheckpointRow] = []
    for checkpoint in storage.list_checkpoints(run_id):
        state = storage.get_version(run_id, checkpoint.version)
        rows.append(
            CheckpointRow(
                checkpoint_id=checkpoint.checkpoint_id,
                version=checkpoint.version,
                trigger=checkpoint.trigger,
                completed=state.progress.completed,
            )
        )
    return rows


def action_rows(storage: Storage, run_id: str) -> list[ActionRow]:
    """The `actions` view, each row carrying the key a reconciliation needs.

    Folded through the ledger's own archive-aware ``folded()`` view, the same
    one the dashboard HITL buttons use, so a compacted run still shows its
    archived unresolved actions and the key offered here is the key that
    would settle the action.
    """
    storage.get_run(run_id)
    folded = ActionLedger(storage, run_id).folded()
    return [
        ActionRow(
            key=key,
            status=action.status.value,
            action_type=action.action_type,
            external_id=action.external_id or "-",
            uncertain=action.status in UNCERTAIN_STATUSES,
        )
        for key, action in sorted(folded.items())
    ]


def event_rows(storage: Storage, run_id: str) -> list[EventRow]:
    """The `events` view: archived prefix included, so a compacted run reads
    the same as one that was never compacted (#532)."""
    storage.get_run(run_id)
    return [
        EventRow(
            sequence=event.sequence,
            type=event.type.value,
            summary=json.dumps(dict(event.payload), default=str),
        )
        for event in _all_events(storage, run_id)
    ]


def family_lines(storage: Storage, run_id: str) -> list[str]:
    """The `tree` view: the parent verdict and every child's, read-only."""
    storage.get_run(run_id)
    run = storage.get_run(run_id)
    lines = [f"{run_id}  [{run.status.value}]  {run.goal[:60]}"]
    children = _children(storage, run_id)
    if not children:
        lines.append("  (no children)")
    for child in children:
        fork_mark = "[fork] " if str(child.metadata.get("fork", "")) == "true" else ""
        try:
            decision = RecoveryEngine(storage).assess(child.run_id)
            mark = "ok " if decision.safe else "!! "
            lines.append(
                f"  {mark}{fork_mark}{child.run_id}  [{decision.mode.value}, "
                f"uncertain={len(decision.uncertain_actions)}]  {child.goal[:44]}"
            )
        except Exception as exc:
            lines.append(f"  !! {fork_mark}{child.run_id}  [assess error: {exc}]")
    return lines


def budget_rows(storage: Storage, run_id: str) -> list[BudgetRow]:
    """Show retry-budget usage for each action type, as `budget` reports it.

    Limits come from the budget registry (``.continuum/budgets.json``), the
    same one the claim sites enforce through ``evaluate_budget``, so the
    dashboard never shows an allowance the next claim would not have.
    Configured action types appear even with no recorded attempts.
    """
    storage.get_run(run_id)
    try:
        raw = load_budgets(Path(DEFAULT_BUDGETS_PATH))
    except Exception:
        raw = {}  # a broken registry must not take the dashboard down with it
    events = _all_events(storage, run_id)
    types_seen: set[str] = set()
    for event in events:
        if event.type is not EventType.ACTION_RECORDED:
            continue
        action = event.payload.get("action")
        if not isinstance(action, dict):
            continue
        action_type = action.get("action_type")
        if isinstance(action_type, str):
            types_seen.add(action_type)
    configured = raw.get("action_types") or {}
    if isinstance(configured, dict):
        types_seen |= {str(name) for name in configured}
    rows: list[BudgetRow] = []
    for action_type in sorted(types_seen):
        used = attempts_for_type(events, action_type)
        _, _, maximum = evaluate_budget(raw, action_type, 0)
        rows.append(
            BudgetRow(
                action_type=action_type,
                attempts=used,
                max_attempts=maximum,
                remaining=max(0, maximum - used),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# control verbs (mutating; the app layer confirms before calling any of these)
# --------------------------------------------------------------------------- #


def force_checkpoint(storage: Storage, run_id: str) -> str:
    """`checkpoint --trigger manual`: seal the current state now."""
    checkpoint = CheckpointManager(storage).checkpoint(
        run_id, trigger="manual", reason="forced from the tui"
    )
    return (
        f"checkpoint {checkpoint.checkpoint_id} written at v{checkpoint.version} "
        f"({checkpoint.state.progress.completed} completed)"
    )


def confirm_state(storage: Storage, run_id: str) -> str:
    """`confirm`: REVIEW_CONFIRMED (Origin.HUMAN), clearing self-certification."""
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )
    return "goal and progress confirmed (REVIEW_CONFIRMED)"


def reconcile_action(storage: Storage, run_id: str, ledger_key: str, *, occurred: bool) -> str:
    """Settle one uncertain side effect, the same ledger write the dashboard
    HITL button and `continuum reconcile` perform."""
    ActionLedger(storage, run_id).reconcile(
        ledger_key, occurred=occurred, note="settled from the tui"
    )
    return f"reconciled {ledger_key[:16]}... occurred={occurred}"


def complete_run(storage: Storage, run_id: str) -> str:
    """`complete`: close the run from the keyboard, mirroring cmd_complete
    (REVIEW_CONFIRMED plus RUN_COMPLETED, both Origin.HUMAN, and the run row
    flips so finished work stops surfacing as the active run)."""
    run = storage.get_run(run_id)
    if run.status is RunStatus.COMPLETED:
        return f"run {run_id} is already completed"
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )
    storage.append_event(
        run_id,
        EventType.RUN_COMPLETED,
        {"closed_by": "tui"},
        source=Origin.HUMAN,
    )
    storage.update_run(run.touch(status=RunStatus.COMPLETED))
    return f"run {run_id} completed"
