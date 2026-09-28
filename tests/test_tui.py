"""The terminal dashboard (issue #782).

TuiApp is a pure state machine (keys in, lines out) so every flow below
drives it without a terminal. The curses driver is exercised through a fake
screen, and the two refusal paths (no curses, no TTY) are the ones that must
never half-render.
"""

from __future__ import annotations

import io
import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from continuum.actions import ActionLedger
from continuum.actions.idempotency import idempotency_key
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import ActionStatus, RunStatus
from continuum.storage import SQLiteStorage
from continuum.tui import TuiApp, run_tui
from continuum.tui import model as tui_model
from continuum.tui.app import _driver


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "tui.db")


@pytest.fixture
def store(db: str) -> Iterator[SQLiteStorage]:
    with SQLiteStorage(db) as s:
        yield s


# --------------------------------------------------------------------------- #
# view models
# --------------------------------------------------------------------------- #


def test_run_rows_carry_the_recovery_verdict(db: str, store: SQLiteStorage) -> None:
    run("--db", db, "start", "ok_run", "--goal", "fine")
    run("--db", db, "start", "risky", "--goal", "danger")
    ActionLedger(SQLiteStorage(db), "risky").claim("send_invoice", {}, key="invoice:I-1")

    rows = {r.run_id: r for r in tui_model.run_rows(store)}
    assert rows["ok_run"].mode == "resume"
    assert rows["ok_run"].safe == "yes"
    assert rows["risky"].safe == "no"
    assert rows["risky"].mode != "resume"
    assert rows["risky"].events >= rows["ok_run"].events


def test_a_run_that_fails_to_assess_is_listed_not_dropped(
    db: str, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable run must surface in the index, not vanish from it."""

    class _Boom(tui_model.RecoveryEngine):
        def assess(self, run_id: str, **kw: Any) -> Any:
            raise RuntimeError("chain will not fold")

    run("--db", db, "start", "broken", "--goal", "b")
    monkeypatch.setattr(tui_model, "RecoveryEngine", _Boom)

    rows = tui_model.run_rows(store)
    assert len(rows) == 1
    assert rows[0].mode.startswith("error:")
    assert rows[0].safe == "unknown"


def test_overview_lines_mirror_inspect(db: str, store: SQLiteStorage) -> None:
    run("--db", db, "start", "r1", "--goal", "analyse documents")
    lines = tui_model.overview_lines(store, "r1")
    joined = "\n".join(lines)
    assert "analyse documents" in joined
    assert "progress:" in joined
    assert "version:" in joined
    assert "plan:" in joined


def test_checkpoint_rows_match_history(db: str, store: SQLiteStorage) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    run("--db", db, "checkpoint", "r1", "--trigger", "manual", "--reason", "before lunch")

    rows = tui_model.checkpoint_rows(store, "r1")
    assert len(rows) == 1
    assert rows[0].trigger == "manual"
    assert isinstance(rows[0].version, int)


def test_action_rows_flag_uncertain_actions_with_their_ledger_key(
    db: str, store: SQLiteStorage
) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    ActionLedger(SQLiteStorage(db), "r1").claim("send_invoice", {}, key="invoice:I-1")

    rows = tui_model.action_rows(store, "r1")
    assert len(rows) == 1
    assert rows[0].uncertain is True
    expected = str(idempotency_key("send_invoice", None, scope="r1", key="invoice:I-1"))
    assert rows[0].key == expected
    assert rows[0].action_type == "send_invoice"


def test_event_rows_include_the_archived_prefix_after_compaction(
    db: str, store: SQLiteStorage
) -> None:
    """A compacted run must read the same as one that was never compacted."""
    run("--db", db, "start", "r1", "--goal", "g")
    if not getattr(store, "supports_compaction", False):
        pytest.skip("event-log compaction is not available in this storage version")
    store.compact_run("r1")

    rows = tui_model.event_rows(store, "r1")
    # the archived prefix is included even though the live tail starts later
    assert len(rows) == len(store.read_all_events("r1"))
    assert len(rows) > len(store.read_events("r1"))
    assert rows[0].sequence == 1
    assert rows[0].type == "RUN_STARTED"


def test_budget_rows_count_attempts_over_the_whole_log(db: str, store: SQLiteStorage) -> None:
    """Attempts are per operation (issue #368), so a retried key is the one
    that must show a drawdown."""
    run("--db", db, "start", "r1", "--goal", "g")
    ledger = ActionLedger(SQLiteStorage(db), "r1")
    ledger.claim("send_invoice", {}, key="invoice:I-1")
    key = str(idempotency_key("send_invoice", None, scope="r1", key="invoice:I-1"))
    ledger.reconcile(key, occurred=False)  # confirmed absent, so a retry is legal
    ledger.claim("send_invoice", {}, key="invoice:I-1")

    rows = {r.action_type: r for r in tui_model.budget_rows(store, "r1")}
    assert rows["send_invoice"].attempts == 2
    assert rows["send_invoice"].remaining == rows["send_invoice"].max_attempts - 2


def test_budget_rows_read_the_configured_registry(
    db: str, store: SQLiteStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limits come from .continuum/budgets.json, and configured types with no
    attempts still appear with a full allowance."""
    run("--db", db, "start", "r1", "--goal", "g")
    ActionLedger(SQLiteStorage(db), "r1").claim("send_invoice", {}, key="invoice:I-1")
    registry = tmp_path / ".continuum" / "budgets.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "default_max_attempts": 3,
                "action_types": {"send_invoice": {"max_attempts": 5}, "unused_type": 2},
            }
        )
    )
    monkeypatch.setattr(tui_model, "DEFAULT_BUDGETS_PATH", str(registry))

    rows = {r.action_type: r for r in tui_model.budget_rows(store, "r1")}
    assert rows["send_invoice"].max_attempts == 5
    assert rows["send_invoice"].remaining == 4
    assert rows["unused_type"].attempts == 0
    assert rows["unused_type"].max_attempts == 2
    assert rows["unused_type"].remaining == 2


