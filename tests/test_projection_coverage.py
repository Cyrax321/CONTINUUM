"""Pin the projection fold's coverage of the event type enum (#1169).

``_dispatch`` folds 20 ``EventType`` members into state and ``_NON_PROJECTING``
declares 20 more as recorded facts the fold is right to skip. That left 11
members in neither list for four releases: they fell through to ``case _:
return False`` and landed in ``report.ignored_types``, the field
``ProjectionReport.complete`` defines as "the fold understood every event type
it consumed". So any run that restored, merged, confirmed, or notified reported
as partially unprojectable, with ``report.applied`` understated and
``ignored_types`` (which ``test_projection_edges.py`` reserves for a deliberately
bogus ``QUANTUM_ENTANGLED``) filling with names the codebase legitimately emits.

The fix declared those 11 non-projecting; this module stops a twelfth from being
introduced silently. The coverage test parses ``semantic.py`` the same way the
gap was found, so a member added to ``EventType`` without either a fold case or a
declaration fails here rather than shipping as a false ``complete = False``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from continuum.events import Event, EventLog, EventType
from continuum.state.semantic import _NON_PROJECTING, project_incremental

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "continuum" / "state" / "semantic.py"


def _handled_type_names() -> set[str]:
    """Every ``EventType`` member the fold names, by case or by declaration."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    cases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Match):
            for case in node.cases:
                pattern = case.pattern
                if isinstance(pattern, ast.MatchValue) and isinstance(pattern.value, ast.Attribute):
                    cases.add(pattern.value.attr)
    return cases | {member.name for member in _NON_PROJECTING}


def test_every_event_type_is_folded_or_declared() -> None:
    """``set(EventType)`` is exactly the fold cases plus the declarations."""
    unhandled = {member.name for member in EventType} - _handled_type_names()
    assert not unhandled, (
        f"{len(unhandled)} EventType members are neither folded by _dispatch nor "
        f"declared in _NON_PROJECTING: {sorted(unhandled)}. Each falls through to "
        "'case _: return False' and is counted in report.ignored_types, so a run "
        "that emits one reports complete = False and understates report.applied. "
        "Either add a fold case if the event changes projected state, or declare "
        "it non-projecting with a comment naming the issue if it is a recorded fact."
    )


def test_declared_non_projecting_events_do_not_mar_the_report() -> None:
    """A run emitting any declared fact reports complete, with nothing ignored.

    This is the symptom #1169 reported: a restored, merged, confirming, or
    notifying run read as partially unprojectable. Every member of the set is
    exercised, not just the four the issue reproduced with, so a future member
    added to the set is covered too.
    """
    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
    for member in _NON_PROJECTING:
        log.append("run_1", member, {})
    state, report = project_incremental("run_1", log.events("run_1"))

    assert not report.ignored_types, (
        f"declared non-projecting types reached report.ignored_types: {report.ignored_types}"
    )
    assert report.complete, (
        "the fold understood every event type it consumed, but report.complete "
        "is False; a declared non-projecting type is being counted as ignored"
    )
    assert report.applied == 1, (
        f"only RUN_STARTED carries state in this stream, applied = {report.applied}"
    )
    assert state.goal.description == "g"


def test_a_genuinely_unknown_type_is_still_reported() -> None:
    """Declaring 11 types must not silence the signal for a real stranger.

    ``test_projection_edges.py`` reserves ``ignored_types`` for events the enum
    does not define; this asserts the channel still fires after the fix, so the
    guard is not masking the failure it exists to surface.
    """
    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
    known = log.events("run_1")[0]
    future = Event(
        run_id="run_1",
        sequence=2,
        type=EventType.TOOL_CALLED,
        payload={},
        prev_hash=known.hash,
    ).sealed()
    unknown = future.model_copy(update={"type": "QUANTUM_ENTANGLED"})

    state, report = project_incremental("run_1", [known, unknown])
    assert state.goal.description == "g"
    assert report.ignored_types == {"QUANTUM_ENTANGLED": 1}
    assert not report.complete
