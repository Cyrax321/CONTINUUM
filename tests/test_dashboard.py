import hashlib
import http.client
import os
import socket
import threading
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from continuum.dashboard import render_dashboard_html, render_run_detail_html
from continuum.dashboard.app import (
    DASHBOARD_DRAIN_LIMIT_BYTES,
    MAX_DASHBOARD_BODY,
    _advisory_trust_html,
    _liveness_html,
    _pins_html,
    make_dashboard_server,
    serve_dashboard,
)
from continuum.events import EventType
from continuum.models import ConstraintPinned, Run
from continuum.storage import SQLiteStorage


def recv_until_close(sock: socket.socket) -> bytes:
    chunks = []
    while chunk := sock.recv(4096):
        chunks.append(chunk)
    return b"".join(chunks)


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
        assert "transfer encoding is not supported" in data
        assert resp.getheader("Connection") == "close"

        # Duplicate Content-Length is ambiguous framing and is refused (#522)
        host, port = addr.split(":")
        request = (
            b"POST /action/confirm HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: 4\r\n"
            b"Content-Length: 9\r\n"
            b"\r\n"
            b"token=x"
        )
        with socket.create_connection((host, int(port)), timeout=10) as sock:
            sock.sendall(request)
            response = recv_until_close(sock)

        assert b"400 Bad Request" in response
        assert b"Connection: close" in response
        assert b"multiple Content-Length headers are not supported" in response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        os.environ.pop("CONTINUUM_DASHBOARD_TOKEN", None)


# --- advisory guards degrade to empty rather than crashing (#1924) --------------- #


class _ExplodingStorage:
    """Storage look-alike that delegates everything but the named methods.

    The advisory helpers take a Storage and must survive any read blowing up:
    the whole point of the guards is that a broken read costs the panel, not
    the page, so the tests drive failures straight through the interface.
    """

    def __init__(self, store: SQLiteStorage, *fail: str) -> None:
        self._store = store
        self._fail = set(fail)

    def __getattr__(self, name: str):
        if name in self._fail:
            raise RuntimeError(f"boom: {name}")
        return getattr(self._store, name)


def _pin_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _seed_run(store: SQLiteStorage, run_id: str = "run_1") -> None:
    store.create_run(Run(run_id=run_id, goal="g"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "g", "total": 5})


def test_advisory_trust_renders_from_a_projected_prefix() -> None:
    """No stored version yet: the advisory falls back to projecting the raw
    events and still renders the score."""
    storage = SQLiteStorage(":memory:")
    _seed_run(storage)
    assert "Advisory prefix trust:" in _advisory_trust_html(storage, "run_1")


def test_advisory_trust_prefers_the_latest_stored_version() -> None:
    """A checkpointed version exists: the advisory reads it instead of
    re-projecting, so a compacted run keeps its advisory too."""
    from continuum.checkpoint import CheckpointManager

    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        CheckpointManager(store).checkpoint("run_1")
        assert store.latest_version("run_1") is not None
        advisory = _advisory_trust_html(store, "run_1")
    assert "Advisory prefix trust:" in advisory


def test_advisory_trust_degrades_to_empty_when_the_log_cannot_fold() -> None:
    """A run whose events do not fold (RUN_STARTED with no goal) yields an empty
    advisory, not an exception: a poisoned log still renders."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {})
    assert _advisory_trust_html(storage, "run_1") == ""


def test_advisory_trust_degrades_to_empty_when_the_version_read_fails() -> None:
    """The outer guard: latest_version raising must cost the panel, not the page."""
    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        wrapper = _ExplodingStorage(store, "latest_version")
        assert _advisory_trust_html(wrapper, "run_1") == ""


def test_liveness_renders_for_a_live_run() -> None:
    storage = SQLiteStorage(":memory:")
    _seed_run(storage)
    assert "<b>Liveness:</b>" in _liveness_html(storage, "run_1")


def test_liveness_degrades_to_empty_when_the_advisory_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisory read swallows its own storage failures, so the panel's outer
    guard only fires when the computation itself blows up: the page must still
    render."""
    import continuum.recovery.health as health

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("advisory exploded")

    monkeypatch.setattr(health, "advisory_for_storage", boom)
    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        assert _liveness_html(store, "run_1") == ""