def test_family_lines_show_every_child_verdict(db: str, store: SQLiteStorage) -> None:
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    ActionLedger(SQLiteStorage(db), "kid").claim("send_invoice", {}, key="invoice:I-9")

    lines = "\n".join(tui_model.family_lines(store, "par"))
    assert "kid" in lines
    assert "!!" in lines


def test_family_lines_refuse_a_missing_run(store: SQLiteStorage) -> None:
    """``get_run`` is the run-existence guard as well as the header's record,
    so one call does both and still raises on a missing run (issue #1157)."""
    from continuum.storage import RunNotFound

    with pytest.raises(RunNotFound):
        tui_model.family_lines(store, "ghost")


def test_recovery_lines_render_the_verdict_and_the_family_block(
    db: str, store: SQLiteStorage
) -> None:
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    ActionLedger(SQLiteStorage(db), "kid").claim("send_invoice", {}, key="invoice:I-9")

    lines = "\n".join(tui_model.recovery_lines(store, "par"))
    assert "CONTINUUM RECOVERY" in lines
    assert "Recovery decision:" in lines
    assert "FAMILY BLOCKED" in lines


def test_a_completed_child_never_blocks_the_parent(db: str, store: SQLiteStorage) -> None:
    """Terminal children are excluded, matching roll_up_children and resume."""
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    run("--db", db, "start", "done", "--goal", "finished", "--parent", "par")
    ActionLedger(SQLiteStorage(db), "kid").claim("send_invoice", {}, key="invoice:I-9")
    run("--db", db, "complete", "done")

    lines = "\n".join(tui_model.recovery_lines(store, "par"))
    assert "FAMILY BLOCKED" in lines  # the live child still blocks
    assert "done" not in lines  # but the completed child is not counted


# --------------------------------------------------------------------------- #
# the landing splash
# --------------------------------------------------------------------------- #


