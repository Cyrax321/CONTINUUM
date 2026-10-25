"""Briefing curation by provenance, not recency (issue #742).

The default briefing rehydrates verified-contract facts, validated state and
system-derived lessons *before* any agent-authored summary, labels every
section's provenance, quarantines stale/contradicted items with a reason, and
omits agent summaries whose environment pins no longer hold - recoverable
through the explicit ``--raw-summary`` diagnostic. These tests pin that
contract, determinism of repeated renders, and that the recovery verdict
itself is untouched.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import (
    Component,
    ComponentValidationEntry,
    Evidence,
    Finding,
    Goal,
    Origin,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    Run,
    SemanticState,
    StateStatus,
    StateValidationResult,
)
from continuum.recovery.briefing_curation import (
    _MAX_QUARANTINE_ITEMS,
    _latest_agent_summary,
    _quarantine,
    curate_briefing,
    render_curated_briefing,
)
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "brief.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Long task"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "Long task"})
    yield path


def test_briefing_orders_verified_before_agent_summary(db: str) -> None:
    """The agent summary renders last, under an explicit provenance label."""
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {
                "summary": {
                    "plan_stack": ["compare INV-004 to vendor list"],
                    "decisions": [{"what": "flag mismatch", "why": "price drift"}],
                    "open_questions": ["who approved discount?"],
                    "working_set": ["INV-004", "vendors.csv"],
                }
            },
            source=Origin.EXTERNAL_AGENT,
        )
    code, out, err = run("--db", db, "briefing")
    assert code == ExitCode.OK, err
    assert "CONTINUUM active run: run_1" in out
    # Verified sections come first and every section carries a provenance tier.
    assert "recovery verdict (verified, sealed contract)" in out
    assert "semantic state (verified, projected from events)" in out
    # Agent material is visually marked as self-authored and renders last.
    assert "where the last session left off (self-authored, unverified)" in out
    assert out.index("recovery verdict") < out.index("where the last session left off")
    for needle in ("compare INV-004 to vendor list", "flag mismatch", "INV-004"):
        assert needle in out, needle


def test_briefing_json_exposes_curation_structure(db: str) -> None:
    """The --json payload carries sections, quarantine and omission reasons."""
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {"summary": {"plan_stack": ["old plan"]}},
            source=Origin.EXTERNAL_AGENT,
        )
    code, out, err = run("--db", db, "--json", "briefing")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    titles = [s["title"] for s in payload["curated_sections"]]
    assert titles[0].startswith("recovery verdict")
    assert titles[-1].startswith("where the last session left off")
    assert all("provenance" in s and "reason" in s for s in payload["curated_sections"])
    assert payload["curated_sections"][-1]["provenance"] == "agent"
    # Nothing stale in this fixture, so no quarantine and no omissions.
    assert payload["quarantine"] == []
    assert payload["omitted"] == []


def test_briefing_without_summary_has_no_agent_section(db: str) -> None:
    """No REASONING_SUMMARY means no agent-authored section, no crash."""
    code, out, err = run("--db", db, "briefing")
    assert code == ExitCode.OK, err
    assert "recovery verdict (verified, sealed contract)" in out
    assert "where the last session left off" not in out


def test_raw_summary_diagnostic_prints_verbatim_payload(db: str) -> None:
    """--raw-summary bypasses curation; the log stays recoverable."""
    payload = {"summary": {"plan_stack": ["old plan"]}, "extra": "kept"}
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1", EventType.REASONING_SUMMARY, payload, source=Origin.EXTERNAL_AGENT
        )
    code, out, err = run("--db", db, "briefing", "--raw-summary")
    assert code == ExitCode.OK, err
    assert json.loads(out) == payload


def test_raw_summary_diagnostic_without_a_summary(db: str) -> None:
    code, out, err = run("--db", db, "briefing", "--raw-summary")
    assert code == ExitCode.OK, err
    assert "No reasoning summary recorded" in out


def test_repeated_renders_are_byte_identical(db: str) -> None:
    """Deterministic curation: two identical inputs render the same bytes."""
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {"summary": {"plan_stack": ["p1", "p2"], "open_questions": ["q1"]}},
            source=Origin.EXTERNAL_AGENT,
        )
    _, first, _ = run("--db", db, "briefing")
    _, second, _ = run("--db", db, "briefing")
    assert first == second


def test_agent_summary_bounds_honoured(db: str) -> None:
    """Plan items cap at 3: the briefing stays a bounded hook payload."""
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {
                "summary": {
                    "plan_stack": [f"plan {i}" for i in range(6)],
                }
            },
            source=Origin.EXTERNAL_AGENT,
        )
    _, out, _ = run("--db", db, "briefing")
    assert "plan 0" in out and "plan 2" in out
    assert "plan 3" not in out and "plan 5" not in out


def test_briefing_names_the_risks_behind_the_verdict(db: str) -> None:
    """Issue #1424: the verified contract section lists the RISK_OBSERVED ids
    that produced the verdict, so a resumed session knows which observation to
    explain rather than only the mode it ended in."""
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.RISK_OBSERVED,
            {"risk_id": "risk-1", "trigger": "meltdown"},
            source=Origin.EXTERNAL_MONITOR,
        )
    code, out, err = run("--db", db, "briefing")
    assert code == ExitCode.OK, err
    assert "recovery verdict:" in out
    assert "triggering risks:" in out


def _archive_summary_prefix(db: str, summary_line: str) -> None:
    """Record a summary, give the run a live tail, then compact the prefix
    (summary included) into the archive so it lives only in events_archive."""
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {"summary": {"plan_stack": [summary_line]}},
            source=Origin.EXTERNAL_AGENT,
        )
        store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 1, "total": 3})
        store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 2, "total": 3})
        # The summary is at sequence 2 (RUN_STARTED is 1); archiving through 2
        # moves it out of the live tail while the run stays resumable.
        store.compact_run("run_1", through_sequence=2)
        assert not any(e.type is EventType.REASONING_SUMMARY for e in store.read_events("run_1")), (
            "precondition: summary must be out of the live tail"
        )
        assert any(e.type is EventType.REASONING_SUMMARY for e in store.read_all_events("run_1")), (
            "precondition: summary must survive in the archive"
        )


def test_briefing_surfaces_agent_summary_after_compaction(db: str) -> None:
    """Regression (#1128): a summary the run compacted into the archive must
    still reach the curated briefing. Curation folds full history, not just the
    live tail, so the agent section does not vanish exactly when the long run it
    exists for has run long enough to be compacted."""
    _archive_summary_prefix(db, "reconcile INV-004 against the vendor list")
    code, out, err = run("--db", db, "briefing")
    assert code == ExitCode.OK, err
    assert "where the last session left off (self-authored, unverified)" in out
    assert "reconcile INV-004 against the vendor list" in out


def test_raw_summary_diagnostic_reads_the_archive_after_compaction(db: str) -> None:
    """Regression (#1128): --raw-summary reads full history too, so a compacted
    run reports its archived summary instead of 'none recorded'."""
    _archive_summary_prefix(db, "settle outstanding amount for INV-001")
    code, out, err = run("--db", db, "briefing", "--raw-summary")
    assert code == ExitCode.OK, err
    assert "No reasoning summary recorded" not in out
    assert json.loads(out)["summary"]["plan_stack"] == ["settle outstanding amount for INV-001"]


# --- unit tests: quarantine, pin conflicts and rendering (issue #1755) ------ #
#
# The CLI-driven tests above cover the happy path through a real database. The
# branches below are deterministic functions of their inputs alone, so they are
# exercised directly with in-memory states and a stub storage: no fixture, no
# filesystem, no recovery engine to build just to reach one line of curation.


class _StubStorage:
    """Storage stand-in exposing only ``read_all_events``.

    ``_latest_agent_summary`` and ``curate_briefing`` fold the run's events
    through that single method, so a canned list is the whole surface they
    need. Returns a fresh copy per call so a test cannot mutate the fixture.
    """

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def read_all_events(self, run_id: str) -> list[Any]:
        return list(self._events)


def _event(payload: dict[str, Any]) -> SimpleNamespace:
    """A REASONING_SUMMARY event with the given payload."""
    return SimpleNamespace(type=EventType.REASONING_SUMMARY, payload=payload)


def _decision(*statuses: ComponentValidationEntry, state: Any = None) -> Any:
    """A decision carrying only what curation and the pin check read.

    Built from the real contract, report and state models where they exist;
    ``SimpleNamespace`` glues them into the shape ``RecoveryDecision`` exposes,
    without having to run the engine to produce one.
    """
    return SimpleNamespace(
        mode=RecoveryMode.RESUME,
        safe=True,
        contract=RecoveryContract(
            run_id="run_1",
            recovery_status=RecoverySafety.SAFE_TO_RESUME,
            reason="checkpoint verified",
            next_allowed_action="resume",
        ),
        state=state
        or SemanticState(run_id="run_1", goal=Goal(description="Settle the invoice batch")),
        informed_retry=None,
        validation=SimpleNamespace(
            report=StateValidationResult(run_id="run_1", statuses=list(statuses))
        ),
    )


def test_quarantine_lists_non_valid_items_with_kind_id_and_reason() -> None:
    """Stale, invalid and requires-review items surface under their kind."""
    state = SimpleNamespace(
        evidence=[
            Evidence(summary="vendor list snapshot", status=StateStatus.STALE),
            Evidence(summary="quote from the draft email", status=StateStatus.INVALID),
        ],
        findings=[Finding(claim="INV-004 is settled", status=StateStatus.REQUIRES_REVIEW)],
    )
    assert _quarantine(state) == [
        {
            "kind": "evidence",
            "id": "vendor list snapshot",
            "reason": "status stale - do not rely on this",
        },
        {
            "kind": "evidence",
            "id": "quote from the draft email",
            "reason": "status invalid - do not rely on this",
        },
        {
            "kind": "finding",
            "id": "INV-004 is settled",
            "reason": "status requires_review - do not rely on this",
        },
    ]


def test_quarantine_skips_valid_items_and_empty_state() -> None:
    """VALID items never enter quarantine; a clean state yields no list."""
    clean = SimpleNamespace(
        evidence=[Evidence(summary="vendor list snapshot"), Evidence(summary="ledger hash")],
        findings=[Finding(claim="INV-004 is settled")],
    )
    assert _quarantine(clean) == []
    assert _quarantine(SimpleNamespace(evidence=[], findings=[])) == []


def test_quarantine_falls_back_to_evidence_id_without_summary() -> None:
    """An evidence item with an empty summary is named by its id."""
    evidence = Evidence(summary="", status=StateStatus.STALE)
    quarantined = _quarantine(SimpleNamespace(evidence=[evidence], findings=[]))
    assert quarantined == [
        {
            "kind": "evidence",
            "id": evidence.evidence_id,
            "reason": "status stale - do not rely on this",
        }
    ]


def test_quarantine_is_bounded() -> None:
    """The quarantine block caps at _MAX_QUARANTINE_ITEMS, first-seen order."""
    state = SimpleNamespace(
        evidence=[Evidence(summary=f"evidence {i}", status=StateStatus.STALE) for i in range(10)],
        findings=[Finding(claim=f"claim {i}", status=StateStatus.INVALID) for i in range(10)],
    )
    quarantined = _quarantine(state)
    assert len(quarantined) == _MAX_QUARANTINE_ITEMS
    assert quarantined[0]["id"] == "evidence 0"
    assert quarantined[-1]["id"] == f"evidence {_MAX_QUARANTINE_ITEMS - 1}"
    # Everything past the cap is dropped, not merely unrendered.
    assert "evidence 9" not in {item["id"] for item in quarantined}


def test_agent_summary_pins_conflict_with_validation_report() -> None:
    """A pinned resource the fold reports conflicted means pins_hold is False."""
    events = [
        _event(
            {
                "summary": {"plan_stack": ["match INV-004 to the ledger"]},
                "pinning": {"vendors.csv": "sha-v1"},
            }
        )
    ]
    decision = _decision(
        ComponentValidationEntry(
            component=Component.ENVIRONMENT,
            component_id="vendors.csv",
            status=StateStatus.CONFLICTED,
        )
    )
    summary, pins_hold = _latest_agent_summary(_StubStorage(events), "run_1", decision)
    assert summary == {"plan_stack": ["match INV-004 to the ledger"]}
    assert pins_hold is False


def test_agent_summary_pins_hold_without_overlap() -> None:
    """A conflicted resource no pin names leaves the summary rehydratable."""
    events = [
        _event(
            {
                "summary": {"plan_stack": ["match INV-004 to the ledger"]},
                "pinning": {"vendors.csv": "sha-v1"},
            }
        )
    ]
    decision = _decision(
        ComponentValidationEntry(
            component=Component.ENVIRONMENT,
            component_id="ledger.csv",
            status=StateStatus.CONFLICTED,
        )
    )
    _, pins_hold = _latest_agent_summary(_StubStorage(events), "run_1", decision)
    assert pins_hold is True


def test_agent_summary_ignores_conflicts_that_name_no_component() -> None:
    """A conflict entry without a component_id cannot match any pin."""
    events = [
        _event(
            {
                "summary": {"plan_stack": ["match INV-004 to the ledger"]},
                "pinning": {"vendors.csv": "sha-v1"},
            }
        )
    ]
    decision = _decision(
        ComponentValidationEntry(
            component=Component.ENVIRONMENT,
            component_id=None,
            status=StateStatus.CONFLICTED,
        )
    )
    _, pins_hold = _latest_agent_summary(_StubStorage(events), "run_1", decision)
    assert pins_hold is True


def test_agent_summary_without_pinning_passes() -> None:
    """Unpinned summaries carry no world to contradict, so they always pass."""
    events = [_event({"summary": {"plan_stack": ["match INV-004 to the ledger"]}})]
    # A conflicted report is irrelevant when the summary pins nothing.
    decision = _decision(
        ComponentValidationEntry(
            component=Component.ENVIRONMENT,
            component_id="vendors.csv",
            status=StateStatus.CONFLICTED,
        )
    )
    _, pins_hold = _latest_agent_summary(_StubStorage(events), "run_1", decision)
    assert pins_hold is True


def test_agent_summary_with_malformed_payload_yields_empty_summary() -> None:
    """A summary field that is not a dict degrades to {} rather than crashing."""
    events = [_event({"summary": "the vendor list looked off"})]
    summary, pins_hold = _latest_agent_summary(_StubStorage(events), "run_1", _decision())
    assert summary == {}
    assert pins_hold is True


def test_agent_summary_missing_returns_none() -> None:
    """A run with no REASONING_SUMMARY yields None, not an empty section."""
    assert _latest_agent_summary(_StubStorage([]), "run_1", _decision()) is None


def test_curate_briefing_omits_agent_summary_when_pins_conflict() -> None:
    """A pin-conflicted summary drops out of sections and lands in omitted."""
    events = [
        _event(
            {
                "summary": {"plan_stack": ["match INV-004 to the ledger"]},
                "pinning": {"vendors.csv": "sha-v1"},
            }
        )
    ]
    decision = _decision(
        ComponentValidationEntry(
            component=Component.ENVIRONMENT,
            component_id="vendors.csv",
            status=StateStatus.CONFLICTED,
        )
    )
    curated = curate_briefing(_StubStorage(events), "run_1", decision)
    titles = [section["title"] for section in curated["sections"]]
    assert not any("last session left off" in title for title in titles)
    assert curated["omitted"] == [
        {
            "kind": "agent summary",
            "reason": "environment pins on the newest summary no longer hold; "
            "read it via `continuum briefing <run_id> --raw-summary`",
        }
    ]


def test_render_quarantine_block_exact_format() -> None:
    """Quarantined items render under the header, one indented line each."""
    curated: dict[str, Any] = {
        "sections": [],
        "quarantine": [
            {
                "kind": "evidence",
                "id": "vendor list snapshot",
                "reason": "status stale - do not rely on this",
            },
            {
                "kind": "finding",
                "id": "INV-004 is settled",
                "reason": "status requires_review - do not rely on this",
            },
        ],
        "omitted": [],
    }
    assert render_curated_briefing(curated) == [
        "do not trust (quarantined):",
        "  [evidence] vendor list snapshot - status stale - do not rely on this",
        "  [finding] INV-004 is settled - status requires_review - do not rely on this",
        "",
    ]


def test_render_omitted_entries_exact_format() -> None:
    """Omissions render as one omitted: line naming kind and reason."""
    reason = (
        "environment pins on the newest summary no longer hold; "
        "read it via `continuum briefing <run_id> --raw-summary`"
    )
    curated: dict[str, Any] = {
        "sections": [],
        "quarantine": [],
        "omitted": [{"kind": "agent summary", "reason": reason}],
    }
    assert render_curated_briefing(curated) == [f"omitted: agent summary - {reason}"]


def test_render_sections_keep_titles_lines_and_trailing_blank() -> None:
    """Each section renders its title, indented lines, then a blank line."""
    curated: dict[str, Any] = {
        "sections": [
            {"title": "recovery verdict (verified, sealed contract)", "lines": ["a", "b"]},
            {"title": "semantic state (verified, projected from events)", "lines": ["c"]},
        ],
        "quarantine": [],
        "omitted": [],
    }
    assert render_curated_briefing(curated) == [
        "recovery verdict (verified, sealed contract)",
        "  a",
        "  b",
        "",
        "semantic state (verified, projected from events)",
        "  c",
        "",
    ]


def test_render_empty_curation_produces_no_lines() -> None:
    """Nothing to say renders as nothing, not an empty header."""
    curated: dict[str, Any] = {"sections": [], "quarantine": [], "omitted": []}
    assert render_curated_briefing(curated) == []
