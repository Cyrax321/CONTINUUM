"""Trajectory reports distilled from archived history (issues #393, #1427)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from heapq import merge

from continuum.events import Event, EventType
from continuum.models import Action, ActionStatus, Origin, TrajectoryReport, utcnow
from continuum.recovery.derived import derived_label, stamp_derived
from continuum.storage.base import Storage

__all__ = [
    "TRAJECTORY_REPORT_CAP_BYTES",
    "analyze_trajectory",
    "build_trajectory_report",
    "health_maybe_generate_trajectory_report",
    "is_quiet_window",
    "maybe_generate_trajectory_report",
    "record_trajectory_report",
    "render_trajectory_report",
]

TRAJECTORY_REPORT_CAP_BYTES = 2048
_MAX_STALL_SITES = 5
_MAX_TOP_TYPES = 3
_MAX_ATTEMPTS = 1000


def _window_events(storage: Storage, run_id: str, start: int, end: int) -> list[Event]:
    stream = merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda e: e.sequence,
    )
    out: list[Event] = []
    for ev in stream:
        if ev.sequence <= start:
            continue
        if ev.sequence > end:
            break
        out.append(ev)
    return out


def is_quiet_window(events: list[Event]) -> bool:
    """Return True if the event window contains no progress or decision events."""
    for ev in events:
        if ev.type is EventType.WORK_COMPLETED:
            try:
                count = int(ev.payload.get("count", 1))
            except Exception:
                count = 1
            if count > 0 and not bool(ev.payload.get("failed", False)):
                return False
        if ev.type is EventType.TASK_UPDATED:
            completed = ev.payload.get("completed")
            if completed is not None:
                try:
                    if int(completed) > 0:
                        return False
                except Exception:
                    return False
        if ev.type is EventType.DECISION_CREATED:
            return False
    return True


def _latest_actions(events: list[Event]) -> dict[str, Action]:
    """Fold the last recorded state of each action key from the window.

    Later events win, so a key claimed and then settled reports its settled
    state rather than every intermediate one.
    """
    latest: dict[str, Action] = {}
    for ev in events:
        if ev.type not in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            continue
        raw_key = ev.payload.get("key")
        if not raw_key:
            continue
        try:
            action = Action.model_validate(ev.payload["action"])
        except Exception:
            continue
        latest[str(raw_key)] = action
    return latest


def _scar_rate(events: list[Event]) -> float:
    """Ratio of actions whose last state hit an error or needed intervention.

    An action the ledger never settled stays STARTED or UNKNOWN: the side effect
    may or may not have happened, so it is a scar on the run's record until a
    reconciliation says otherwise.
    """
    latest = _latest_actions(events)
    if not latest:
        return 0.0
    scars = sum(
        1 for a in latest.values() if a.status in (ActionStatus.STARTED, ActionStatus.UNKNOWN)
    )
    return round(scars / len(latest), 4)


def _uncertain_count(events: list[Event]) -> int:
    """Number of actions whose side effect still needs reconciliation.

    The ledger sets ``side_effect_uncertain`` when it cannot tell whether the
    effect happened, which is exactly the state a reconciliation exists to
    settle. Counting keys rather than events keeps a key re-recorded several
    times during recovery from inflating the figure.
    """
    return sum(1 for a in _latest_actions(events).values() if a.side_effect_uncertain)


def _total_attempts(events: list[Event]) -> int:
    """Total actions claimed across the window.

    A claim is an action put into flight, which the ledger records as an
    ACTION_RECORDED carrying ``status=started``. Settlements reuse the same
    event type with a terminal status, so counting the events themselves would
    charge a settled action twice; only claims-in-flight are attempts. A key
    re-claimed after it failed is a second attempt and counts as one, which is
    how an operator would read the run. The cap keeps a pathological log from
    producing a number the report's byte budget cannot hold.
    """
    claims = 0
    for ev in events:
        if ev.type is not EventType.ACTION_RECORDED:
            continue
        try:
            action = Action.model_validate(ev.payload["action"])
        except Exception:
            continue
        if action.status is ActionStatus.STARTED:
            claims += 1
    return min(claims, _MAX_ATTEMPTS)


def _plan_step_spans(events: list[Event]) -> list[tuple[str, int, int]]:
    """Derive ordered (step_id, first_sequence, last_sequence) spans from PLAN_UPSERT events (issue #1463)."""
    transitions: list[tuple[str, int]] = []
    current_step: str | None = None
    plan_units: dict[str, str] = {}

    for ev in events:
        if ev.type is not EventType.PLAN_UPSERT:
            continue
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        units = payload.get("units")
        if not isinstance(units, list):
            continue

        for u in units:
            if not isinstance(u, dict):
                continue
            uid = str(u.get("id") or u.get("step_id") or "").strip()
            if not uid:
                continue
            if "status" in u and u["status"] is not None:
                plan_units[uid] = str(u["status"]).strip().lower()
            elif uid not in plan_units:
                plan_units[uid] = "pending"

        active: str | None = None
        for uid, status in plan_units.items():
            if status in ("working", "in_progress"):
                active = uid
                break

        if active is None:
            for uid, status in plan_units.items():
                if status not in ("done", "completed", "blocked"):
                    active = uid
                    break

        if active and active != current_step:
            transitions.append((active, ev.sequence))
            current_step = active

    if not transitions:
        return []

    end_seq = max((ev.sequence for ev in events), default=transitions[-1][1])
    spans: list[tuple[str, int, int]] = []
    for idx, (step_id, start_seq) in enumerate(transitions):
        if idx + 1 < len(transitions):
            next_start = transitions[idx + 1][1]
            last_seq = max(start_seq, next_start - 1)
        else:
            last_seq = max(start_seq, end_seq)
        spans.append((step_id, start_seq, last_seq))

    return spans


def _locate_action_type_step(
    action_type: str,
    events: list[Event],
    latest_actions: dict[str, Action],
    spans: list[tuple[str, int, int]],
) -> str | None:
    """Find the plan step whose sequence span contains the stalling action's events."""
    if not spans:
        return None

    stalling_keys = {
        key
        for key, action in latest_actions.items()
        if action.action_type == action_type
        and action.status in (ActionStatus.FAILED, ActionStatus.STARTED, ActionStatus.UNKNOWN)
    }

    seqs: list[int] = []
    for ev in events:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            raw_key = payload.get("key")
            if raw_key and str(raw_key) in stalling_keys:
                seqs.append(ev.sequence)
            elif not raw_key:
                act = payload.get("action")
                if isinstance(act, dict) and act.get("action_type") == action_type:
                    seqs.append(ev.sequence)

    if not seqs:
        for ev in events:
            if ev.type is EventType.ACTION_RECORDED:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                act = payload.get("action")
                if isinstance(act, dict) and act.get("action_type") == action_type:
                    seqs.append(ev.sequence)

    if not seqs:
        return None

    step_counts: Counter[str] = Counter()
    for seq in seqs:
        for step_id, first_seq, last_seq in spans:
            if first_seq <= seq <= last_seq:
                step_counts[step_id] += 1
                break

    if not step_counts:
        return None

    return step_counts.most_common(1)[0][0]


def _stall_sites(events: list[Event]) -> list[str]:
    """Action types where the run repeatedly stalled, by retry or by failure.

    A stall is a site the run kept hitting: the same action type failing again
    and again, or the same resource re-claimed because its first attempt never
    settled. A settled failure counts once, the way an operator would count it;
    a re-claimed key that is still STARTED or UNKNOWN is a retry and counts
    double, so a single retry surfaces even when no type has yet failed twice.

    When the run records a plan via PLAN_UPSERT, each stalling action type is
    joined to the plan step span containing its events and reported as
    action_type@step (issue #1463). When no plan is recorded, it falls back
    to the bare action type.
    """
    event_count: dict[str, int] = {}
    for ev in events:
        if ev.type not in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            continue
        raw_key = ev.payload.get("key")
        if raw_key:
            event_count[str(raw_key)] = event_count.get(str(raw_key), 0) + 1

    latest = _latest_actions(events)
    counts: Counter[str] = Counter()
    for key, action in latest.items():
        if action.status not in (ActionStatus.FAILED, ActionStatus.STARTED, ActionStatus.UNKNOWN):
            continue
        retried_unsettled = event_count.get(key, 0) >= 2 and action.status in (
            ActionStatus.STARTED,
            ActionStatus.UNKNOWN,
        )
        counts[action.action_type] += 2 if retried_unsettled else 1

    if not counts:
        return []
    stalled = [t for t, c in counts.items() if c >= 2]
    if not stalled:
        most = counts.most_common(1)
        stalled = [most[0][0]] if most else []
    stalled_sorted = sorted(stalled, key=lambda t: (-counts[t], t))

    spans = _plan_step_spans(events)
    results: list[str] = []
    for t in stalled_sorted[:_MAX_STALL_SITES]:
        step = _locate_action_type_step(t, events, latest, spans)
        results.append(f"{t}@{step}" if step else t)
    return results


def _top_failure_types(events: list[Event]) -> list[str]:
    fails: list[str] = []
    for ev in events:
        if ev.type is EventType.ACTION_RECORDED:
            try:
                action = Action.model_validate(ev.payload["action"])
            except Exception:
                continue
            if action.status.value in ("failed",):
                fails.append(action.action_type)
        elif ev.type is EventType.TOOL_FAILED:
            tool = ev.payload.get("tool_name") or ev.payload.get("action_type") or "unknown"
            fails.append(str(tool))
    if not fails:
        for ev in events:
            if ev.type is EventType.ACTION_RECORDED:
                try:
                    action = Action.model_validate(ev.payload["action"])
                except Exception:
                    continue
                if action.status.value in ("started", "unknown"):
                    fails.append(action.action_type)
    counts = Counter(fails)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _ in ordered[:_MAX_TOP_TYPES]]