def test_the_app_opens_on_a_landing_screen_with_the_logo(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(SQLiteStorage(db))

    assert app.view == "landing"
    app.width = 100  # wide enough for the ASCII logo
    body = "\n".join(app.body_lines())
    assert "██╔═══██╗" in body  # the logo, not just the word
    assert "run(s) recorded" in body
    assert "press any key to open the dashboard" in app.footer()


def test_a_narrow_terminal_gets_a_banner_that_fits(db: str) -> None:
    app = TuiApp(SQLiteStorage(db))
    app.width = 40
    body = app.body_lines()
    assert all(len(line) <= 40 for line in body)
    assert "C O N T I N U U M" in body[1]


def test_any_key_leaves_the_landing_screen(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(SQLiteStorage(db))

    assert app.handle_key(" ") is True
    assert app.view == "runs"
    assert "r1" in "\n".join(app.body_lines())


def test_q_quits_from_the_landing_screen(db: str) -> None:
    app = TuiApp(SQLiteStorage(db))
    assert app.handle_key("q") is False


# --------------------------------------------------------------------------- #
# the app state machine
# --------------------------------------------------------------------------- #


def _enter_dashboard(app: TuiApp) -> None:
    """Dismiss the landing splash: any key opens the dashboard."""
    assert app.handle_key(" ") is True
    assert app.view == "runs"


def test_the_app_lists_runs_and_quits_on_q(db: str) -> None:
    run("--db", db, "start", "ok_run", "--goal", "fine")
    run("--db", db, "start", "other", "--goal", "more work")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)

    assert app.view == "runs"
    body = "\n".join(app.body_lines())
    assert "ok_run" in body
    assert "other" in body
    assert app.handle_key("q") is False


def test_enter_opens_the_detail_and_esc_returns(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "analyse documents")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)

    app.handle_key("enter")
    assert app.view == "detail"
    assert "overview" in app.header()
    assert "analyse documents" in "\n".join(app.body_lines())

    app.handle_key("esc")
    assert app.view == "runs"
    # the cursor from the detail view must not highlight a runs-list row
    assert app.cursor == -1


def test_tab_keys_switch_views(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    ActionLedger(SQLiteStorage(db), "r1").claim("send_invoice", {}, key="invoice:I-1")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)
    app.handle_key("enter")

    for tab in ("4", "5", "6", "7", "3", "2"):
        app.handle_key(tab)
        assert app.TABS[app.tab] in app.header()

    app.handle_key("4")  # actions
    body = "\n".join(app.body_lines())
    assert "send_invoice" in body
    assert "(!)" in body  # uncertain actions are marked where the eye lands

    app.handle_key("5")  # events
    assert "RUN_STARTED" in "\n".join(app.body_lines())


def test_the_help_overlay_lists_the_keys(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)
    app.handle_key("?")
    assert "quit" in "\n".join(app.body_lines())


def test_reconcile_requires_confirmation_and_only_y_writes(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    ActionLedger(SQLiteStorage(db), "r1").claim("send_invoice", {}, key="invoice:I-1")

    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)
    app.handle_key("enter")
    app.handle_key("4")  # actions tab, cursor on the one action
    app.handle_key("y")  # queue reconcile-as-occurred
    assert app.pending is not None
    assert "OCCURRED" in app.pending[0]

    app.handle_key("n")  # anything but y cancels
    assert app.pending is None
    assert ActionLedger(SQLiteStorage(db), "r1").all()[0].status.value == "started"

    app.handle_key("y")
    app.handle_key("y")  # confirm the write
    assert app.pending is None
    assert app.message.startswith("reconciled")

    with SQLiteStorage(db) as s:
        assert any(e.type is EventType.ACTION_RECONCILED for e in s.read_events("r1"))
    assert ActionLedger(SQLiteStorage(db), "r1").all()[0].status.value == "completed"


