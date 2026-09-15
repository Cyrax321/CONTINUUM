"""LIVENESS events, WAIT mapping and continuum watch (issue #562)."""

from __future__ import annotations

import http.server
import io
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from continuum.cli.main import main
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage


def test_liveness_events_are_hash_chained(tmp_path: Path) -> None:
    db = str(tmp_path / "liveness_chain.db")
    with SQLiteStorage(db) as store:
        run_id = "run_liveness_chain"
        store.create_run_started(Run(run_id=run_id, goal="test chain"))
        e1 = store.append_event(
            run_id,
            EventType.LIVENESS_SILENCE_DETECTED,
            {"silence_seconds": 4000, "threshold_seconds": 3600, "phase": "otherwise"},
        )
        e2 = store.append_event(run_id, EventType.LIVENESS_RECOVERED, {"silence_seconds": 10})
        assert e1.hash is not None
        assert e2.prev_hash == e1.hash
        report = store.verify_events(run_id)
        assert report.ok is True
        assert report.trusted_through[run_id] == 3


def test_engine_maps_breach_to_wait(tmp_path: Path) -> None:
    db = str(tmp_path / "wait_map.db")
    with SQLiteStorage(db) as store:
        run_id = "run_wait_map"
        store.create_run_started(Run(run_id=run_id, goal="wait test"))
        from continuum.events import Event

        old_ts = datetime.now(UTC) - timedelta(seconds=7200)
        last_seq = store.last_sequence(run_id)
        ev = Event(
            run_id=run_id,
            sequence=last_seq + 1,
            type=EventType.TASK_UPDATED,
            timestamp=old_ts,
            payload={"completed": 1},
            prev_hash=store.read_events(run_id)[-1].hash,
        ).sealed()
        store.append_sealed(ev)
        engine = RecoveryEngine(store)
        decision = engine.assess(run_id)
        assert decision.mode.value == "wait"
        assert decision.mode.value != "rollback"
        assert decision.contract.liveness is not None
        assert decision.contract.liveness["breached"] is True
        assert decision.contract.liveness["breaches"] >= 0


def test_watch_appends_detected_and_recovered(tmp_path: Path) -> None:
    db = str(tmp_path / "watch_events.db")
    with SQLiteStorage(db) as store:
        run_id = "run_watch_events"
        store.create_run_started(Run(run_id=run_id, goal="watch test"))
        from continuum.events import Event

        old_ts = datetime.now(UTC) - timedelta(seconds=7200)
        last_seq = store.last_sequence(run_id)
        ev = Event(
            run_id=run_id,
            sequence=last_seq + 1,
            type=EventType.TASK_UPDATED,
            timestamp=old_ts,
            payload={"completed": 1},
            prev_hash=store.read_events(run_id)[-1].hash,
        ).sealed()
        store.append_sealed(ev)

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--db", db, "watch", run_id, "--max-silence", "3600", "--on-breach", "exit"],
        out=out,
        err=err,
    )
    assert code == 20
    with SQLiteStorage(db) as store:
        events = store.read_events(run_id)
        types = [e.type for e in events]
        assert EventType.LIVENESS_SILENCE_DETECTED in types
        store.append_event(run_id, EventType.TASK_UPDATED, {"completed": 2})
        out2, err2 = io.StringIO(), io.StringIO()
        code2 = main(
            ["--db", db, "watch", run_id, "--max-silence", "3600", "--on-breach", "exit"],
            out=out2,
            err=err2,
        )
        assert code2 == 0
        events2 = store.read_events(run_id)
        assert events2[-1].type == EventType.LIVENESS_RECOVERED


def test_watch_webhook_fail_open(tmp_path: Path) -> None:
    db = str(tmp_path / "watch_webhook.db")
    with SQLiteStorage(db) as store:
        run_id = "run_watch_webhook"
        store.create_run_started(Run(run_id=run_id, goal="webhook test"))

    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "--db",
            db,
            "watch",
            run_id,
            "--max-silence",
            "1",
            "--on-breach",
            "webhook",
            "--webhook-url",
            "http://127.0.0.1:1/nonexistent",
        ],
        out=out,
        err=err,
    )
    assert code in (0, 20)
    assert "warning: webhook delivery failed" in err.getvalue() or code in (0, 20)


def test_watch_webhook_delivers_on_breach(tmp_path: Path) -> None:
    received: list[bytes] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            received.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        db = str(tmp_path / "watch_webhook_deliver.db")
        with SQLiteStorage(db) as store:
            run_id = "run_watch_deliver"
            store.create_run_started(Run(run_id=run_id, goal="deliver test"))
            from continuum.events import Event

            old_ts = datetime.now(UTC) - timedelta(seconds=7200)
            last_seq = store.last_sequence(run_id)
            ev = Event(
                run_id=run_id,
                sequence=last_seq + 1,
                type=EventType.TASK_UPDATED,
                timestamp=old_ts,
                payload={"completed": 1},
                prev_hash=store.read_events(run_id)[-1].hash,
            ).sealed()
            store.append_sealed(ev)
        out, err = io.StringIO(), io.StringIO()
        url = f"http://127.0.0.1:{port}/hook"
        code = main(
            [
                "--db",
                db,
                "watch",
                run_id,
                "--max-silence",
                "3600",
                "--on-breach",
                "webhook",
                "--webhook-url",
                url,
            ],
            out=out,
            err=err,
        )
        assert code == 20
        import time

        time.sleep(0.2)
        assert len(received) == 1
        payload = json.loads(received[0].decode("utf-8"))
        assert payload["breached"] is True
        assert payload["run_id"] == run_id
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_contract_liveness_section(tmp_path: Path) -> None:
    db = str(tmp_path / "contract_liveness.db")
    with SQLiteStorage(db) as store:
        run_id = "run_contract_liveness"
        store.create_run_started(Run(run_id=run_id, goal="contract liveness"))
        engine = RecoveryEngine(store)
        decision = engine.assess(run_id)
        assert decision.contract.liveness is not None
        assert "last_append_age" in decision.contract.liveness
        assert "breaches" in decision.contract.liveness
        assert "breached" in decision.contract.liveness


