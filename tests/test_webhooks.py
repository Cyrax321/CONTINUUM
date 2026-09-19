"""Registry, dedup, dead-letter, and CLI wiring for webhook-out (issue #305).

The delivery primitive (signing, POST, fail-open) is covered by
``test_notify.py``. These tests cover everything built on top of it: the
``webhooks.json`` registry, the per-transition dedup that keeps a polling
cron from spamming, the dead-letter row for undelivered notifications, and
the two CLI surfaces (``resume`` wiring, ``notify-test``).
"""

from __future__ import annotations

import http.server
import io
import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.cli import ExitCode, main
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import RecoveryContract, RecoverySafety, Run, utcnow
from continuum.recovery.notify import SIGNATURE_HEADER, verify_signature
from continuum.recovery.webhooks import (
    EVENT_REQUEST_HUMAN,
    EVENT_REQUIRES_REVIEW,
    WebhookConfigError,
    WebhookEndpoint,
    WebhookRegistry,
    load_webhook_registry,
    notify_blocked,
    verdict_key,
)
from continuum.storage import SQLiteStorage


def _receiver(captured: dict, status: int = 200) -> http.server.HTTPServer:
    """A local HTTP sink recording every POST's body and signature."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            captured.setdefault("hits", []).append(
                {"body": body, "signature": self.headers.get(SIGNATURE_HEADER)}
            )
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    with SQLiteStorage(":memory:") as storage:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        yield storage


def _contract(checkpoint_version: int = 0) -> RecoveryContract:
    return RecoveryContract(
        run_id="run_1",
        checkpoint_version=checkpoint_version,
        recovery_status=RecoverySafety.REQUIRES_HUMAN,
    )


def _events(storage: SQLiteStorage, kind: EventType) -> list:
    return [e for e in storage.read_events("run_1") if e.type is kind]


# --- the registry ----------------------------------------------------------- #


def test_registry_absent_file_is_empty_and_silent(tmp_path: Path) -> None:
    registry = load_webhook_registry(tmp_path / "webhooks.json")
    assert registry == WebhookRegistry()
    assert registry.for_event(EVENT_REQUEST_HUMAN) == []


def test_registry_parses_endpoints_and_defaults(tmp_path: Path) -> None:
    path = tmp_path / "webhooks.json"
    path.write_text(
        json.dumps(
            {
                "dashboard_base_url": "http://localhost:8765",
                "endpoints": [
                    {"url": "https://hooks.example.com/x"},
                    {
                        "url": "https://hooks.example.com/y",
                        "secret": "s",
                        "events": ["request_human", "requires_review"],
                        "re_notify_seconds": 60,
                        "retries": 0,
                        "timeout": 2.5,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = load_webhook_registry(path)
    assert registry.dashboard_base_url == "http://localhost:8765"
    first, second = registry.endpoints
    # Defaults: request_human only, one hour window, two retries.
    assert first.events == frozenset({EVENT_REQUEST_HUMAN})
    assert first.re_notify_seconds == 3600 and first.retries == 2
    assert second.secret == "s" and second.retries == 0 and second.timeout == 2.5
    assert registry.for_event(EVENT_REQUIRES_REVIEW) == [second]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"endpoints": {}}',
        '{"endpoints": [{"url": "ftp://nope"}]}',
        '{"endpoints": [{"url": "https://ok", "events": ["request_hooman"]}]}',
        '{"endpoints": [{"url": "https://ok", "events": []}]}',
        '{"endpoints": [{"url": "https://ok", "re_notify_seconds": -1}]}',
        '{"endpoints": [{"url": "https://ok", "retries": "many"]}]}',
        '{"endpoints": [{"url": "https://ok", "timeout": 0}]}',
        '{"dashboard_base_url": "not-a-url", "endpoints": []}',
    ],
)
def test_registry_refuses_what_it_cannot_honour(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "webhooks.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(WebhookConfigError) as excinfo:
        load_webhook_registry(path)
    # The message names the resolved file the operator must open (#333).
    assert str(path.resolve()) in str(excinfo.value)


# --- delivery, dedup, dead-letter ------------------------------------------- #


def test_verdict_key_ignores_per_assessment_bookkeeping() -> None:
    """Every resume builds a fresh contract; the key must track the blockage.

    ``created_at`` is regenerated per assessment and ``liveness`` /
    ``post_checkpoint_observations`` change as the run idles - none of them
    change the decision. Hashing them would re-ring the bell on every cron
    tick; this is the regression test for exactly that.
    """
    first = _contract()
    later_assessment = _contract().model_copy(
        update={
            "created_at": utcnow() + timedelta(seconds=5),
            "liveness": {"last_append_age_seconds": 61, "breaches": 0},
            "post_checkpoint_observations": [{"path": "out.txt", "change": "modified"}],
        }
    )
    assert verdict_key(EVENT_REQUEST_HUMAN, first) == verdict_key(
        EVENT_REQUEST_HUMAN, later_assessment
    )
    # A genuinely different decision is a different verdict.
    assert verdict_key(EVENT_REQUEST_HUMAN, first) != verdict_key(
        EVENT_REQUEST_HUMAN, _contract(checkpoint_version=7)
    )


def test_blocked_run_posts_payload_with_event_and_deep_link(store: SQLiteStorage) -> None:
    captured: dict = {}
    server = _receiver(captured)
    try:
        registry = WebhookRegistry(
            endpoints=(
                WebhookEndpoint(url=f"http://127.0.0.1:{server.server_port}/hook", secret="s"),
            ),
            dashboard_base_url="http://localhost:8765",
        )
        records = notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload={"run_id": "run_1", "mode": "request_human", "safe": False},
            contract=_contract(),
            registry=registry,
        )
        assert [r.status for r in records] == ["sent"]
        hit = captured["hits"][0]
        body = json.loads(hit["body"])
        assert body["event"] == "request_human"
        assert body["dashboard_url"] == "http://localhost:8765/runs/run_1"
        # The signature is over the exact wire bytes and verifies.
        assert verify_signature(hit["body"], "s", hit["signature"])
        assert len(_events(store, EventType.NOTIFICATION_SENT)) == 1
    finally:
        server.shutdown()


def test_same_verdict_within_window_is_skipped_not_resent(store: SQLiteStorage) -> None:
    captured: dict = {}
    server = _receiver(captured)
    try:
        endpoint = WebhookEndpoint(url=f"http://127.0.0.1:{server.server_port}/hook")
        registry = WebhookRegistry(endpoints=(endpoint,))
        payload = {"run_id": "run_1", "mode": "request_human", "safe": False}
        for _ in range(3):  # a cron re-running resume every minute
            notify_blocked(
                store,
                "run_1",
                mode=EVENT_REQUEST_HUMAN,
                payload=payload,
                contract=_contract(),
                registry=registry,
            )
        assert len(captured["hits"]) == 1, "dedup must collapse re-assessments"
        records = notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(),
            registry=registry,
        )
        assert [r.status for r in records] == ["skipped"]
        # And a skipped round writes no new event rows.
        assert len(_events(store, EventType.NOTIFICATION_SENT)) == 1
    finally:
        server.shutdown()


def test_new_verdict_and_expired_window_ring_again(store: SQLiteStorage) -> None:
    captured: dict = {}
    server = _receiver(captured)
    try:
        endpoint = WebhookEndpoint(
            url=f"http://127.0.0.1:{server.server_port}/hook", re_notify_seconds=60
        )
        registry = WebhookRegistry(endpoints=(endpoint,))
        payload = {"run_id": "run_1", "mode": "request_human", "safe": False}
        notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(),
            registry=registry,
        )
        # A different contract is a new blockage: ring immediately.
        notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(checkpoint_version=7),
            registry=registry,
        )
        assert len(captured["hits"]) == 2
        # The same old verdict, but past the window: ring again.
        notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(),
            registry=registry,
            now=utcnow() + timedelta(seconds=61),
        )
        assert len(captured["hits"]) == 3
    finally:
        server.shutdown()


def test_dedup_survives_compaction_of_the_notification_row(
    store: SQLiteStorage,
) -> None:
    """A compaction inside the window must not re-ring the bell (issue #1186).

    The dedup state lives in the event log itself, and compacting a long
    blocked run is exactly what the docs prescribe for it. The scan has to
    walk the archived prefix too, or an operator who compacted the run gets
    paged again for the same standing blockage, and again on every
    subsequent assessment until the window expires.
    """
    captured: dict = {}
    server = _receiver(captured)
    try:
        endpoint = WebhookEndpoint(
            url=f"http://127.0.0.1:{server.server_port}/hook", re_notify_seconds=3600
        )
        registry = WebhookRegistry(endpoints=(endpoint,))
        payload = {"run_id": "run_1", "mode": "request_human", "safe": False}
        notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(),
            registry=registry,
        )
        assert len(captured["hits"]) == 1

        # The operator compacts the run to shrink the log. The SENT row leaves
        # the live tail verbatim, timestamp intact.
        store.compact_run("run_1")
        assert _events(store, EventType.NOTIFICATION_SENT) == [], "row is archived now"
        assert len(store.read_all_events("run_1")) > len(store.read_events("run_1"))

        records = notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(),
            registry=registry,
            now=utcnow() + timedelta(minutes=20),
        )
        assert [r.status for r in records] == ["skipped"]
        assert len(captured["hits"]) == 1, "the archived SENT row still holds the window"
        # A skipped round still writes nothing.
        assert len(_events(store, EventType.NOTIFICATION_SENT)) == 0

        # The archived timestamp still governs the window: past it, ring again.
        notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload=payload,
            contract=_contract(),
            registry=registry,
            now=utcnow() + timedelta(hours=2),
        )
        assert len(captured["hits"]) == 2
    finally:
        server.shutdown()


def test_event_filter_subscribes_per_endpoint(store: SQLiteStorage) -> None:
    captured: dict = {}
    server = _receiver(captured)
    try:
        registry = WebhookRegistry(
            endpoints=(WebhookEndpoint(url=f"http://127.0.0.1:{server.server_port}/hook"),)
        )
        # The default endpoint subscribes to request_human, not requires_review.
        records = notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUIRES_REVIEW,
            payload={},
            contract=_contract(),
            registry=registry,
        )
        assert records == []
        assert "hits" not in captured
        assert _events(store, EventType.NOTIFICATION_SENT) == []
    finally:
        server.shutdown()


def test_undeliverable_notification_dead_letters_without_raising(store: SQLiteStorage) -> None:
    captured: dict = {}
    server = _receiver(captured, status=500)
    try:
        endpoint = WebhookEndpoint(url=f"http://127.0.0.1:{server.server_port}/hook", retries=1)
        records = notify_blocked(
            store,
            "run_1",
            mode=EVENT_REQUEST_HUMAN,
            payload={},
            contract=_contract(),
            registry=WebhookRegistry(endpoints=(endpoint,)),
        )
        assert [r.status for r in records] == ["failed"]
        assert "2 attempt(s)" in records[0].detail
        # retries=1 means first try plus one retry.
        assert len(captured["hits"]) == 2
        dead = _events(store, EventType.NOTIFICATION_FAILED)
        assert len(dead) == 1
        assert dead[0].payload["url"] == endpoint.url
        assert _events(store, EventType.NOTIFICATION_SENT) == []
    finally:
        server.shutdown()


def test_failed_delivery_also_suppresses_the_window(store: SQLiteStorage) -> None:
    """A dead receiver must not be re-hit every minute by the dedup miss."""
    captured: dict = {}
    server = _receiver(captured, status=500)
    try:
        endpoint = WebhookEndpoint(url=f"http://127.0.0.1:{server.server_port}/hook")
        registry = WebhookRegistry(endpoints=(endpoint,))
        for _ in range(2):
            notify_blocked(
                store,
                "run_1",
                mode=EVENT_REQUEST_HUMAN,
                payload={},
                contract=_contract(),
                registry=registry,
            )
        assert len(captured["hits"]) == endpoint.retries + 1
        assert len(_events(store, EventType.NOTIFICATION_FAILED)) == 1
    finally:
        server.shutdown()


# --- CLI: notify-test ------------------------------------------------------- #


def _run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _write_registry(root: Path, url: str, *, secret: str | None = None) -> Path:
    (root / ".continuum").mkdir(exist_ok=True)
    path = root / ".continuum" / "webhooks.json"
    entry = {"url": url}
    if secret:
        entry["secret"] = secret
    path.write_text(json.dumps({"endpoints": [entry]}), encoding="utf-8")
    return path


def test_notify_test_without_config_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = _run_cli("notify-test")
    assert code == ExitCode.ERROR
    assert "no endpoints registered" in err


def test_notify_test_probes_every_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    server = _receiver(captured)
    try:
        _write_registry(tmp_path, f"http://127.0.0.1:{server.server_port}/hook", secret="s")
        code, out, err = _run_cli("notify-test", "run_1")
        assert code == ExitCode.OK, err
        assert "delivered" in out
        hit = captured["hits"][0]
        assert json.loads(hit["body"])["event"] == "notify-test"
        assert verify_signature(hit["body"], "s", hit["signature"])
    finally:
        server.shutdown()


def test_notify_test_exits_nonzero_when_a_receiver_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    server = _receiver(captured, status=500)
    try:
        _write_registry(tmp_path, f"http://127.0.0.1:{server.server_port}/hook")
        code, _, err = _run_cli("notify-test")
        assert code == ExitCode.ERROR
        assert "failed" in err
    finally:
        server.shutdown()


def test_notify_test_does_not_create_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wiring probe must not leave an empty .continuum database behind."""
    monkeypatch.chdir(tmp_path)
    _write_registry(tmp_path, "http://127.0.0.1:1/unreachable")
    _run_cli("notify-test")
    assert not (tmp_path / "continuum.db").exists()
    assert list(tmp_path.glob("*.db")) == []


# --- CLI: resume wiring ------------------------------------------------------ #


def _seed_blocked_run(db: str) -> None:
    """A run whose resume verdict is request_human (in-flight side effect)."""
    with SQLiteStorage(db) as storage:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
        )
        storage.append_event(
            "run_1",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": "ev_a", "summary": "s", "source": "dataset"},
        )
        CheckpointManager(storage).checkpoint(
            "run_1", environment=capture("run_1", StaticProvider(dataset="v3"))
        )
        from continuum.actions import ActionLedger

        ActionLedger(storage, "run_1").claim("github.create_issue", {"title": "Anomaly"})


