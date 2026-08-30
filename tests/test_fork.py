"""Fork semantics (issue #259): detect, approve, deny, verify-after-fork.

The third outcome of replay-or-fork. These tests pin the detection rule
(token overlap on unclaimed same-type calls), the approval mechanics
(RUN_FORKED on the parent, linked child run), refusal of bad approvals,
chain integrity across the fork boundary, and the interplay with #243
family aggregation.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.gate import load_gate_config
from continuum.models import Origin, Run
from continuum.recovery.family import roll_up_children
from continuum.recovery.fork import ForkNeighbour, approve_fork, detect_fork_candidates
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "f.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Send invoices"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "Send invoices", "total": 3})
        ledger = ActionLedger(store, "run_1")
        outcome = ledger.claim(
            "send_invoice",
            {"recipient": "a@x.com", "path": "out/INV-001.pdf"},
            key="invoice:INV-001",
        )
        ledger.complete(outcome.key, external_id="INV-001.sent", result={"ok": True})
    yield path


def fold(db: str) -> dict:
    from continuum.actions.ledger import fold_action_events

    with SQLiteStorage(db) as store:
        return fold_action_events(store.read_events("run_1"))


# --- detection -------------------------------------------------------------- #


def test_token_overlap_surfaces_a_fork_candidate(db: str) -> None:
    candidates = detect_fork_candidates(
        action_type="send_invoice",
        tool_input={"recipient": "b@y.com", "outfile": "OUTBOX/INV-001.pdf"},
        actions_by_key=fold(db),
    )
    assert len(candidates) == 1
    neighbour = candidates[0]
    assert isinstance(neighbour, ForkNeighbour)
    assert neighbour.action_type == "send_invoice"
    assert neighbour.status == "completed"
    assert "INV-001" in neighbour.shared_tokens or "INV" in neighbour.shared_tokens


def test_no_overlap_yields_no_candidates(db: str) -> None:
    candidates = detect_fork_candidates(
        action_type="send_invoice",
        tool_input={"recipient": "c@z.com", "outfile": "out/INV-999.pdf"},
        actions_by_key=fold(db),
    )
    assert candidates == []


def test_other_action_types_never_match(db: str) -> None:
    candidates = detect_fork_candidates(
        action_type="delete_record",
        tool_input={"path": "out/INV-001.pdf"},
        actions_by_key=fold(db),
    )
    assert candidates == []


# --- gate surface ------------------------------------------------------------ #


def test_gate_refusal_carries_candidates_and_the_command(db: str, tmp_path: Path) -> None:
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(
        '{"tools": {"send_invoice_tool": '
        '{"key_template": "invoice:{invoice}", "action_type": "send_invoice"}}}'
    )
    config = load_gate_config(gate_path)
    from continuum.gate import decide

    # A different key that still shares the INV-001 resource token: this is
    # the divergence shape (same resource, new parameters) after a restore.
    decision = decide(
        config,
        "send_invoice_tool",
        {"invoice": "INV-001-draft", "recipient": "b@y.com", "outfile": "o/INV-001.pdf"},
        run_id="run_1",
        actions_by_key=fold(db),
    )
    assert decision.allow is False
    assert decision.fork_candidates
    assert "continuum fork run_1 --reason" in decision.reason

    unrelated = decide(
        config,
        "send_invoice_tool",
        {"invoice": "INV-777", "recipient": "d@w.com"},
        run_id="run_1",
        actions_by_key=fold(db),
    )
    assert unrelated.allow is False
    assert unrelated.fork_candidates == ()
    assert "continuum fork" not in unrelated.reason


# --- approval ---------------------------------------------------------------- #


def test_approve_fork_writes_event_and_linked_child(db: str) -> None:
    with SQLiteStorage(db) as store:
        before = store.latest_version("run_1")
        child = approve_fork(store, "run_1", reason="payee renegotiated the amount")

        assert child.parent_run_id == "run_1"
        assert child.metadata["fork"] == "true"
        assert child.metadata["fork_reason"] == "payee renegotiated the amount"

        events = store.read_events("run_1")
        fork_events = [e for e in events if e.type is EventType.RUN_FORKED]
        assert len(fork_events) == 1
        event = fork_events[0]
        assert event.source is Origin.HUMAN
        assert event.payload["child_run_id"] == child.run_id
        assert event.payload["divergence_sequence"] == (before.source_sequence if before else 0)

        # The parent's semantic state is untouched by the lineage record.
        from continuum.state.semantic import project

        state = project("run_1", events)
        assert state.goal.description == "Send invoices"
        assert child.run_id not in {e.type.value for e in events}


def test_fork_child_is_independently_resumable(db: str) -> None:
    """The child must be usable straight out of approve_fork.

    This test used to append RUN_STARTED to the child itself before resuming,
    which meant it proved the fixture's work rather than the code's: fork wrote
    only a run row, so the child had an empty log and every reader refused it,
    including the `continuum resume <child>` line that `continuum fork` prints as
    the next step. Same defect class as #47. approve_fork now writes the row and
    its RUN_STARTED in one transaction, so nothing is staged here.
    """
    with SQLiteStorage(db) as store:
        child = approve_fork(store, "run_1", reason="new terms")
        # The child's own log is startable with no help from this test.
        events = store.read_events(child.run_id)
        assert events[0].type == EventType.RUN_STARTED
        assert events[0].source is Origin.HUMAN
        assert all(e.type in (EventType.RUN_STARTED, EventType.ATTEMPT_LESSON) for e in events)

    code, out, _ = run("--db", db, "resume", child.run_id)
    # Resume reaches its own verdict about the CHILD, independent of the parent.
    assert code == ExitCode.OK
    assert f"Run: {child.run_id}" in out
    assert "Recovery decision: RESUME" in out
    assert "FAMILY BLOCKED" not in out


def test_every_reader_accepts_a_fresh_fork_child(db: str) -> None:
    """The commands `continuum fork` points the user at must all work.

    `fork` prints "Resume it independently" and "Lineage: continuum tree", so
    those two at minimum have to succeed on an untouched child. The rest are
    included because they share the projection path that an empty log breaks.
    """
    with SQLiteStorage(db) as store:
        child = approve_fork(store, "run_1", reason="branching")

    for command in ("resume", "inspect", "replay", "verify", "events", "show-contract"):
        code, _, err = run("--db", db, command, child.run_id)
        assert code == ExitCode.OK, f"{command} failed on a fresh fork child: {err}"

    code, out, _ = run("--db", db, "tree", "run_1")
    assert code == ExitCode.OK
    assert child.run_id in out
    assert "assess error" not in out, f"tree still cannot assess the child: {out}"


def test_duplicate_child_and_empty_reason_are_refused(db: str) -> None:
    with SQLiteStorage(db) as store:
        approve_fork(store, "run_1", reason="first", child_run_id="fork_x")
        with pytest.raises(ValueError, match="already exists"):
            approve_fork(store, "run_1", reason="second", child_run_id="fork_x")
        with pytest.raises(ValueError, match="reason"):
            approve_fork(store, "run_1", reason="   ")

    events = SQLiteStorage(db).read_events("run_1")  # type: ignore[call-arg]
    forks = [e for e in events if e.type is EventType.RUN_FORKED]
    assert len(forks) == 1


def test_auto_named_children_do_not_collide(db: str) -> None:
    with SQLiteStorage(db) as store:
        first = approve_fork(store, "run_1", reason="a")
        second = approve_fork(store, "run_1", reason="b")
    assert first.run_id != second.run_id
    assert first.run_id.startswith("run_1_fork")


def test_cli_fork_command_records_and_tree_marks_it(db: str) -> None:
    code, out, _ = run("--db", db, "fork", "run_1", "--reason", "split strategy", "--child", "fk1")
    assert code == ExitCode.OK
    assert "fk1" in out

    code, out, _ = run("--db", db, "tree", "run_1")
    assert code == ExitCode.OK
    assert "[fork]" in out and "fk1" in out

    code, _, err = run("--db", db, "fork", "run_1", "--reason", "dup", "--child", "fk1")
    assert code != ExitCode.OK


def test_missing_run_exits_not_found(db: str) -> None:
    code, _, _ = run("--db", db, "fork", "ghost", "--reason", "x")
    assert code != ExitCode.OK


# --- chain integrity and family interplay ------------------------------------ #


def test_both_chains_verify_after_fork_and_tamper_is_local(db: str) -> None:
    with SQLiteStorage(db) as store:
        child = approve_fork(store, "run_1", reason="branch")
        store.append_event(child.run_id, EventType.RUN_STARTED, {"goal": child.goal})

    code, out, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.OK, out
    code, out, _ = run("--db", db, "verify", child.run_id)
    assert code == ExitCode.OK, out

    # Tamper with the parent log: the parent must fail verification while the
    # child's chain stays trusted.
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE events SET payload = json_set(payload, '$.tampered', 1) "
        "WHERE run_id = 'run_1' AND sequence = 2"
    )
    conn.commit()
    conn.close()

    code, _, _ = run("--db", db, "verify", "run_1")
    assert code != ExitCode.OK
    code, _, _ = run("--db", db, "verify", child.run_id)
    assert code == ExitCode.OK


def test_an_unsafe_fork_blocks_the_parent_rollup(db: str) -> None:
    with SQLiteStorage(db) as store:
        child = approve_fork(store, "run_1", reason="risky branch")
        store.append_event(child.run_id, EventType.RUN_STARTED, {"goal": child.goal})
        # Leave an uncertain side effect inside the fork.
        ActionLedger(store, child.run_id).claim("wire_transfer", {}, key="wire:1")

    statuses, blocked = roll_up_children(SQLiteStorage(db), "run_1")
    assert any(s.run_id == child.run_id for s in statuses)
    assert blocked is True