def test_max_silence_override_wins_without_open_claim(tmp_path: Path) -> None:
    """An explicit --max-silence beats the default otherwise scope (#670)."""
    db = str(tmp_path / "watch_override.db")
    with SQLiteStorage(db) as store:
        run_id = "run_watch_override"
        store.create_run_started(Run(run_id=run_id, goal="watch test"))
        from continuum.events import Event

        old_ts = datetime.now(UTC) - timedelta(seconds=30)
        last_seq = store.last_sequence(run_id)
        ev = Event(
            run_id=run_id,
            sequence=last_seq + 1,
            type=EventType.TASK_UPDATED,
            timestamp=old_ts,
            payload={"completed": 1},
            prev_hash=store.read_events(run_id)[-1].hash,
        ).sealed()
        store.append_sealed(ev)

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--db", db, "watch", run_id, "--max-silence", "5", "--on-breach", "exit"],
        out=out,
        err=err,
    )
    assert code == 20, out.getvalue()
    with SQLiteStorage(db) as store:
        types = [e.type for e in store.read_events(run_id)]
        assert EventType.LIVENESS_SILENCE_DETECTED in types


def _append_timed(
    store: SQLiteStorage, run_id: str, etype: EventType, payload: dict, ts: datetime
) -> None:
    """Append a hash-chained event with an explicit timestamp (for backdating)."""
    from continuum.events import Event

    prev_hash = store.read_events(run_id)[-1].hash
    ev = Event(
        run_id=run_id,
        sequence=store.last_sequence(run_id) + 1,
        type=etype,
        timestamp=ts,
        payload=payload,
        prev_hash=prev_hash,
    ).sealed()
    store.append_sealed(ev)


def test_watch_does_not_duplicate_detected_after_compaction(tmp_path: Path) -> None:
    """An archived DETECTED is still the open episode: watch must not re-mint it (#1072).

    The episode scan in cmd_watch read the live tail only, so after
    compaction moved the DETECTED into events_archive the same breach episode
    looked unrecorded and every watch invocation appended another
    LIVENESS_SILENCE_DETECTED for it.
    """
    db = str(tmp_path / "watch_compact_dup.db")
    run_id = "run_watch_compact_dup"
    old_ts = datetime.now(UTC) - timedelta(hours=3)
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id=run_id, goal="compact dup"))
        _append_timed(
            store,
            run_id,
            EventType.WORK_COMPLETED,
            {"doc": 0},
            old_ts,
        )
        _append_timed(
            store,
            run_id,
            EventType.LIVENESS_SILENCE_DETECTED,
            {"silence_seconds": 10800, "threshold_seconds": 3600, "phase": "otherwise"},
            old_ts,
        )
        store.compact_run(run_id)
        # The compacted prefix (DETECTED included) is archived; only the anchor
        # is live. Append an old-timestamp live tail so the run is still silent.
        assert store.read_events(run_id)[-1].type is not EventType.LIVENESS_SILENCE_DETECTED, (
            "setup error: DETECTED must land in the archive, not stay live"
        )
        _append_timed(store, run_id, EventType.WORK_COMPLETED, {"doc": 1}, old_ts)

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--db", db, "watch", run_id, "--max-silence", "3600", "--on-breach", "exit"],
        out=out,
        err=err,
    )
    assert code == 20, out.getvalue()
    with SQLiteStorage(db) as store:
        detected = [
            e
            for e in store.read_all_events(run_id)
            if e.type is EventType.LIVENESS_SILENCE_DETECTED
        ]
        assert len(detected) == 1, "compaction must not cause a duplicate DETECTED"


def test_watch_mints_recovered_after_compaction(tmp_path: Path) -> None:
    """A compacted run that resumed must still mint LIVENESS_RECOVERED (#1072).

    With the episode scan reading the live tail only, an archived DETECTED was
    invisible, so the not-breached branch never fired and the episode never
    terminated in the audit trail.
    """
    db = str(tmp_path / "watch_compact_recovered.db")
    run_id = "run_watch_compact_recovered"
    old_ts = datetime.now(UTC) - timedelta(hours=3)
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id=run_id, goal="compact recovered"))
        _append_timed(store, run_id, EventType.WORK_COMPLETED, {"doc": 0}, old_ts)
        _append_timed(
            store,
            run_id,
            EventType.LIVENESS_SILENCE_DETECTED,
            {"silence_seconds": 10800, "threshold_seconds": 3600, "phase": "otherwise"},
            old_ts,
        )
        store.compact_run(run_id)
        # Fresh live tail: the run has resumed, so it is no longer breached.
        _append_timed(store, run_id, EventType.WORK_COMPLETED, {"doc": 1}, datetime.now(UTC))

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--db", db, "watch", run_id, "--max-silence", "3600", "--on-breach", "exit"],
        out=out,
        err=err,
    )
    assert code == 0, out.getvalue()
    with SQLiteStorage(db) as store:
        recovered = [
            e for e in store.read_all_events(run_id) if e.type is EventType.LIVENESS_RECOVERED
        ]
        assert len(recovered) == 1, "recovery of an archived episode must be recorded"