def _attempts_in_window(events: list[Event]) -> int:
    attempt_markers = 0
    keys: set[str] = set()
    for ev in events:
        if ev.type in (EventType.RECOVERY_STARTED, EventType.RUN_FORKED, EventType.ATTEMPT_LESSON):
            attempt_markers += 1
        if ev.type is EventType.ACTION_RECORDED:
            raw_key = ev.payload.get("key")
            if raw_key:
                keys.add(str(raw_key))
    if attempt_markers:
        return min(attempt_markers, _MAX_ATTEMPTS)
    if keys:
        return min(len(keys), _MAX_ATTEMPTS)
    return 1 if events else 0


def _truncate_list(items: list[str], cap: int) -> list[str]:
    return [str(x)[:128] for x in items[:cap]]


def build_trajectory_report(
    storage: Storage,
    run_id: str,
    window_start: int,
    window_end: int,
    *,
    now: datetime | None = None,
) -> TrajectoryReport:
    """Distill metrics and failure patterns across an event window into a report.

    Pure over the folded events: the same window in the same storage always
    yields byte-identical figures, and the report id is the prefix of the
    digest the model recomputes from its own fields, so a stored report can be
    checked against the events it summarises rather than taken on trust.
    """
    events = _window_events(storage, run_id, window_start, window_end)
    attempts = _attempts_in_window(events)
    total = _total_attempts(events)
    uncertain = _uncertain_count(events)
    scar = _scar_rate(events)
    stalls = _truncate_list(_stall_sites(events), _MAX_STALL_SITES)
    top = _truncate_list(_top_failure_types(events), _MAX_TOP_TYPES)
    created = now or utcnow()
    candidate = TrajectoryReport(
        # The id is filled from the digest once the content is final, so it
        # never names a set of fields the report no longer carries.
        report_id="pending",
        window_start=window_start,
        window_end=window_end,
        compaction_seq=window_end,
        attempts=attempts,
        total_attempts=total,
        uncertain_count=uncertain,
        scar_rate=scar,
        stall_sites=stalls,
        top_failure_action_types=top,
        created_at=created,
    )
    # A report is a derived artifact of its window, stamped through the shared
    # non-amplification helper (#392) so its origin can never diverge from the
    # one the informed-retry block would carry for the same events.
    candidate = TrajectoryReport.model_validate(
        stamp_derived(candidate.model_dump(mode="json"), events)
    )
    while (
        len(json.dumps(candidate.model_dump(mode="json"), sort_keys=True).encode())
        > TRAJECTORY_REPORT_CAP_BYTES
    ):
        if candidate.stall_sites:
            candidate = candidate.model_copy(update={"stall_sites": candidate.stall_sites[:-1]})
            continue
        if candidate.top_failure_action_types:
            candidate = candidate.model_copy(
                update={"top_failure_action_types": candidate.top_failure_action_types[:-1]}
            )
            continue
        break
    # The digest covers the truncated lists, so a report that had to shed a
    # stall site to fit the byte budget gets an id that matches what it stores.
    return candidate.model_copy(update={"report_id": candidate.digest()[:16]})


