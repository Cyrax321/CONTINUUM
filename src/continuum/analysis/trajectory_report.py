"""Sleep-time trajectory reports distilled from archived history (issue #393)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from continuum.events import Event, EventType
from continuum.models import Origin, TrajectoryReport, utcnow
from continuum.provenance_map import derived_provenance_for_events
from continuum.storage.base import Storage

__all__ = [
    "TRAJECTORY_REPORT_CAP_BYTES",
    "build_trajectory_report",
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
    from heapq import merge

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


def _scar_rate(events: list[Event]) -> float:
    from continuum.models import Action, ActionStatus

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

    if not latest:
        return 0.0
    scars = sum(
        1 for a in latest.values() if a.status in (ActionStatus.STARTED, ActionStatus.UNKNOWN)
    )
    return round(scars / len(latest), 4) if latest else 0.0


def _stall_sites(events: list[Event]) -> list[str]:
    from continuum.models import Action

    fails: list[str] = []
    for ev in events:
        if ev.type not in (EventType.ACTION_RECORDED, EventType.ACTION_RECONCILED):
            continue
        try:
            action = Action.model_validate(ev.payload["action"])
        except Exception:
            continue
        if action.status.value in ("failed", "unknown", "started"):
            fails.append(action.action_type)

    if not fails:
        return []
    counts = Counter(fails)
    stalled = [t for t, c in counts.items() if c >= 2]
    if not stalled:
        most = counts.most_common(1)
        return [most[0][0]] if most else []
    stalled_sorted = sorted(stalled, key=lambda t: (-counts[t], t))
    return stalled_sorted[:_MAX_STALL_SITES]


def _top_failure_types(events: list[Event]) -> list[str]:
    from continuum.models import Action

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
    from continuum.security.hashing import stable_hash

    events = _window_events(storage, run_id, window_start, window_end)
    attempts = _attempts_in_window(events)
    scar = _scar_rate(events)
    stalls = _stall_sites(events)
    top = _top_failure_types(events)
    stalls = _truncate_list(stalls, _MAX_STALL_SITES)
    top = _truncate_list(top, _MAX_TOP_TYPES)
    derived_origin = derived_provenance_for_events(events)
    raw_id = stable_hash(
        {
            "run_id": run_id,
            "window_start": window_start,
            "window_end": window_end,
            "attempts": attempts,
            "scar_rate": scar,
            "stalls": sorted(stalls),
            "top": sorted(top),
        }
    )
    report_id = raw_id[:16]
    if not report_id:
        report_id = "report_1"
    created = now or utcnow()
    candidate = TrajectoryReport(
        report_id=report_id,
        window_start=window_start,
        window_end=window_end,
        compaction_seq=window_end,
        attempts=attempts,
        scar_rate=scar,
        stall_sites=stalls,
        top_failure_action_types=top,
        created_at=created,
        derived_origin=derived_origin.value,
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
    return candidate


def record_trajectory_report(
    storage: Storage, run_id: str, report: TrajectoryReport
) -> TrajectoryReport:
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


def maybe_generate_trajectory_report(
    storage: Storage,
    run_id: str,
    *,
    window_start: int | None = None,
    window_end: int | None = None,
) -> TrajectoryReport | None:
    if window_start is None or window_end is None:
        try:
            all_events = list(storage.read_events(run_id)) + list(
                storage.read_archived_events(run_id)
            )
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


def render_trajectory_report(report: TrajectoryReport) -> list[str]:
    label = (
        "unverified (derived)"
        if report.derived_origin in ("external_agent", "llm")
        else f"derived from {report.derived_origin}"
    )
    lines: list[str] = []
    lines.append(
        f"trajectory report {report.report_id} window {report.window_start}->{report.window_end} [{label}]:"
    )
    lines.append(f"  attempts {report.attempts}, scar_rate {report.scar_rate:.2f}")
    if report.stall_sites:
        lines.append(f"  stall_sites: {', '.join(report.stall_sites)}")
    if report.top_failure_action_types:
        lines.append(f"  top failures: {', '.join(report.top_failure_action_types)}")
    return lines
