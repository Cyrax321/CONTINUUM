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

import pytest

from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Origin, Run
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