def _history_window(storage: Storage, run_id: str) -> tuple[int, int] | None:
    """Full run window, from the first event to the last across both logs.

    ``storage.last_sequence`` reads the live log only, and a compacted run keeps
    its newest events live, so the merged stream is what actually bounds the
    history. Returns None when the run has no events at all.
    """
    end = 0
    saw_any = False
    for ev in merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda e: e.sequence,
    ):
        saw_any = True
        if ev.sequence > end:
            end = ev.sequence
    if not saw_any or end <= 0:
        return None
    return 0, end


def analyze_trajectory(
    storage: Storage, run_id: str, *, now: datetime | None = None
) -> TrajectoryReport | None:
    """Fold the run's whole history, archive plus active log, into one report.

    Unlike :func:`maybe_generate_trajectory_report` this runs on demand and
    covers the full window regardless of whether it was quiet, which is what an
    operator inspecting a live run needs. Read-only: nothing is appended.
    """
    window = _history_window(storage, run_id)
    if window is None:
        return None
    return build_trajectory_report(storage, run_id, window[0], window[1], now=now)


def record_trajectory_report(
    storage: Storage, run_id: str, report: TrajectoryReport
) -> TrajectoryReport:
    """Persist a trajectory report event to storage if not already recorded."""
    try:
        existing_events = list(storage.read_events(run_id)) + list(
            storage.read_archived_events(run_id)
        )
    except Exception:
        existing_events = list(storage.read_events(run_id))
    for ev in existing_events:
        if (
            ev.type is EventType.TRAJECTORY_REPORT
            and ev.payload.get("window_end") == report.window_end
        ):
            try:
                existing = TrajectoryReport.model_validate(ev.payload)
                return existing
            except Exception:
                continue
    payload = report.model_dump(mode="json")
    payload["derived_origin"] = str(report.derived_origin)
    storage.append_event(run_id, EventType.TRAJECTORY_REPORT, payload, source=Origin.DETERMINISTIC)
    return report