def test_resume_notifies_on_request_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    server = _receiver(captured)
    try:
        db = str(tmp_path / "demo.db")
        _seed_blocked_run(db)
        _write_registry(tmp_path, f"http://127.0.0.1:{server.server_port}/hook")

        code, _, err = _run_cli("--db", db, "resume", "run_1", "--env", "dataset=v3")
        assert code == ExitCode.REQUIRES_HUMAN
        assert "notification sent" in err
        body = json.loads(captured["hits"][0]["body"])
        # The payload mirrors the resume JSON contract the issue asked for.
        assert body["mode"] == "request_human"
        assert body["safe"] is False
        assert body["event"] == "request_human"
        assert body["run_id"] == "run_1"

        # A second resume of the same verdict is deduped, exit code unchanged.
        code2, _, err2 = _run_cli("--db", db, "resume", "run_1", "--env", "dataset=v3")
        assert code2 == ExitCode.REQUIRES_HUMAN
        # Dedup state is the event log, so a fresh process does not re-ring.
        assert len(captured["hits"]) == 1, err2
        assert "notification sent" not in err2
    finally:
        server.shutdown()


def test_resume_without_registry_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "demo.db")
    _seed_blocked_run(db)
    code, out, err = _run_cli("--db", db, "resume", "run_1", "--env", "dataset=v3")
    assert code == ExitCode.REQUIRES_HUMAN
    assert "notification" not in err


def test_resume_with_malformed_registry_warns_but_keeps_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "demo.db")
    _seed_blocked_run(db)
    (tmp_path / ".continuum").mkdir(exist_ok=True)
    (tmp_path / ".continuum" / "webhooks.json").write_text("{nope", encoding="utf-8")
    code, _, err = _run_cli("--db", db, "resume", "run_1", "--env", "dataset=v3")
    # The bell is broken, not the verdict: same exit code, one warning.
    assert code == ExitCode.REQUIRES_HUMAN
    assert "warning:" in err and "notification skipped" in err
