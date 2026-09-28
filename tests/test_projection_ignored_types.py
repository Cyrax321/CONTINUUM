"""The projection fold must understand every event type it can consume (issue #1169).

project_incremental folds events through a match with one case per state-bearing
type and treats the rest as recorded facts via _NON_PROJECTING. Eleven EventType
members were in neither list, so they fell through to `case _` and were counted in
report.ignored_types -- the field ProjectionReport.complete defines as "the fold
understood every event type it consumed". Any run that restored, merged, confirmed
or notified reported as partially unprojectable: applied understated, complete was
False, and ignored_types filled with the names of events the codebase legitimately
emits. The projected state itself was always right; only the bookkeeping lied.
"""

from __future__ import annotations

from continuum.events import EventLog, EventType
from continuum.state.semantic import project_incremental

# The eleven types the fold used to fall through on. Each is a recorded fact
# rather than state: authority audit, notification receipts, the perception and
# branch ledger, the briefing summary surface, and the restore and merge markers.
FACT_TYPES = (
    EventType.AUTHORITY_CONSUMED,
    EventType.AUTHORITY_RECONCILED,
    EventType.BRANCH_RESOLVED,
    EventType.MEMORY_TOMBSTONED,
    EventType.NOTIFICATION_FAILED,
    EventType.NOTIFICATION_SENT,
    EventType.PERCEPTION_OBSERVED,
    EventType.REASONING_SUMMARY,
    EventType.REVIEW_CONFIRMED,
    EventType.RUN_MERGED,
    EventType.RUN_RESTORED,
)


def started(log: EventLog, **payload: object) -> EventLog:
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g", **payload})
    return log


def test_every_fact_type_is_understood_not_ignored() -> None:
    """A run emitting the recorded-fact types reports complete, not partially unprojectable."""
    log = started(EventLog())
    log.append("run_1", EventType.WORK_COMPLETED, {"item": 1})
    for fact in FACT_TYPES:
        log.append("run_1", fact, {"any": "payload"})

    _state, report = project_incremental("run_1", log.events("run_1"))

    assert report.consumed == 1 + 1 + len(FACT_TYPES)
    # None of the eleven is a legitimate occupant of ignored_types, which
    # test_projection_edges reserves for a deliberately bogus type.
    assert report.ignored_types == {}
    assert report.complete


def test_no_event_type_the_codebase_emits_is_ignored() -> None:
    """Every enum member is either folded or declared a recorded fact.

    This is the invariant that failed for eleven members: adding a type to the
    enum without accounting for it in either the match or _NON_PROJECTING made
    the fold report a run it fully understood as incomplete. Driving the fold
    with one event of each type pins the contract where it lives, on the enum.
    """
    ignored: list[str] = []
    for event_type in EventType:
        log = started(EventLog())
        log.append("run_1", event_type, {})
        try:
            _state, report = project_incremental("run_1", log.events("run_1"))
        except Exception:
            # The type has a dispatch case that validates its payload, and an
            # empty payload failed validation -- still a type the fold
            # understood, and definitely not one it silently ignored.
            continue
        if event_type.name in report.ignored_types:
            ignored.append(event_type.name)

    assert not ignored, f"the fold ignores types the codebase emits: {ignored}"


def test_report_complete_means_every_consumed_type_was_understood() -> None:
    """The contract ProjectionReport.complete documents, held against the sweep."""
    log = started(EventLog())
    log.append("run_1", EventType.WORK_COMPLETED, {"item": 1})
    log.append("run_1", EventType.RUN_RESTORED, {"anchor": "a1"})

    _state, report = project_incremental("run_1", log.events("run_1"))

    # consumed counts the events the fold read, applied the ones it folded into
    # state; a recorded fact is consumed but not applied, and neither number
    # should be depressed by a type the fold simply did not recognise.
    assert report.consumed == 3
    assert report.applied == 2
    assert report.ignored_types == {}
    assert report.complete