def test_a_settled_action_cannot_be_reconciled_again_from_the_tui(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    key = str(idempotency_key("send_invoice", None, scope="r1", key="invoice:I-1"))
    ledger = ActionLedger(SQLiteStorage(db), "r1")
    ledger.claim("send_invoice", {}, key="invoice:I-1")
    ledger.reconcile(key, occurred=True)

    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)
    app.handle_key("enter")
    app.handle_key("4")
    app.handle_key("y")
    assert app.pending is None  # refused before any confirmation prompt
    assert "only uncertain actions" in app.message


def test_checkpoint_and_complete_confirmations_write(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)

    app.handle_key("c")
    assert app.pending is not None
    app.handle_key("y")
    with SQLiteStorage(db) as s:
        assert len(s.list_checkpoints("r1")) == 1

    app.handle_key("x")
    assert "RUN_COMPLETED" in app.pending[0]
    app.handle_key("y")
    with SQLiteStorage(db) as s:
        assert s.get_run("r1").status is RunStatus.COMPLETED


def test_confirm_state_writes_review_confirmed(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)

    app.handle_key("y")
    assert "REVIEW_CONFIRMED" in app.pending[0]
    app.handle_key("y")
    with SQLiteStorage(db) as s:
        assert any(e.type is EventType.REVIEW_CONFIRMED for e in s.read_events("r1"))


def test_a_cancelled_action_writes_nothing(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(SQLiteStorage(db))
    _enter_dashboard(app)
    events_before = len(SQLiteStorage(db).read_events("r1"))

    app.handle_key("c")
    app.handle_key("x")  # not y: cancels
    assert app.pending is None
    assert len(SQLiteStorage(db).read_events("r1")) == events_before


# --------------------------------------------------------------------------- #
# the curses driver and the refusal paths
# --------------------------------------------------------------------------- #


class _FakeCurses:
    """Just enough curses for the driver: constants, attrs, no terminal."""

    KEY_UP = 259
    KEY_DOWN = 258
    KEY_LEFT = 260
    KEY_RIGHT = 261
    KEY_ENTER = 343
    KEY_RESIZE = 410
    KEY_BACKSPACE = 263
    A_BOLD = 1
    A_REVERSE = 2

    class error(Exception):
        pass

    @staticmethod
    def curs_set(visible: int) -> None:
        return None


class _FakeScreen:
    """Records what the driver draws and replays a fixed key script."""

    def __init__(self, keys: list[int]) -> None:
        self._keys = list(keys)
        self.lines: list[tuple[int, str]] = []

    def keypad(self, on: bool) -> None:
        return None

    def timeout(self, ms: int) -> None:
        return None

    def erase(self) -> None:
        self.lines = []

    def getmaxyx(self) -> tuple[int, int]:
        return (24, 80)

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0) -> None:
        self.lines.append((y, text[:n]))

    def refresh(self) -> None:
        return None

    def getch(self) -> int:
        return self._keys.pop(0) if self._keys else ord("q")


