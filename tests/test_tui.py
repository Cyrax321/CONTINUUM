"""The terminal dashboard (issue #782).

TuiApp is a pure state machine (keys in, lines out) so every flow below
drives it without a terminal. The curses driver is exercised through a fake
screen, and the two refusal paths (no curses, no TTY) are the ones that must
never half-render.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from continuum.actions import ActionLedger
from continuum.actions.idempotency import idempotency_key
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import RunStatus
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


def test_family_lines_show_every_child_verdict(db: str, store: SQLiteStorage) -> None:
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    ActionLedger(SQLiteStorage(db), "kid").claim("send_invoice", {}, key="invoice:I-9")

    lines = "\n".join(tui_model.family_lines(store, "par"))
    assert "kid" in lines
    assert "!!" in lines


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

    monkeypatch.setattr(sys, "stdout", _NotATty())
    err = io.StringIO()
    code = run_tui(SQLiteStorage(db), err=err)

    assert code == ExitCode.ERROR
    # On platforms without curses (Windows) the import check refuses first,
    # before the TTY check is reached. Both are refusals pointing at the
    # browser dashboard, so either message satisfies this test.
    out = err.getvalue()
    assert "not a TTY" in out or "not available on this platform" in out


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


def test_bare_continuum_prints_help_when_piped(db: str) -> None:
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
