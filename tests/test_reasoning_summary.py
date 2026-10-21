"""Reasoning-context rehydration (issue #235).

Task-state recovery without cognitive-state recovery produces sessions that
are safe yet amnesiac: they know 47/100 is done, not what the agent was
thinking. `continuum_record_summary` stores a bounded, self-authored plan
summary; `continuum briefing` serves the newest one back at session start.
Summaries are EXTERNAL_AGENT evidence - informational on resume, never gating.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.recovery.summary import INFORMED_RETRY_CAP_BYTES, _fit
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "rs.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Long task"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "Long task"})
    yield path


def test_cli_briefing_serves_the_newest_summary(db: str) -> None:
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
    for needle in (
        "where the last session left off",
        "compare INV-004 to vendor list",
        "flag mismatch",
        "who approved discount?",
        "INV-004",
    ):
        assert needle in out, needle


def test_only_the_newest_summary_is_served(db: str) -> None:
    with SQLiteStorage(db) as store:
        for stack in (["old plan"], ["new plan"]):
            store.append_event(
                "run_1",
                EventType.REASONING_SUMMARY,
                {"summary": {"plan_stack": stack}},
                source=Origin.EXTERNAL_AGENT,
            )
    _, out, _ = run("--db", db, "briefing")
    assert "new plan" in out and "old plan" not in out


def test_no_summary_leaves_the_briefing_unchanged(db: str) -> None:
    code, out, _ = run("--db", db, "briefing")
    assert code == ExitCode.OK
    assert "where the last session left off" not in out


def test_summaries_are_non_gating_evidence(db: str) -> None:
    """A summary must never change mode or safety: it is orientation, not
    certification."""
    decision_before = _assess(db)
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {"summary": {"plan_stack": ["anything"]}},
            source=Origin.EXTERNAL_AGENT,
        )
    decision_after = _assess(db)
    assert decision_before.mode is decision_after.mode
    assert decision_before.safe == decision_after.safe


def _assess(db: str):
    from continuum.recovery import RecoveryEngine

    return RecoveryEngine(SQLiteStorage(db)).assess("run_1")


# --- MCP surface --------------------------------------------------------------- #


def test_summary_event_shape_is_bounded_json(db: str) -> None:
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.REASONING_SUMMARY,
            {"summary": {"plan_stack": ["s"], "note": "n"}},
            source=Origin.EXTERNAL_AGENT,
        )
        events = [e for e in store.read_events("run_1") if e.type is EventType.REASONING_SUMMARY]
    assert events[-1].payload["summary"]["plan_stack"] == ["s"]


def test_mcp_resume_flow_carries_summary_through_briefing_command(db: str, tmp_path: Path) -> None:
    """End-to-end through real processes: record over MCP stdio, brief via CLI."""
    import subprocess

    env = dict(os.environ)
    env["CONTINUUM_MCP_MUTATING_CLIENTS"] = "claude-code"
    handshake = [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code", "version": "1"},
                },
            }
        ),
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "continuum_record_summary",
                    "arguments": {
                        "run_id": "run_1",
                        "plan_stack": ["reconciling INV-77"],
                        "open_questions": ["vendor replied?"],
                    },
                },
            }
        ),
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "continuum.mcp.server", "--db", db],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    try:
        for line in handshake:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        deadline = 60
        reply = None
        while deadline:
            line = proc.stdout.readline()
            if not line:
                break
            parsed = json.loads(line)
            if parsed.get("id") == 2:
                reply = parsed
                break
    finally:
        proc.kill()
    assert reply is not None and "result" in reply, reply
    inner = json.loads(reply["result"]["content"][0]["text"])
    assert inner.get("recorded") is True, inner

    code, out, _ = run("--db", db, "--json", "briefing")
    assert code == ExitCode.OK
    context = json.loads(out)["context"]
    assert "reconciling INV-77" in context
    assert "vendor replied?" in context


def test_fit_evicts_in_priority_order() -> None:
    """Trim oversized blocks deterministically, preserving earlier entries."""
    block = {
        "avoid": [f"avoid-{i}" for i in range(6)],
        "current_failures": [
            {"id": "failure-1", "detail": "x" * 5000},
            {"id": "failure-2", "detail": "second detail"},
            {"id": "failure-3", "detail": "third detail"},
        ],
        "settled_effects": ["effect-1", "effect-2", "effect-3"],
        "last_attempt_steps": ["step-1", "step-2", "step-3"],
    }

    fitted = _fit(block)

    assert len(json.dumps(fitted, sort_keys=True).encode()) <= (INFORMED_RETRY_CAP_BYTES)
    assert fitted["avoid"] == block["avoid"][:4]
    assert fitted["current_failures"] == [{"id": "failure-1", "detail": ""}]
    assert fitted["settled_effects"] == ["effect-1"]
    assert fitted["last_attempt_steps"] == ["step-1"]
