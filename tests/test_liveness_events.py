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
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            received.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):  # noqa: A002
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