def _last_compaction_window(
    storage: Storage, run_id: str, *, require_anchor: bool = False
) -> tuple[int, int] | None:
    try:
        all_events = list(storage.read_events(run_id)) + list(storage.read_archived_events(run_id))
    except Exception:
        all_events = list(storage.read_events(run_id))
    anchors: list[int] = []
    for ev in all_events:
        if ev.type is EventType.EVENT_LOG_ANCHORED:
            anchor = ev.payload.get("anchor_sequence")
            if anchor is None:
                anchor = ev.payload.get("sequence")
            try:
                anchors.append(int(anchor) if anchor is not None else ev.sequence)
            except Exception:
                anchors.append(ev.sequence)
    if not anchors:
        if require_anchor:
            return None
        try:
            window_end = storage.last_sequence(run_id)
            window_start = 0
        except Exception:
            return None
        if window_end == 0:
            return None
    else:
        anchors_sorted = sorted(set(anchors))
        window_end = anchors_sorted[-1]
        window_start = anchors_sorted[-2] if len(anchors_sorted) >= 2 else 0
    if window_end <= window_start:
        return None
    return int(window_start), int(window_end)


def maybe_generate_trajectory_report(
    storage: Storage,
    run_id: str,
    *,
    window_start: int | None = None,
    window_end: int | None = None,
) -> TrajectoryReport | None:
    """Generate and record a trajectory report if the target window is quiet."""
    if window_start is None or window_end is None:
        window = _last_compaction_window(storage, run_id)
        if window is None:
            return None
        window_start, window_end = window
    assert window_start is not None and window_end is not None
    if window_end <= window_start:
        return None
    try:
        all_events_check = list(storage.read_events(run_id)) + list(
            storage.read_archived_events(run_id)
        )
    except Exception:
        all_events_check = list(storage.read_events(run_id))
    for ev in all_events_check:
        if ev.type is EventType.TRAJECTORY_REPORT and ev.payload.get("window_end") == window_end:
            try:
                return TrajectoryReport.model_validate(ev.payload)
            except Exception:
                return None
    events = _window_events(storage, run_id, int(window_start), int(window_end))
    if not events:
        return None
    if not is_quiet_window(events):
        return None
    report = build_trajectory_report(storage, run_id, int(window_start), int(window_end))
    return record_trajectory_report(storage, run_id, report)


def health_maybe_generate_trajectory_report(
    storage: Storage, run_id: str
) -> TrajectoryReport | None:
    """Generate a trajectory report for the latest anchored compaction window."""
    window = _last_compaction_window(storage, run_id, require_anchor=True)
    if window is None:
        return None
    window_start, window_end = window
    return maybe_generate_trajectory_report(
        storage, run_id, window_start=int(window_start), window_end=int(window_end)
    )


def render_trajectory_report(report: TrajectoryReport) -> list[str]:
    """Format a trajectory report into human-readable lines for display."""
    label = derived_label({"derived_origin": report.derived_origin})
    lines: list[str] = []
    lines.append(
        f"trajectory report {report.report_id} window {report.window_start}->{report.window_end} [{label}]:"
    )
    lines.append(
        f"  attempts {report.attempts}, total claimed {report.total_attempts}, "
        f"uncertain {report.uncertain_count}, scar_rate {report.scar_rate:.2f}"
    )
    if report.stall_sites:
        lines.append(f"  stall_sites: {', '.join(report.stall_sites)}")
    if report.top_failure_action_types:
        lines.append(f"  top failures: {', '.join(report.top_failure_action_types)}")
    return lines
