"""Advisory policy review: what recovery history says, grouped by action type.

``docs/research/policy_learning.md`` identified the operational signals worth
watching - attempts, human-gate outcomes, compaction survival, drift after
automatic repairs - and rejected the tempting next step: learning thresholds
from them. A history of human gates cannot prove a gate was unnecessary, only
that it happened; tuning policy from that data would lower a safety bar using
evidence biased by the bar itself.

This module is the accepted half of that recommendation: a weekly report a
maintainer reads, not a policy engine. It is a pure projection of the event
log, the same discipline as the informed-retry block (#265):

* **Read-only.** Only ``read_events`` / ``read_archived_events`` /
  ``read_all_events`` / ``list_runs`` are called. Running the report can
  never change a recovery decision, a repair plan, or the log itself.
* **Live and archived.** History is read through ``read_all_events``, which
  merges the compacted archive prefix with the live log (#239), so a
  compacted run reports its whole recorded past, and rows note how much of
  each group survived compaction in the archive.
* **Honest about gaps.** Outcome events that no surface currently writes
  (``RECOVERY_COMPLETED`` / ``RECOVERY_BLOCKED``) are counted as ``unknown``
  rather than inferred. A missing outcome is reported as missing.
* **Deterministic.** No clock, no randomness: identical history yields a
  byte-identical report, so two runs a week apart can be diffed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from continuum.events import EventType

if TYPE_CHECKING:
    from continuum.storage.base import Storage

__all__ = ["build_policy_review", "render_policy_review"]

#: Event types that end an attempt, mapped to the outcome name they report.
#: Whichever appears first after a ``RECOVERY_STARTED`` wins; none appearing
#: means the outcome is unknown, never guessed.
_OUTCOME_EVENTS: dict[EventType, str] = {
    EventType.RECOVERY_COMPLETED: "completed",
    EventType.RECOVERY_BLOCKED: "blocked",
}


def _step_kind(step: Any) -> str | None:
    """The repair kind of one plan-step record, or ``None`` if unreadable."""
    if isinstance(step, dict):
        kind = step.get("kind")
        if isinstance(kind, str) and kind:
            return kind
    return None


def _action_type_of(event: Any) -> str | None:
    """The action type carried by an action-ledger event, or ``None``."""
    action = event.payload.get("action")
    if isinstance(action, dict):
        action_type = action.get("action_type")
        if isinstance(action_type, str) and action_type:
            return action_type
    return None


def _review_run(storage: Storage, run_id: str, report: dict[str, Any]) -> None:
    """Fold one run's live and archived history into the running report.

    ``report`` is mutated in place; the caller seeds the shared skeleton so a
    multi-run report needs no merging pass afterwards.
    """
    archived = storage.read_archived_events(run_id)
    events = storage.read_all_events(run_id)
    archived_seqs = {event.sequence for event in archived}

    report["window"]["events"] += len(events)
    report["window"]["archived_events"] += len(archived)
    timestamps = [event.timestamp for event in events]
    if timestamps:
        first, last = min(timestamps), max(timestamps)
        window = report["window"]
        if window["first_event_at"] is None or first.isoformat() < window["first_event_at"]:
            window["first_event_at"] = first.isoformat()
        if window["last_event_at"] is None or last.isoformat() > window["last_event_at"]:
            window["last_event_at"] = last.isoformat()

    def split(group: dict[str, int], sequence: int) -> None:
        """Credit one contributing event to the archive or the live log."""
        group["archived" if sequence in archived_seqs else "live"] += 1
        group["survived_compaction"] = group["archived"] > 0

    # -- repairs: one attempt per plan step, grouped by RepairKind --------- #
    repairs: dict[str, dict[str, Any]] = report["repair_kinds"]
    targets_seen: dict[tuple[str, str], int] = {}
    for index, event in enumerate(events):
        if event.type is not EventType.RECOVERY_STARTED:
            continue
        split(report["compact_survival"], event.sequence)
        first_outcome = next((e for e in events[index + 1 :] if e.type in _OUTCOME_EVENTS), None)
        outcome = "unknown"
        if first_outcome is not None:
            outcome = _OUTCOME_EVENTS[first_outcome.type]
        plan = event.payload.get("plan")
        steps = plan if isinstance(plan, list) else []
        for step in steps:
            kind = _step_kind(step)
            if kind is None:
                report["unparsed_plan_steps"] += 1
                continue
            row = repairs.setdefault(
                kind,
                {
                    "action_type": kind,
                    "attempts": 0,
                    "human_required": 0,
                    "repeated_repairs": 0,
                    "outcomes": {"completed": 0, "blocked": 0, "unknown": 0},
                    "compact_survival": {"archived": 0, "live": 0, "survived_compaction": False},
                },
            )
            row["attempts"] += 1
            row["outcomes"][outcome] += 1
            split(row["compact_survival"], event.sequence)
            if step.get("requires_human") is True:
                row["human_required"] += 1
            target = step.get("target")
            if isinstance(target, str) and target:
                seen = targets_seen.get((kind, target), 0)
                if seen:
                    # The same kind of repair for the same target again: the
                    # earlier repair did not hold, which is drift after an
                    # automatic repair - the signal the research doc names.
                    row["repeated_repairs"] += 1
                targets_seen[(kind, target)] = seen + 1

    # -- side effects: ledger events, grouped by action_type --------------- #
    actions: dict[str, dict[str, Any]] = report["side_effect_actions"]
    # Latest status per ledger key (fold semantics: last write wins), so a
    # claim later completed or settled by a reconcile is not misread as
    # in-flight. Statuses are the lowercase ``ActionStatus`` values.
    latest_status: dict[str, str] = {}
    for event in events:
        if event.type not in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            continue
        action_type = _action_type_of(event)
        if action_type is None:
            continue
        row = actions.setdefault(
            action_type,
            {
                "action_type": action_type,
                "claims": 0,
                "reconciled_effect_found": 0,
                "reconciled_absent": 0,
                "compensated": 0,
                "unsettled": 0,
                "compact_survival": {"archived": 0, "live": 0, "survived_compaction": False},
            },
        )
        split(row["compact_survival"], event.sequence)
        action = event.payload.get("action")
        status = str(event.payload.get("status") or (action or {}).get("status") or "")
        key = str(event.payload.get("key", ""))
        latest_status[key] = status
        if event.type is EventType.ACTION_RECORDED and status == "started":
            # The claim record: the intent to act, one per action.
            row["claims"] += 1
        elif event.type is EventType.ACTION_COMPENSATED:
            row["compensated"] += 1
        elif event.type is EventType.ACTION_RECONCILED:
            # "completed" means the outside world held the effect: intent
            # and world had drifted, and the reconciliation found it.
            # "failed" means absence was confirmed (issue #29).
            if status == "completed":
                row["reconciled_effect_found"] += 1
            else:
                row["reconciled_absent"] += 1
    # An action whose latest status is still "started" is in flight or was
    # forgotten mid-run: counted per action type, never silently dropped.
    unsettled_keys = {key for key, status in latest_status.items() if status == "started"}
    for event in events:
        if event.type not in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            continue
        action_type = _action_type_of(event)
        key = str(event.payload.get("key", ""))
        if action_type is not None and key in unsettled_keys:
            actions[action_type]["unsettled"] = 1

    # -- human gates ------------------------------------------------------- #
    gates = report["human_gates"]
    for event in events:
        if event.type is EventType.APPROVAL_REQUESTED:
            gates["approval_requested"] += 1
        elif event.type is EventType.APPROVAL_GRANTED:
            gates["approval_granted"] += 1
        elif event.type is EventType.APPROVAL_REVOKED:
            gates["approval_revoked"] += 1
    gates["human_required_repairs"] = sum(row["human_required"] for row in repairs.values())


def build_policy_review(storage: Storage, run_id: str | None = None) -> dict[str, Any]:
    """Aggregate recovery history into an advisory, machine-readable report.

    Reads ``run_id`` alone, or every run when ``run_id`` is ``None``. The
    result is deterministic - no clock, no randomness - and read-only: only
    the storage's read paths are touched. Grouped rows are sorted by action
    type so the JSON and text outputs are stable for diffing across weeks.

    Missing-data semantics, documented where a reader will meet them:

    * ``outcomes.unknown`` counts attempts with no ``RECOVERY_COMPLETED`` /
      ``RECOVERY_BLOCKED`` event after them. No current surface writes those
      events, so unknown is the honest default, not an anomaly.
    * ``compact_survival.archived`` counts rows readable only through the
      archive prefix: proof the signal survived a compaction. Events a
      ``RecoveryLedger.compact`` dropped are not recoverable and surface as
      unknown outcomes, never as inferred ones.
    * ``unsettled`` counts ledger keys whose latest recorded status is still
      ``started``: in flight, or forgotten mid-run. ``claims`` counts claim
      records (status ``started`` on ``ACTION_RECORDED``), so a claim later
      completed or reconciled is one claim, not two.
    * Side-effect statuses are the lowercase ``ActionStatus`` values
      (``started`` / ``completed`` / ``failed`` / ...), read from the event
      payload the action ledger writes.
    """
    runs = [run_id] if run_id is not None else sorted(r.run_id for r in storage.list_runs())
    report: dict[str, Any] = {
        "report": "policy-review",
        "advisory": True,
        "window": {
            "scope": f"run:{run_id}" if run_id is not None else "all-runs",
            "runs": len(runs),
            "history": "live-and-archived",
            "events": 0,
            "archived_events": 0,
            "first_event_at": None,
            "last_event_at": None,
        },
        "repair_kinds": {},
        "side_effect_actions": {},
        "human_gates": {
            "approval_requested": 0,
            "approval_granted": 0,
            "approval_revoked": 0,
            "human_required_repairs": 0,
        },
        "compact_survival": {"archived": 0, "live": 0, "survived_compaction": False},
        "unparsed_plan_steps": 0,
        "notes": [
            "advisory only: this report never feeds plan_repairs or any recovery decision",
            "a high human-required rate means investigate probes or workflows, not lower the safety bar",
            "outcomes marked unknown have no RECOVERY_COMPLETED/RECOVERY_BLOCKED event after the attempt",
        ],
    }
    for one_run in runs:
        _review_run(storage, one_run, report)
    report["repair_kinds"] = [report["repair_kinds"][k] for k in sorted(report["repair_kinds"])]
    report["side_effect_actions"] = [
        report["side_effect_actions"][k] for k in sorted(report["side_effect_actions"])
    ]
    report["compact_survival"]["survived_compaction"] = report["compact_survival"]["archived"] > 0
    return report


def _percent(count: int, total: int) -> str:
    """``count/total`` as a whole-percent string, or ``-`` when total is 0."""
    return f"{round(100 * count / total)}%" if total else "-"


def render_policy_review(report: dict[str, Any]) -> list[str]:
    """Render the report as deterministic text lines for the CLI."""
    window = report["window"]
    lines = [
        "Advisory policy review (read-only; never feeds plan_repairs)",
        (
            f"scope: {window['scope']} over {window['runs']} run(s); "
            f"history: live + archived ({window['archived_events']} archived of "
            f"{window['events']} events)"
        ),
        "",
        "repair attempts by action type",
    ]
    if report["repair_kinds"]:
        for row in report["repair_kinds"]:
            outcomes = row["outcomes"]
            lines.append(
                f"  {row['action_type']:<24} attempts {row['attempts']}  "
                f"human-required {row['human_required']} "
                f"({_percent(row['human_required'], row['attempts'])})  "
                f"repeated {row['repeated_repairs']}  outcomes: "
                f"{outcomes['completed']} completed, {outcomes['blocked']} blocked, "
                f"{outcomes['unknown']} unknown"
            )
    else:
        lines.append("  (no repair attempts recorded)")
    lines += ["", "side effects by action type"]
    if report["side_effect_actions"]:
        for row in report["side_effect_actions"]:
            lines.append(
                f"  {row['action_type']:<24} claims {row['claims']}  "
                f"reconciled: {row['reconciled_effect_found']} effect found / "
                f"{row['reconciled_absent']} absent  compensated {row['compensated']}  "
                f"unsettled {row['unsettled']}"
            )
    else:
        lines.append("  (no side-effect actions recorded)")
    gates = report["human_gates"]
    lines += [
        "",
        "human gates",
        (
            f"  approvals: {gates['approval_requested']} requested, "
            f"{gates['approval_granted']} granted, {gates['approval_revoked']} revoked; "
            f"{gates['human_required_repairs']} repair step(s) required a human"
        ),
        "",
        "notes",
    ]
    lines += [f"  - {note}" for note in report["notes"]]
    if report["unparsed_plan_steps"]:
        lines.append(
            f"  - {report['unparsed_plan_steps']} plan step(s) could not be read "
            f"and are excluded from the counts above"
        )
    return lines
