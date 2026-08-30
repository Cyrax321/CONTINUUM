import http.client
import os
import threading
import urllib.parse
from pathlib import Path

from continuum.dashboard import render_dashboard_html, render_run_detail_html
from continuum.dashboard.app import (
    DASHBOARD_DRAIN_LIMIT_BYTES,
    MAX_DASHBOARD_BODY,
    make_dashboard_server,
)
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_dashboard_renders_runs() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    html = render_dashboard_html(storage)
    assert "run_1" in html
    assert "CONTINUUM Dashboard" in html


def test_dashboard_empty_runs() -> None:
    storage = SQLiteStorage(":memory:")
    html = render_dashboard_html(storage)
    assert "No runs" in html


def test_dashboard_run_detail_shows_ledger_and_contract() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    html = render_run_detail_html(storage, "run_1")
    assert "run_1" in html
    assert "Contract" in html
    assert "Validation" in html


def test_dashboard_pagination_hint_shows_for_many_events() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    for i in range(30):
        storage.append_event("run_1", EventType.TOOL_CALLED, {"i": i})
    html = render_run_detail_html(storage, "run_1")
    assert "Showing last 20 of 30 events" in html
    assert "continuum events run_1" in html
    assert "for full log" in html


def test_dashboard_no_pagination_hint_for_few_events() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    for i in range(5):
        storage.append_event("run_1", EventType.TOOL_CALLED, {"i": i})
    html = render_run_detail_html(storage, "run_1")
    assert "Showing last 20 of" not in html


def test_dashboard_no_pagination_hint_for_exactly_twenty() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    for i in range(20):
        storage.append_event("run_1", EventType.TOOL_CALLED, {"i": i})
    html = render_run_detail_html(storage, "run_1")
    assert "Showing last 20 of" not in html


def test_dashboard_pagination_hint_includes_archived_after_compaction() -> None:
    """After compaction the hint must count archived events (Refs #520).

    Before the fix total = len(live) so a run with 20 live but 80 archived
    showed no hint even though the full log is 100. The fix counts both.
    """
    import html as html_lib

    # Use html-sensitive run_id to verify escaping in the hint.
    run_id = "run<1>&test"
    storage = SQLiteStorage(":memory:")
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    # 98 total before compact: 1 RUN_STARTED + 97 TOOL_CALLED
    for i in range(97):
        storage.append_event(run_id, EventType.TOOL_CALLED, {"i": i})
    assert len(storage.read_events(run_id)) == 98
    assert len(storage.read_archived_events(run_id)) == 0
    # Compact so 80 archived, 20 live (18 tail + 2 anchor/checkpoint), 100 total.
    storage.compact_run(run_id, through_sequence=80)
    live = len(storage.read_events(run_id))
    archived = len(storage.read_archived_events(run_id))
    assert archived == 80
    assert live == 20
    assert live + archived == 100
    html = render_run_detail_html(storage, run_id)
    # Hint must show combined total, not just live (20 would be no hint).
    assert "Showing last 20 of 100 events" in html
    # HTML-escaped: run_id in hint is escaped, raw <>& must not appear.
    assert html_lib.escape(run_id) in html
    assert f"continuum events {html_lib.escape(run_id)} for full log" in html
    # Old logic would have computed total = live = 20 and shown no hint.
    assert live == 20


def test_dashboard_no_hint_for_fifteen_events_no_compact() -> None:
    """15 events with no anchor must not show the hint (Refs #520)."""
    storage = SQLiteStorage(":memory:")
    storage.create_run_started(Run(run_id="run_15", goal="g"))
    for i in range(14):
        storage.append_event("run_15", EventType.TOOL_CALLED, {"i": i})
    assert len(storage.read_events("run_15")) == 15
    assert len(storage.read_archived_events("run_15")) == 0
    html = render_run_detail_html(storage, "run_15")
    assert "Showing last 20 of" not in html