def test_pins_degrade_to_empty_without_pinned_constraints() -> None:
    """A run with no pins emits no panel at all."""
    storage = SQLiteStorage(":memory:")
    _seed_run(storage)
    assert _pins_html(storage, "run_1") == ""


def test_pins_renders_the_table_for_a_pinned_constraint() -> None:
    """A pinned constraint renders its id, a status, the sha256 prefix, and the
    flagged marker; a present pin is not flagged."""
    storage = SQLiteStorage(":memory:")
    _seed_run(storage)
    storage.append_event(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c1", sha256=_pin_sha("hello")).model_dump(),
    )
    panel = _pins_html(storage, "run_1")
    assert "Constraint pins:" in panel
    assert "flagged: none" in panel
    assert "<td>c1</td>" in panel
    assert "<td>present</td>" in panel
    assert f"<td>{_pin_sha('hello')[:8]}</td>" in panel
    assert "<td> flagged</td>" not in panel


def test_pins_prefers_the_latest_stored_version() -> None:
    """A checkpointed run reads its pins from the stored version instead of
    re-projecting the log."""
    from continuum.checkpoint import CheckpointManager

    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        store.append_event(
            "run_1",
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_pin_sha("hello")).model_dump(),
        )
        CheckpointManager(store).checkpoint("run_1")
        panel = _pins_html(store, "run_1")
    assert "<td>c1</td>" in panel


def test_pins_flagged_when_the_context_drops_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin whose marker is missing from the reconstructed context is flagged:
    the accounting is what the context actually contains, not a summarizer's
    claim, and the panel must say so."""
    import continuum.checkpoint.context as context_mod

    monkeypatch.setattr(
        context_mod,
        "build_recovery_context",
        lambda state: SimpleNamespace(render=lambda: "context without pin markers"),
    )
    storage = SQLiteStorage(":memory:")
    _seed_run(storage)
    storage.append_event(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c1", sha256=_pin_sha("hello")).model_dump(),
    )
    panel = _pins_html(storage, "run_1")
    assert "flagged: c1" in panel
    assert "<td>absent</td>" in panel
    assert "<td> flagged</td>" in panel


def test_pins_degrade_to_empty_when_projection_fails() -> None:
    """The projection-failure early return: an unfoldable log yields no panel."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {})
    assert _pins_html(storage, "run_1") == ""


def test_pins_degrade_to_empty_when_the_version_read_fails() -> None:
    """The outer guard: latest_version raising must cost the panel, not the page."""
    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        store.append_event(
            "run_1",
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_pin_sha("hello")).model_dump(),
        )
        wrapper = _ExplodingStorage(store, "latest_version")
        assert _pins_html(wrapper, "run_1") == ""


