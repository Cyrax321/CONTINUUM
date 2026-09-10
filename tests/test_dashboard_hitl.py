"""Human-in-the-loop dashboard surface (issue #242).

Buttons on the run page map 1:1 onto the human CLI verbs (confirm,
reconcile occurred=true/false, complete) and land identical events with
identical provenance. Mutating endpoints are fail-closed:
CONTINUUM_DASHBOARD_TOKEN must be set or every POST is refused.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.dashboard.app import make_dashboard_server
from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.storage import SQLiteStorage


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CONTINUUM_DASHBOARD_TOKEN", "op-secret")
    return "op-secret"


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "hitl.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Do a thing"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "Do a thing"})
    yield path


@pytest.fixture
def addr(db: str):
    server = _ServerThread(db)
    server.start()
    yield server.addr
    server.shutdown()


class _ServerThread:
    """Runs the dashboard in a thread on an OS-assigned port.

    Built on make_dashboard_server so shutdown() can actually stop the
    server: a leaked listener held its port for the whole session and made
    later tests' port probes collide (Connection refused under load).
    """

    def __init__(self, db: str) -> None:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = int(sock.getsockname()[1])
        self.server = make_dashboard_server(SQLiteStorage(db), port=self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.addr = f"127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


_ports = {"n": 8940}


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def post(addr: str, path: str, form: dict[str, str]) -> tuple[int, str]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(f"http://{addr}{path}", data=data)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def seed_uncertain(db: str, key: str = "invoice:I-1") -> str:
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    outcome = ledger.claim("send_invoice", {}, key=key)
    del outcome
    from continuum.actions.idempotency import idempotency_key

    return idempotency_key("send_invoice", None, scope="run_1", key=key)


def fold_statuses(db: str) -> list[str]:
    from continuum.actions.ledger import fold_action_events

    with SQLiteStorage(db) as store:
        folded = fold_action_events(store.read_events("run_1"))
    return [a.status.value for a in folded.values()]


# --- auth ------------------------------------------------------------------------ #


def test_mutations_refused_without_token(db: str, addr: str) -> None:
    status, body = post(addr, "/action/confirm", {"run_id": "run_1"})
    assert status == 403
    assert "invalid dashboard token" in body


def test_mutations_refused_with_wrong_token(db: str, addr: str) -> None:
    status, body = post(addr, "/action/confirm", {"run_id": "run_1", "token": "wrong"})
    assert status == 403
    assert "invalid dashboard token" in body


def test_reads_stay_open_without_token(db: str, addr: str) -> None:
    status, body = urllib_get(addr, "/")
    assert status == 200
    assert "CONTINUUM Dashboard" in body


def urllib_get(addr: str, path: str) -> tuple[int, str]:
    resp = urllib.request.urlopen(f"http://{addr}{path}", timeout=5)
    return resp.status, resp.read().decode()


# --- confirm ------------------------------------------------------------------------ #


def test_confirm_button_lands_human_review_confirmed(db: str, addr: str) -> None:
    status, body = post(addr, "/action/confirm", {"run_id": "run_1", "token": "op-secret"})
    assert status == 200
    assert "[ok] goal and progress confirmed" in body

    with SQLiteStorage(db) as store:
        events = [e for e in store.read_events("run_1") if e.type is EventType.REVIEW_CONFIRMED]
    assert events and events[-1].source is Origin.HUMAN
    assert events[-1].payload.get("via") == "dashboard"


# --- reconcile ------------------------------------------------------------------------ #


def test_reconcile_true_settles_uncertain_action(db: str, addr: str) -> None:
    key = seed_uncertain(db, key="invoice:H-1")
    status, body = post(
        addr,
        "/action/reconcile",
        {
            "run_id": "run_1",
            "ledger_key": key,
            "occurred": "true",
            "token": "op-secret",
        },
    )
    assert status == 200
    assert "reconciled" in body
    assert "completed" in fold_statuses(db)


def test_archived_uncertain_action_remains_visible_after_compaction(db: str) -> None:
    from continuum.dashboard.hitl import pending_actions_with_keys

    key = seed_uncertain(db, key="invoice:H-archived")
    with SQLiteStorage(db) as store:
        store.compact_run("run_1")
        assert not any(
            event.type is EventType.ACTION_RECORDED for event in store.read_events("run_1")
        )

        pending = pending_actions_with_keys(store, "run_1")

    assert [(ledger_key, action.status.value) for ledger_key, action in pending] == [
        (key, "started")
    ]


def test_reconcile_false_frees_the_action_for_retry(db: str, addr: str) -> None:
    seed_uncertain(db, key="invoice:H-2")
    status, _ = post(
        addr,
        "/action/reconcile",
        {
            "run_id": "run_1",
            "ledger_key": idem("invoice:H-2"),
            "occurred": "false",
            "token": "op-secret",
        },
    )
    assert status == 200
    assert "failed" in fold_statuses(db)


def idem(rendered: str) -> str:
    from continuum.actions.idempotency import idempotency_key

    return idempotency_key("send_invoice", None, scope="run_1", key=rendered)


# --- complete ------------------------------------------------------------------------ #


def test_complete_button_flips_the_run_row(db: str, addr: str) -> None:
    status, body = post(
        addr,
        "/action/complete",
        {
            "run_id": "run_1",
            "summary": "shipped",
            "token": "op-secret",
        },
    )
    assert status == 200
    assert "run completed" in body
    with SQLiteStorage(db) as store:
        assert store.get_run("run_1").status.value == "completed"
        events = [e for e in store.read_events("run_1") if e.type is EventType.RUN_COMPLETED]
    assert events and events[-1].payload["summary"] == "shipped"


# --- unknown route --------------------------------------------------------------------- #


def test_unknown_post_route_maps_to_404(db: str, addr: str) -> None:
    status, body = post(
        addr,
        "/action/teleport",
        {
            "run_id": "run_1",
            "token": "op-secret",
        },
    )
    assert status == 404


EOF_MARK = None