def test_dashboard_no_hint_for_twenty_exact_no_compact() -> None:
    """Exactly 20 combined events must not show the hint (Refs #520)."""
    storage = SQLiteStorage(":memory:")
    storage.create_run_started(Run(run_id="run_20", goal="g"))
    for i in range(19):
        storage.append_event("run_20", EventType.TOOL_CALLED, {"i": i})
    assert len(storage.read_events("run_20")) == 20
    html = render_run_detail_html(storage, "run_20")
    assert "Showing last 20 of" not in html


def test_dashboard_hint_for_twenty_one_with_one_archived() -> None:
    """21 combined (20 live + 1 archived) must show hint for 21 (Refs #520)."""
    run_id = "run_21"
    storage = SQLiteStorage(":memory:")
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    for i in range(18):
        storage.append_event(run_id, EventType.TOOL_CALLED, {"i": i})
    assert len(storage.read_events(run_id)) == 19
    # Compact 1 archived, leaving 20 live (18 tail + 2 system) + 1 archived = 21 total.
    storage.compact_run(run_id, through_sequence=1)
    live = len(storage.read_events(run_id))
    archived = len(storage.read_archived_events(run_id))
    assert archived == 1
    assert live == 20
    assert live + archived == 21
    html = render_run_detail_html(storage, run_id)
    assert "Showing last 20 of 21 events" in html
    assert f"continuum events {run_id} for full log" in html


def test_dashboard_post_body_too_large_returns_413(tmp_path: Path) -> None:
    """Large POST bodies are refused with 413, matching gateway pattern (#317)."""
    # token needed for the small-body control, but the oversize check happens before auth
    os.environ["CONTINUUM_DASHBOARD_TOKEN"] = "op-secret"
    db = str(tmp_path / "dash.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

    server = make_dashboard_server(SQLiteStorage(db), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.server_address[1]}"
    try:
        # Oversize: 1 MB + 1, like the repro in #317 (6 MB would also work but is slower).
        huge = "x" * (MAX_DASHBOARD_BODY + 1)
        body = urllib.parse.urlencode(
            {"run_id": "run_1", "token": "op-secret", "blob": huge}
        ).encode()
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST", "/action/reconcile", body=body, headers={"Content-Length": str(len(body))}
        )
        resp = conn.getresponse()
        data = resp.read().decode(errors="ignore")
        conn.close()
        assert resp.status == 413
        assert "Body too large" in data
        assert str(MAX_DASHBOARD_BODY) in data

        # Drain limit is the same bound the gateway uses, so behaviour is consistent.
        from continuum.gateway import DRAIN_LIMIT_BYTES

        assert DASHBOARD_DRAIN_LIMIT_BYTES == DRAIN_LIMIT_BYTES

        # Small body still succeeds (control): a valid confirm with a tiny payload.
        small_body = urllib.parse.urlencode({"run_id": "run_1", "token": "op-secret"}).encode()
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/action/confirm",
            body=small_body,
            headers={"Content-Length": str(len(small_body))},
        )
        resp = conn.getresponse()
        data = resp.read().decode(errors="ignore")
        conn.close()
        assert resp.status == 200
        assert "goal and progress confirmed" in data

        # Malformed Content-Length returns 400 without crashing (#522)
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/action/confirm",
            body=b"x",
            headers={"Content-Length": "abc"},
        )
        resp = conn.getresponse()
        data = resp.read().decode(errors="ignore")
        conn.close()
        assert resp.status == 400
        assert "400 Bad Request" in data

        # Chunked Transfer-Encoding returns 400 (#522)
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request(
            "POST",
            "/action/confirm",
            body=b"1\r\nx\r\n0\r\n\r\n",
            headers={"Transfer-Encoding": "chunked"},
        )
        resp = conn.getresponse()
        data = resp.read().decode(errors="ignore")
        conn.close()
        assert resp.status == 400
        assert "chunked transfer encoding is not supported" in data
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        os.environ.pop("CONTINUUM_DASHBOARD_TOKEN", None)