def test_pins_degrade_to_empty_when_the_payload_has_no_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accounting can come back with no pin rows even though the state has
    pins: the panel drops rather than rendering an empty table."""
    import continuum.state.semantic as semantic_mod

    storage = SQLiteStorage(":memory:")
    _seed_run(storage)
    storage.append_event(
        "run_1",
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="c1", sha256=_pin_sha("hello")).model_dump(),
    )
    monkeypatch.setattr(
        semantic_mod, "constraint_pins_payload", lambda *a, **k: {"pins": {}, "flagged": []}
    )
    assert _pins_html(storage, "run_1") == ""


# --- run detail fallbacks (#1924) ------------------------------------------------ #


def test_detail_renders_confirm_form_and_settle_rows_for_pending_action() -> None:
    """A run parked in request_human by an uncertain side effect gets the
    confirm form and one settle row per pending action."""
    from continuum.actions import ActionLedger

    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        ActionLedger(store, "run_1").claim("send_invoice", {}, key="invoice:I-1")
        page = render_run_detail_html(store, "run_1")
    assert "Human-in-the-loop" in page
    assert "Confirm goal + progress" in page
    assert "Settle: it DID happen" in page
    assert "Settle: it did NOT happen" in page
    assert "send_invoice" in page


def test_detail_renders_confirm_form_without_pending_actions() -> None:
    """A request_human with nothing to settle renders the confirm form alone:
    the pending-action loop is skipped, not the whole panel."""
    from continuum.models import Origin

    with SQLiteStorage(":memory:") as store:
        # An agent-started run carries a self-certified goal that only a human
        # confirmation clears, so the verdict is request_human with no action
        # pending settlement.
        store.create_run_started(Run(run_id="run_1", goal="g"), source=Origin.LLM)
        page = render_run_detail_html(store, "run_1")
    assert "Human-in-the-loop" in page
    assert "Confirm goal + progress" in page
    assert "Settle: it DID happen" not in page


def test_detail_tolerates_an_archived_read_failure() -> None:
    """read_archived_events raising degrades to 'no archived events' and the
    events table still renders."""
    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        wrapper = _ExplodingStorage(store, "read_archived_events")
        page = render_run_detail_html(wrapper, "run_1")
    assert "Recent events" in page
    assert "RUN_STARTED" in page


def test_detail_renders_settle_rows_even_when_the_verdict_engine_fails() -> None:
    """read_archived_events failing breaks the recovery assessment (decision is
    None, so there is no confirm form) but not the action ledger: the pending
    side effect still gets its settle buttons."""
    from continuum.actions import ActionLedger

    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        ActionLedger(store, "run_1").claim("send_invoice", {}, key="invoice:I-1")
        wrapper = _ExplodingStorage(store, "read_archived_events")
        page = render_run_detail_html(wrapper, "run_1")
    assert "Human-in-the-loop" in page
    assert "Confirm goal + progress" not in page
    assert "Settle: it DID happen" in page
    assert "Settle: it did NOT happen" in page


def test_detail_renders_hitl_unavailable_when_the_ledger_read_fails() -> None:
    """A failing ledger read replaces the HITL controls with an explicit
    '[hitl unavailable: ...]' note instead of taking the page down."""
    with SQLiteStorage(":memory:") as store:
        _seed_run(store)
        wrapper = _ExplodingStorage(store, "read_all_events")
        page = render_run_detail_html(wrapper, "run_1")
    assert "[hitl unavailable:" in page
    assert "Recent events" in page


# --- serve_dashboard lifecycle (#1924) ------------------------------------------- #


def test_serve_dashboard_serves_then_closes_its_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve_dashboard blocks and closes the socket on the way out: the finally
    is the only thing standing between a stopped server and a leaked listener."""
    import time

    captured: dict[str, object] = {}
    real_server_factory = make_dashboard_server

    def recording_factory(storage: SQLiteStorage, port: int = 8000, host: str = "127.0.0.1"):
        server = real_server_factory(storage, port=port, host=host)
        captured["server"] = server
        return server

    monkeypatch.setattr("continuum.dashboard.app.make_dashboard_server", recording_factory)
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    thread = threading.Thread(
        target=serve_dashboard, args=(storage,), kwargs={"port": 0}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while "server" not in captured and time.monotonic() < deadline:
            thread.join(timeout=0.05)
        server = captured.get("server")
        assert server is not None, "serve_dashboard never built its server"
        addr = f"127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        assert resp.status == 200
        assert "CONTINUUM Dashboard" in body
    finally:
        server = captured.get("server")
        if server is not None:
            server.shutdown()  # type: ignore[attr-defined]
        thread.join(timeout=10)
        assert not thread.is_alive(), "serve_dashboard did not return after shutdown"

    # server_close ran in the finally, so the listening socket is gone.
    assert captured["server"].socket.fileno() == -1  # type: ignore[attr-defined]


# --- run detail routing (#1924) -------------------------------------------------- #


def test_get_serves_run_detail_and_404s_for_an_unknown_run(tmp_path: Path) -> None:
    """A known run answers 200 with its detail page; an unknown run answers 404
    because a typo'd name must never look like a clean bill of health."""
    db = str(tmp_path / "runs.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

    server = make_dashboard_server(SQLiteStorage(db), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{server.server_address[1]}"
    try:
        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request("GET", "/runs/run_1")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        assert resp.status == 200
        assert "Run run_1" in body

        conn = http.client.HTTPConnection(addr, timeout=10)
        conn.request("GET", "/runs/ghost")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        assert resp.status == 404
        assert "Run not found" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
