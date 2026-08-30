"""Actionable recovery guidance (human_steps on resume/validate).

The contract names what is blocked; guidance renders what to do about it.
These tests pin the derivation rules and prove the CLI/MCP surfaces carry
the steps.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.guidance import human_steps_for
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def make_run(db: str, run_id: str = "run_1") -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id=run_id, goal="Do things"))
        store.append_event(run_id, EventType.RUN_STARTED, {"goal": "Do things"})


def seed_uncertain(db: str, action_type: str = "send_invoice", key: str = "invoice:1") -> str:
    with SQLiteStorage(db) as store:
        outcome = ActionLedger(store, "run_1").claim(action_type, {}, key=key)
    return outcome.action.action_id


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "g.db")
    make_run(path)
    yield path


def assess(db: str):
    from continuum.recovery import RecoveryEngine

    return RecoveryEngine(SQLiteStorage(db)).assess("run_1")


# --- pure derivation ---------------------------------------------------------- #


def test_safe_resume_yields_no_steps(db: str) -> None:
    decision = assess(db)  # nothing pending; but MCP provenance forces review
    steps = human_steps_for(decision, run_id="run_1")
    if decision.mode.value == "resume" and decision.safe:
        assert steps == []


def test_uncertain_action_without_probe_names_the_exact_call(db: str) -> None:
    seed_uncertain(db)
    decision = assess(db)
    steps = human_steps_for(decision, run_id="run_1", probed_types=[])
    reconcile = [t for t in steps if "continuum_reconcile_action" in t]
    assert reconcile, steps
    joined = " ".join(reconcile)
    assert "send_invoice" in joined and "run_1" in joined
    # The absence of automation is stated, so the agent knows why it is manual.
    assert any("no probe registered" in t for t in reconcile)


def test_a_registered_probe_becomes_one_command(db: str) -> None:
    seed_uncertain(db)
    decision = assess(db)
    steps = human_steps_for(decision, run_id="run_1", probed_types=["send_invoice"])
    auto = [t for t in steps if "continuum reconcile run_1" in t]
    assert auto, steps
    assert not any("continuum_reconcile_action" in t for t in steps)


def test_human_review_points_at_confirm(db: str) -> None:
    seed_uncertain(db)
    decision = assess(db)  # external-agent style plan includes human_review
    confirm = [t for t in human_steps_for(decision, run_id="run_1") if "confirm" in t]
    if any(s.kind.value == "human_review" for s in decision.plan.steps):
        assert confirm


# --- the self-report note --------------------------------------------------------- #


def test_self_report_note_is_withheld_while_an_action_is_unresolved(db: str) -> None:
    """ "Nothing is wrong" must not be said over an unreconciled side effect (#369).

    An uncertain action reaches `request_human` through
    `decision.uncertain_actions`, never through `report.statuses`, so a predicate
    that scanned only the validation report saw goal and progress alone and
    emitted the note beside a contract blocked on an unknown outcome. The agent
    was told "Work is not blocked" and pointed straight past the one thing this
    system exists to stop it walking past.
    """
    from continuum.recovery.guidance import self_report_guidance

    seed_uncertain(db)
    decision = assess(db)
    assert decision.uncertain_actions, "fixture must leave an unresolved action"
    assert self_report_guidance(decision) == {}


def test_self_report_note_is_given_when_only_provenance_blocks(db: str) -> None:
    """The note exists for a real case, so keep proving it still appears.

    A run whose only fault is that an agent reported its own goal and progress is
    not blocked, and the obvious next move (`continuum_confirm`) is refused by
    design, so without this note the caller is stranded with no legal way forward.
    """
    from continuum.models import Origin
    from continuum.recovery.guidance import self_report_guidance

    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.TASK_UPDATED,
            {"completed": 1, "total": 4, "pending": 3, "failed": 0},
            source=Origin.EXTERNAL_AGENT,
        )
    decision = assess(db)
    assert not decision.uncertain_actions
    note = self_report_guidance(decision)
    assert note, decision.mode
    assert "Nothing is wrong with this run" in note["self_report_guidance"]


# --- CLI + MCP surfaces ------------------------------------------------------------ #


def test_cli_resume_json_and_text_carry_the_steps(db: str, tmp_path: Path) -> None:
    seed_uncertain(db)
    code, out, err = run("--db", db, "--json", "resume", "run_1")
    assert code != ExitCode.OK
    payload = json.loads(out)
    assert payload["human_steps"], payload

    code, out, _ = run("--db", db, "resume", "run_1")
    assert "Next steps:" in out
    assert "1." in out


def test_cli_validate_also_carries_steps(db: str) -> None:
    seed_uncertain(db)
    code, out, _ = run("--db", db, "--json", "validate", "run_1", "--tolerate-unknown")
    payload = json.loads(out)
    assert isinstance(payload["human_steps"], list)


def test_gate_presence_is_surfaced_as_protocol_guidance(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".continuum").mkdir()
    (tmp_path / ".continuum" / "gate.json").write_text(json.dumps({"tools": {}}))
    seed_uncertain(db)
    code, out, err = run("--db", db, "--json", "resume", "run_1")
    payload = json.loads(out)
    assert any("gate active" in t for t in payload["human_steps"])


def test_mcp_resume_includes_human_steps(db: str, tmp_path: Path) -> None:
    """Drive the real stdio server: resume must carry executable steps."""
    import subprocess

    env = dict(os.environ)
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
                    "name": "continuum_resume",
                    "arguments": {"run_id": "run_1"},
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
        cwd=str(tmp_path),
    )
    try:
        for line in handshake:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        reply = None
        import time

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break  # server exited; do not spin
            parsed = json.loads(line)
            if parsed.get("id") == 2:
                reply = parsed
                break
    finally:
        proc.kill()
    assert reply is not None
    text = reply["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert isinstance(payload.get("human_steps"), list)


import os  # noqa: E402