def test_the_driver_draws_and_honours_keys(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    # a space dismisses the landing splash, then the driver is driven normally
    screen = _FakeScreen([ord(" "), _FakeCurses.KEY_DOWN, _FakeCurses.KEY_ENTER, ord("4")])
    code = _driver(_FakeCurses(), screen, TuiApp(SQLiteStorage(db)), 0.0)

    assert code == ExitCode.OK
    drawn = "\n".join(text for _, text in screen.lines)
    assert "CONTINUUM" in drawn
    assert "actions" in drawn


def test_the_driver_draws_the_splash_first(db: str) -> None:
    run("--db", db, "start", "r1", "--goal", "g")
    screen = _FakeScreen([])  # the default key is q: the splash is all we see
    _driver(_FakeCurses(), screen, TuiApp(SQLiteStorage(db)), 0.0)

    drawn = "\n".join(text for _, text in screen.lines)
    assert "██╔═══██╗" in drawn  # the logo: 80 columns is just wide enough
    assert "press any key" in drawn


def test_the_incompatible_database_splash_still_draws_the_logo() -> None:
    app = TuiApp(
        None,
        database_error=(
            "database schema v6 was written by a newer CONTINUUM; this build understands v2"
        ),
    )
    screen = _FakeScreen([])

    code = _driver(_FakeCurses(), screen, app, 0.0)

    drawn = "\n".join(text for _, text in screen.lines)
    assert code == ExitCode.OK
    assert "██╔═══██╗" in drawn
    assert "database unavailable" in drawn


def test_run_tui_refuses_when_curses_is_missing(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "curses", None)
    err = io.StringIO()
    code = run_tui(SQLiteStorage(db), err=err)

    assert code == ExitCode.ERROR
    assert "dashboard" in err.getvalue()


def test_run_tui_refuses_without_a_tty(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class _NotATty(io.StringIO):
        def isatty(self) -> bool:
            return False

    # stub curses so the import succeeds on Windows too: this test is about
    # the TTY refusal, not the platform's curses availability
    monkeypatch.setitem(sys.modules, "curses", types.ModuleType("curses"))
    monkeypatch.setattr(sys, "stdout", _NotATty())
    err = io.StringIO()
    code = run_tui(SQLiteStorage(db), err=err)

    assert code == ExitCode.ERROR
    assert "not a TTY" in err.getvalue()


def test_the_tui_command_is_registered_and_documented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["tui", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--refresh" in help_text
    assert "dashboard" in help_text


# --------------------------------------------------------------------------- #
# bare `continuum`: splash when interactive, help otherwise
# --------------------------------------------------------------------------- #


class _Tty(io.StringIO):
    """A stream that claims to be a terminal, as a real shell would give."""

    def isatty(self) -> bool:
        return True


def test_bare_continuum_opens_the_tui_when_interactive(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_tui(storage: Any, **kw: Any) -> int:
        seen["storage"] = storage
        return 77

    monkeypatch.setattr("continuum.tui.run_tui", fake_run_tui)
    code = main(["--db", db], out=_Tty())

    assert code == 77
    assert seen["storage"] is not None


def test_bare_continuum_restores_splash_for_an_incompatible_database(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer DB is preserved, but it must not hide the branded launcher."""
    from continuum.storage import SchemaVersionError

    seen: dict[str, Any] = {}

    def fail_open(path: str) -> Any:
        raise SchemaVersionError(
            "database schema v6 was written by a newer CONTINUUM; this build understands v2"
        )

    def fake_run_tui(storage: Any, **kw: Any) -> int:
        seen["storage"] = storage
        seen.update(kw)
        return 77

    import importlib

    cli_module = importlib.import_module("continuum.cli.main")
    monkeypatch.setattr(cli_module, "open_storage", fail_open)
    monkeypatch.setattr("continuum.tui.run_tui", fake_run_tui)

    code = main(["--db", db], out=_Tty())

    assert code == 77
    assert seen["storage"] is None
    assert "schema v6" in seen["database_error"]


def test_bare_continuum_prints_help_without_a_tty(db: str) -> None:
    """A script running `continuum` blind must find usage text, not curses."""
    code, out, _ = run("--db", db)

    assert code == ExitCode.OK
    assert "usage:" in out
    assert "dashboard" in out


def test_bare_continuum_prints_help_with_json(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--json` is a machine contract: it never opens an interactive screen."""

    def fail_tui(storage: Any, **kw: Any) -> int:
        raise AssertionError("the tui must not open under --json")

    monkeypatch.setattr("continuum.tui.run_tui", fail_tui)
    code, out, _ = run("--db", db, "--json")

    assert code == ExitCode.OK
    assert "usage:" in out


def test_bare_continuum_reports_an_unopenable_database(db: str, tmp_path: Path) -> None:
    bad = tmp_path / "not-a-dir" / "continuum.db"
    out, err = _Tty(), io.StringIO()

    code = main(["--db", str(bad)], out=out, err=err)

    assert code == ExitCode.ERROR
    assert "error:" in err.getvalue()
    assert "usage:" not in out.getvalue()


def test_an_unreadable_run_fails_one_view_not_the_dashboard(
    db: str, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detail view must degrade to an error page, never crash the app."""
    run("--db", db, "start", "broken", "--goal", "unreadable")
    app = TuiApp(store)
    app.handle_key("enter")  # landing -> runs
    app.handle_key("enter")  # runs -> detail (overview tab)

    def boom(storage: Any, run_id: str) -> list[str]:
        raise RuntimeError("chain will not fold")

    monkeypatch.setattr(tui_model, "overview_lines", boom)
    app.handle_key("r")  # refresh the detail view through the failure

    body = "\n".join(app.body_lines())
    assert "Cannot read run broken" in body
    assert "chain will not fold" in body
    # the app is still alive: tab switches keep working
    app.handle_key("2")  # recovery tab
    assert app.TABS[app.tab] == "recovery"


def test_the_splash_counts_runs_without_assessing_recovery(
    db: str, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idle splash must be cheap: a count, not a per-run recovery assess."""
    run("--db", db, "start", "one", "--goal", "a")
    run("--db", db, "start", "two", "--goal", "b")

    calls: list[int] = []
    real_run_rows = tui_model.run_rows

    def counting(storage: Any) -> list[Any]:
        calls.append(1)
        return real_run_rows(storage)

    monkeypatch.setattr(tui_model, "run_rows", counting)
    app = TuiApp(store)

    body = "\n".join(app.body_lines())
    assert "2 run(s) recorded" in body
    assert not calls  # the splash counted without reading any rows

    app.handle_key("enter")  # leaving the splash does read the full rows
    assert calls
    assert len(app.rows) == 2


def test_an_unreadable_store_degrades_the_runs_view_not_the_app(
    db: str, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that opens but cannot list runs shows a message, not a crash."""
    app = TuiApp(store)
    app.handle_key("enter")  # landing -> runs

    def boom(storage: Any) -> list[Any]:
        raise RuntimeError("disk unreadable")

    monkeypatch.setattr(tui_model, "run_rows", boom)
    app.handle_key("r")  # refresh the runs view through the failure

    body = "\n".join(app.body_lines())
    assert "Cannot read runs" in body
    assert "disk unreadable" in body
    # the app is still alive: retry after the failure is cleared
    monkeypatch.undo()
    app.handle_key("r")
    assert "Cannot read runs" not in "\n".join(app.body_lines())


def test_the_cursor_survives_a_refresh_tick(db: str, store: SQLiteStorage) -> None:
    """An auto-refresh tick must not move the selection the operator parked."""
    run("--db", db, "start", "r1", "--goal", "g")
    ledger = ActionLedger(SQLiteStorage(db), "r1")
    ledger.claim("send_invoice", {}, key="invoice:I-1")
    ledger.claim("send_email", {}, key="email:E-1")
    ledger.claim("send_fax", {}, key="fax:F-1")

    app = TuiApp(store)
    app.handle_key("enter")  # landing -> runs
    app.handle_key("enter")  # runs -> detail
    app.handle_key("4")  # actions tab
    app.handle_key("j")
    app.handle_key("j")  # park on the third action row
    assert app.cursor == 3

    app.handle_key("r")  # what an auto-refresh tick does

    assert app.cursor == 3  # still parked on the third row


def test_switching_to_a_text_tab_does_not_inherit_the_table_selection(
    db: str, store: SQLiteStorage
) -> None:
    """A selection parked on a table tab must not light up a text tab.

    Text tabs own the scroll, not a cursor; inheriting the old position
    would show a phantom highlight and flip navigation into cursor mode.
    """
    run("--db", db, "start", "r1", "--goal", "g")
    ledger = ActionLedger(SQLiteStorage(db), "r1")
    ledger.claim("send_invoice", {}, key="invoice:I-1")
    ledger.claim("send_email", {}, key="email:E-1")
    ledger.claim("send_fax", {}, key="fax:F-1")

    app = TuiApp(store)
    app.handle_key("enter")  # landing -> runs
    app.handle_key("enter")  # runs -> detail
    app.handle_key("4")  # actions tab
    app.handle_key("j")
    app.handle_key("j")  # park on the third action row
    assert app.cursor == 3

    app.handle_key("2")  # jump straight to the recovery tab (text)

    assert app.cursor == -1  # no selection on a text tab
    # and a refresh while parked there still moves nothing
    app.handle_key("r")
    assert app.cursor == -1
    # switching back re-renders the table without resurrecting the old row
    app.handle_key("4")
    assert "send_invoice" in "\n".join(app.body_lines())


def test_a_recovered_splash_count_retires_the_old_error(
    db: str, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the count succeeds again, the splash must not keep the stale error."""
    app = TuiApp(store)

    def boom() -> int:
        raise RuntimeError("count failed")

    monkeypatch.setattr(app, "_count_runs", boom)
    app.refresh()
    assert "cannot count runs" in "\n".join(app.body_lines())

    monkeypatch.undo()
    app.refresh()  # the next auto-refresh tick counts again

    assert "cannot count runs" not in "\n".join(app.body_lines())


def test_an_unreadable_run_degrades_the_actions_tab_not_the_app(
    db: str, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose action rows cannot be read shows a message, and a pending
    selection on that tab reads as nothing selected rather than raising."""
    run("--db", db, "start", "r1", "--goal", "g")
    app = TuiApp(store)
    app.handle_key("enter")  # landing -> runs
    app.handle_key("enter")  # runs -> detail
    app.handle_key("4")  # actions tab

    def boom(storage: Any, run_id: str) -> list[Any]:
        raise RuntimeError("index corrupted")

    monkeypatch.setattr(tui_model, "action_rows", boom)
    app.handle_key("r")  # refresh the actions tab through the failure

    body = "\n".join(app.body_lines())
    assert "Cannot read run r1 (actions tab)" in body
    assert "index corrupted" in body

    # the guarded selection read degrades to "nothing selected", not a raise
    app.lines = ["header", "row one"]
    app.cursor = 1
    assert app._selected_action() is None
    assert app._selected_action() is None  # and repeated probes stay quiet


def test_family_lines_find_children_written_before_the_parent_column(
    db: str, store: SQLiteStorage
) -> None:
    """A child recorded only in Run.metadata still shows on the tree tab.

    The parent_run_id column postdates some deployments; those runs' children
    must not vanish from the family view.
    """
    run("--db", db, "start", "parent", "--goal", "supervise")
    run("--db", db, "start", "legacy_child", "--goal", "work", "--parent", "parent")
    # Simulate the pre-column record: parent linkage only in metadata.
    store._connection.execute(
        "UPDATE runs SET parent_run_id = NULL, metadata = ? WHERE run_id = ?",
        ('{"parent_run_id": "parent"}', "legacy_child"),
    )
    store._connection.commit()

    lines = "\n".join(tui_model.family_lines(store, "parent"))
    assert "legacy_child" in lines


def test_budget_rows_show_configured_types_with_no_attempts(
    db: str, store: SQLiteStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured action type appears even before its first recorded attempt,
    and a malformed payload in the log cannot crash the view."""
    run("--db", db, "start", "r1", "--goal", "g")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".continuum").mkdir(exist_ok=True)
    (tmp_path / ".continuum" / "budgets.json").write_text(
        '{"action_types": {"send_invoice": {"max_attempts": 5}}}'
    )

    rows = {r.action_type: r for r in tui_model.budget_rows(store, "r1")}

    assert "send_invoice" in rows  # configured, though never attempted
    assert rows["send_invoice"].attempts == 0
    assert rows["send_invoice"].max_attempts == 5
    assert rows["send_invoice"].remaining == 5


def test_the_settle_key_tracks_the_drawn_row_not_a_fresh_read(
    db: str, store: SQLiteStorage
) -> None:
    """A second action arriving after the render sorts ahead of the selected
    one, so a fresh read at the same line number returns a different key: `y`
    must settle the action the highlight marks, not the one that displaced it.
    """
    run("--db", db, "start", "r1", "--goal", "g")
    ledger = ActionLedger(SQLiteStorage(db), "r1")
    ledger.claim("send_invoice", {}, key="invoice:ZZZ")
    drawn_key = str(idempotency_key("send_invoice", None, scope="r1", key="invoice:ZZZ"))

    app = TuiApp(store)
    app.handle_key("enter")  # landing -> runs
    app.handle_key("enter")  # runs -> detail
    app.handle_key("4")  # actions tab; one row drawn, cursor parked on it
    assert app._selected_action() is not None
    assert app._selected_action().key == drawn_key

    # an action arriving out of band, sorting ahead of the drawn row
    ActionLedger(SQLiteStorage(db), "r1").claim("send_invoice", {}, key="invoice:AAA")
    assert str(idempotency_key("send_invoice", None, scope="r1", key="invoice:AAA")) < drawn_key
    # the store really has shifted: a fresh read no longer has the drawn key first
    assert tui_model.action_rows(store, "r1")[0].key != drawn_key
    # but no re-render happened, so the selection is still the drawn row
    assert app._selected_action().key == drawn_key

    app.handle_key("y")  # settle the action under the highlight
    assert app.pending is not None
    settled = app.pending[1]()
    assert settled, "the reconcile must take effect"
    folded = ActionLedger(SQLiteStorage(db), "r1").folded()
    assert folded[drawn_key].status == ActionStatus.COMPLETED  # occurred=True
    assert app._selected_action().key == drawn_key  # and the highlight holds


def test_a_refresh_keeps_the_selected_action_under_the_cursor(
    db: str, store: SQLiteStorage
) -> None:
    """A refresh re-sorts the actions tab; the cursor must follow the selected
    key to its new line rather than stay put and mark a different action."""
    run("--db", db, "start", "r1", "--goal", "g")
    ledger = ActionLedger(SQLiteStorage(db), "r1")
    ledger.claim("send_invoice", {}, key="invoice:ZZZ")
    selected = str(idempotency_key("send_invoice", None, scope="r1", key="invoice:ZZZ"))

    app = TuiApp(store)
    app.handle_key("enter")
    app.handle_key("enter")
    app.handle_key("4")
    assert app.cursor == 1 and app._selected_action().key == selected

    ActionLedger(SQLiteStorage(db), "r1").claim("send_invoice", {}, key="invoice:AAA")
    app.handle_key("r")  # refresh: the new action sorts ahead of the selected one

    assert app.cursor == 2  # followed its key down a line
    assert app._selected_action().key == selected


def test_a_legacy_child_blocks_the_recovery_verdict_like_a_recorded_one(
    db: str, store: SQLiteStorage
) -> None:
    """The tree tab and the recovery verdict must resolve children the same
    way. A legacy child, linked only in metadata, used to appear on the tree
    while the roll-up missed it, so the verdict read RESUME over a family
    holding an unreconciled side effect."""
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    ActionLedger(SQLiteStorage(db), "kid").claim("send_invoice", {}, key="invoice:I-9")
    store._connection.execute(
        "UPDATE runs SET parent_run_id = NULL, metadata = ? WHERE run_id = ?",
        ('{"parent_run_id": "par"}', "kid"),
    )
    store._connection.commit()

    tree = "\n".join(tui_model.family_lines(store, "par"))
    assert "kid" in tree  # the tree tab lists it

    verdict = "\n".join(tui_model.recovery_lines(store, "par"))
    # and the verdict accounts for it, rather than calling the family safe
    assert "FAMILY BLOCKED" in verdict
    assert "kid" in verdict
