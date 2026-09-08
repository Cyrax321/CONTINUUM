"""Event-log compaction (issue #239).

`continuum compact` archives the pre-anchor prefix of a run's log and
appends an EVENT_LOG_ANCHORED marker, keeping the live chain append-only
while bounding replay cost for month-long runs. These tests pin the
mechanics: prefix moves verbatim to the archive, the anchor chains onto
the archive boundary so verify fails when the boundary or the archive is
tampered with, post-anchor projection via checkpoint restore stays
correct, resume/verify/replay all work on compacted runs, archived
completed actions still guard against replays, and engines without an
archive refuse the command.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from continuum.actions.idempotency import idempotency_key
from continuum.checkpoint import CheckpointManager
from continuum.cli import ExitCode, main
from continuum.cli.main import cmd_compact
from continuum.events import Event, EventType
from continuum.models import ActionStatus, Origin, Run, SemanticState, utcnow
from continuum.replayguard import GuardKind, protected_call
from continuum.state.semantic import project_incremental
from continuum.state.versioning import state_fingerprint
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "c.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="long task"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "long task", "total": 10})
        CheckpointManager(store).checkpoint("run_1")
    yield path


def work(db: str, i: int) -> None:
    with SQLiteStorage(db) as store:
        kind, _ = protected_call(
            store,
            "run_1",
            action_type="process_doc",
            key=f"doc:{i}",
            fn=lambda doc=i: {"doc": doc},
        )
        assert kind is GuardKind.ALLOW


def test_compact_archives_prefix_and_keeps_tail_intact(db: str) -> None:
    for i in range(6):
        work(db, i)
    with SQLiteStorage(db) as store:
        pre_count = len(store.read_events("run_1"))

    code, out, err = run("--db", db, "--json", "compact", "run_1", "--force")
    assert code == ExitCode.OK, err

    with SQLiteStorage(db) as store:
        live = store.read_events("run_1")
        archived = [
            dict(r)
            for r in store._connection.execute(
                "SELECT * FROM events_archive WHERE run_id = 'run_1' ORDER BY sequence"
            )
        ]
    # The forced anchor-checkpoint's own marker plus the ANCHOR event remain
    # live; everything at or before their boundary is archived verbatim.
    assert len(live) == 2
    assert {e.type for e in live} == {
        EventType.STATE_CHECKPOINTED,
        EventType.EVENT_LOG_ANCHORED,
    }
    assert live[-1].type is EventType.EVENT_LOG_ANCHORED
    assert archived[0]["sequence"] == 1
    assert len(archived) == pre_count

    # Chain integrity on the live log.
    code, _, err = run("--db", db, "verify", "run_1")
    assert code == ExitCode.OK, err


def test_verify_flags_tampered_archive_rows_deep(db: str) -> None:
    """Editing history in events_archive must fail verify, not just a
    hand-rolled digest recomputation in the test (PR #253 review)."""
    work(db, 1)
    run("--db", db, "--json", "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        store._connection.execute("UPDATE events_archive SET payload = '{\"tampered\": true}'")
        report = store.verify_events("run_1")
    assert not report.ok
    assert any(v.kind == "TAMPERED_CONTENT" for v in report.violations)

    code, _, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.CORRUPTED


def test_verify_rejects_a_deleted_boundary_event(db: str) -> None:
    """The exploit the anchored walk used to allow: delete the first live
    row (the forced checkpoint marker) and the old logic trusted whatever
    came next as genesis. The walk must resume at the archive edge."""
    work(db, 1)
    run("--db", db, "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        store._connection.execute(
            "DELETE FROM events WHERE sequence ="
            " (SELECT MIN(sequence) FROM events WHERE run_id = 'run_1')"
        )
        report = store.verify_events("run_1")
    assert not report.ok
    kinds = {v.kind for v in report.violations}
    assert "SEQUENCE_GAP" in kinds or "BROKEN_CHAIN" in kinds

    code, _, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.CORRUPTED


def test_verify_rejects_a_truncated_archive(db: str) -> None:
    work(db, 1)
    run("--db", db, "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        store._connection.execute(
            "DELETE FROM events_archive WHERE sequence ="
            " (SELECT MAX(sequence) FROM events_archive WHERE run_id = 'run_1')"
        )
        report = store.verify_events("run_1")
    assert not report.ok

    code, _, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.CORRUPTED


def test_verify_rejects_an_anchored_log_with_no_archive(db: str) -> None:
    """An anchor without its archive is not a genesis; it is missing
    history, and verify must say so."""
    work(db, 1)
    run("--db", db, "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        store._connection.execute("DELETE FROM events_archive WHERE run_id = 'run_1'")
        report = store.verify_events("run_1")
    assert not report.ok

    code, _, _ = run("--db", db, "verify", "run_1")
    assert code == ExitCode.CORRUPTED


def test_resume_works_on_a_compacted_run(db: str) -> None:
    for i in range(3):
        work(db, i)
    code, _, err = run("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err
    code, out, err = run("--db", db, "--json", "resume", "run_1")
    assert code in (ExitCode.OK, ExitCode.REQUIRES_HUMAN), err
    payload = json.loads(out)
    assert payload["mode"] in ("resume", "request_human")


def test_replay_reports_anchored_mode(db: str) -> None:
    work(db, 5)
    run("--db", db, "compact", "run_1", "--force")
    code, out, err = run("--db", db, "--json", "replay", "run_1")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert "anchored" in payload["verification"]
    # The anchored branch verifies for real; it never reports a hardcoded pass.
    assert payload["verified"] is True


def test_replay_fails_when_the_stored_version_disagrees(db: str) -> None:
    """A compacted run must not lose replay's corruption contract (PR #253
    review): editing the stored version has to surface as CORRUPTED."""
    work(db, 5)
    run("--db", db, "compact", "run_1", "--force")
    with SQLiteStorage(db) as store:
        row = store._connection.execute(
            "SELECT version, state FROM versions WHERE run_id = 'run_1' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        # Tamper coherently: rewrite the stored state AND its fingerprint
        # column, so the read-back integrity check passes and the tampering
        # can only be caught by replay's actual comparison. Editing progress
        # instead would trip the over-total guard at load time and never
        # reach it.
        mutated = SemanticState.model_validate_json(row["state"])
        mutated = mutated.model_copy(
            update={"goal": mutated.goal.model_copy(update={"description": "tampered goal"})}
        )
        store._connection.execute(
            "UPDATE versions SET state = ?, fingerprint = ? WHERE run_id = 'run_1' AND version = ?",
            (mutated.model_dump_json(), state_fingerprint(mutated), row["version"]),
        )
    code, out, err = run("--db", db, "--json", "replay", "run_1")
    assert code == ExitCode.CORRUPTED, err
    payload = json.loads(out)
    assert payload["verified"] is False


def test_inspect_and_status_survive_compaction(db: str) -> None:
    work(db, 2)
    run("--db", db, "compact", "run_1", "--force")
    code, out, _ = run("--db", db, "inspect", "run_1")
    assert code == ExitCode.OK
    code, out, _ = run("--db", db, "status", "run_1")
    assert code == ExitCode.OK


def test_compact_requires_an_existing_version(tmp_path: Path) -> None:
    path = str(tmp_path / "empty.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="bare", goal="g"))
    code, _, err = (
        run("--db", path, "--force", "compact", "bare")
        if False
        else run("--db", path, "compact", "bare", "--force")
    )
    assert code == ExitCode.ERROR
    assert "anchored" in err or "no stored version" in err


def test_repeated_compaction_replays_only_the_live_tail(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compacted run remains eligible without rescanning its archive."""
    work(db, 1)
    code, _, err = run("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err

    work(db, 2)
    original = SQLiteStorage.read_archived_events

    def reject_archive_read(self: SQLiteStorage, run_id: str):
        raise AssertionError(f"project_current rescanned the archive for {run_id}")

    monkeypatch.setattr(SQLiteStorage, "read_archived_events", reject_archive_read)
    code, _, err = run("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err

    monkeypatch.setattr(SQLiteStorage, "read_archived_events", original)
    with SQLiteStorage(db) as store:
        assert len(store.read_archived_events("run_1")) > 0
        assert store.verify_events("run_1").ok


def test_bounded_size_after_compaction(db: str) -> None:
    """The acceptance core: compaction bounds live-log growth."""
    for i in range(40):
        work(db, i)
    with SQLiteStorage(db) as store:
        before = len(store.read_events("run_1"))
    code, _, err = run("--db", db, "compact", "run_1", "--force")
    assert code == ExitCode.OK, err
    for i in range(40, 50):
        work(db, i)
    with SQLiteStorage(db) as store:
        live_rows = store._connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE run_id='run_1'"
        ).fetchone()["n"]
        archived_rows = store._connection.execute(
            "SELECT COUNT(*) AS n FROM events_archive WHERE run_id='run_1'"
        ).fetchone()["n"]
    # Bounded live log; history preserved verbatim in the archive.
    assert archived_rows == before, (archived_rows, before)
    assert live_rows <= 25


# --- protected nodes keep working across compaction ------------------------------ #


def test_completed_action_from_the_archived_prefix_does_not_re_run(db: str) -> None:
    """Exactly-once survives compaction: a claim settled before the anchor
    is still a cache hit afterwards, with the callback never re-invoked."""
    work(db, 1)
    run("--db", db, "compact", "run_1", "--force")
    calls: list[int] = []
    kind, value = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="process_doc",
        key="doc:1",
        fn=lambda: calls.append(1) or {"doc": 1},
    )
    assert kind is GuardKind.SKIP_DUPLICATE
    assert value == {"doc": 1}
    assert not calls, "an archived completed action must not re-run the effect"


def test_a_key_with_no_archived_claim_still_gets_a_fresh_slot(db: str) -> None:
    work(db, 1)
    run("--db", db, "compact", "run_1", "--force")
    kind, value = protected_call(
        SQLiteStorage(db),
        "run_1",
        action_type="process_doc",
        key="doc:99",
        fn=lambda: {"doc": 99},
    )
    assert kind is GuardKind.ALLOW and value == {"doc": 99}


def test_action_index_covers_the_archive_after_rebuild(tmp_path: Path) -> None:
    """The derived index must not forget archived claims (PR #260 review):
    after compaction it lags until rebuilt, then cross-run lookups see the
    archived completion again. A second run interleaves global insertion
    order, so the post-compaction fold provably differs from what the
    incremental index recorded."""
    path = str(tmp_path / "idx.db")
    with SQLiteStorage(path) as store:
        store.create_run_started(Run(run_id="r1", goal="one"))
        kind, _ = protected_call(
            store, "r1", action_type="process_doc", key="doc:1", fn=lambda: {"doc": 1}
        )
        assert kind is GuardKind.ALLOW
        store.create_run_started(Run(run_id="r2", goal="two"))
        store.append_event("r2", EventType.TASK_UPDATED, {"n": 1})

        store.compact_run("r1")
        assert store.action_index_drift() > 0
        store.rebuild_action_index()
        assert store.action_index_drift() == 0
        key = str(idempotency_key("process_doc", None, scope="r1", key="doc:1"))
        foreign = store.foreign_action(key, exclude_run="r2")
    assert foreign is not None
    assert foreign.status is ActionStatus.COMPLETED


# --- capability gate and projection hygiene -------------------------------------- #


def test_compact_refuses_an_engine_without_archive_support(tmp_path: Path) -> None:
    path = str(tmp_path / "c.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="r", goal="g"))
        # Instance override: pretend this engine maintains no archive table.
        store.supports_compaction = False
        out, err = io.StringIO(), io.StringIO()
        code = cmd_compact(argparse.Namespace(run_id="r", force=True, json=True), store, out, err)
    assert code == ExitCode.ERROR
    assert "does not support compaction" in err.getvalue()


def test_anchor_event_is_non_projecting(db: str) -> None:
    work(db, 1)
    with SQLiteStorage(db) as store:
        events = list(store.read_events("run_1"))
    anchor = Event(
        event_id="evt_anchor_test",
        run_id="run_1",
        sequence=len(events) + 1,
        type=EventType.EVENT_LOG_ANCHORED,
        timestamp=utcnow(),
        payload={},
        causer_event_id=None,
        source=Origin.DETERMINISTIC,
        prev_hash=None,
    ).sealed()
    _state, report = project_incremental("run_1", [*events, anchor])
    assert not report.ignored_types, (
        "EVENT_LOG_ANCHORED must be declared non-projecting, not silently ignored"
    )


def test_compact_run_rejects_through_sequence_that_would_eat_the_anchor(db: str) -> None:
    """Issue #705: through_sequence at or above the anchor's own sequence
    must be rejected. Accepting it archived and deleted the anchor (and with
    it the whole live log), so the next append minted a fresh genesis and
    forked the hash chain away from the archive."""
    for i in range(4):
        work(db, i)
    with SQLiteStorage(db) as store:
        pre_live = len(store.read_events("run_1"))

        with pytest.raises(ValueError, match="anchor"):
            store.compact_run("run_1", through_sequence=10_000)

        # The rejected call leaves a healthy, verifiable log behind: nothing
        # was archived, only the forced checkpoint marker was appended.
        report = store.verify_events("run_1")
        assert report.ok, [v.kind for v in report.violations]
        live = store.read_events("run_1")
        assert len(live) == pre_live + 1
        assert store.read_events("run_1")[0].sequence == 1, "live rows must not have moved"

        # A bounded value below the anchor still compacts normally.
        result = store.compact_run("run_1", through_sequence=1)
        assert result["archived"] >= 1
        assert any(e.type is EventType.EVENT_LOG_ANCHORED for e in store.read_events("run_1"))
        report = store.verify_events("run_1")
        assert report.ok, [v.kind for v in report.violations]
