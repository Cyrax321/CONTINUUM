"""The ``continuum tui`` driver: a full-screen terminal dashboard (issue #782).

Two layers, deliberately separated:

- :class:`TuiApp` is a pure state machine. Keys arrive as plain names
  (``"up"``, ``"enter"``, ``"y"``) and rendering is a list of strings, so
  every flow is testable without a terminal.
- :func:`run_tui` is a thin curses driver that maps real key codes onto those
  names and draws the lines. It contains no decisions of its own.

House style: read-only until an action is confirmed. Every mutating verb
lands in ``pending`` first, the footer shows exactly what will happen, and
only a further ``y`` performs the write. Anything else cancels.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from typing import Any

from continuum import __version__
from continuum.cli.exitcodes import ExitCode
from continuum.storage.base import Storage
from continuum.tui import model
from continuum.tui.model import RunRow

__all__ = ["TuiApp", "run_tui"]

#: The splash logo, hand-drawn in the ANSI Shadow style and joined at full
#: glyph width so it fits a standard 80-column terminal (78 columns exactly).
_LOGO_LINES = (
    " ██████╗ ██████╗ ███╗   ██╗████████╗██╗███╗   ██╗██╗   ██╗██╗   ██╗███╗   ███╗",
    "██╔════╝██╔═══██╗████╗  ██║╚══██╔══╝██║████╗  ██║██║   ██║██║   ██║████╗ ████║",
    "██║     ██║   ██║██╔██╗ ██║   ██║   ██║██╔██╗ ██║██║   ██║██║   ██║██╔████╔██║",
    "██║     ██║   ██║██║╚██╗██║   ██║   ██║██║╚██╗██║██║   ██║██║   ██║██║╚██╔╝██║",
    "╚██████╗╚██████╔╝██║ ╚████║   ██║   ██║██║ ╚████║╚██████╔╝╚██████╔╝██║ ╚═╝ ██║",
    " ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝   ╚═╝   ╚═╝╚═╝  ╚═══╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝",
)
_LOGO_WIDTH = max(len(line) for line in _LOGO_LINES)
_LANDING_TAGLINE = "durable recovery for long-running agents"


class TuiApp:
    """State machine for the terminal dashboard.

    The view is three-level: a landing splash (logo, version, run count),
    then a runs index, then one run's detail with tabs (overview, recovery,
    checkpoints, actions, events, family, budget). Table tabs carry a
    selectable cursor; text tabs only scroll.
    """

    TABS = ("overview", "recovery", "checkpoints", "actions", "events", "family", "budget")
    _TABLE_TABS = frozenset({"checkpoints", "actions", "events", "budget"})

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.view = "landing"
        self.width = 80  # the driver restamps this from the real screen each draw
        self.tab = 0
        self.index = 0  # selection in the runs index
        self.cursor = -1  # selected body line in a table tab, -1 when none
        self.scroll = 0
        self.rows: list[RunRow] = []
        self.lines: list[str] = []
        self.pending: tuple[str, Callable[[], str]] | None = None
        self.message = ""
        self.show_help = False
        self.refresh()

    # ------------------------------------------------------------------ #
    # data
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        """Reload the current view's data from storage. Read-only."""
        if self.view in ("landing", "runs"):
            # the landing screen shows the run count, and leaving it lands on
            # the runs list, so both views read the same rows
            self.rows = model.run_rows(self.storage)
            self.cursor = -1  # the runs list marks its selection itself
            if self.rows:
                self.index = min(self.index, len(self.rows) - 1)
        else:
            self._refresh_detail()

    def _run_id(self) -> str | None:
        if 0 <= self.index < len(self.rows):
            return self.rows[self.index].run_id
        return None

    def _refresh_detail(self) -> None:
        run_id = self._run_id()
        if run_id is None:
            self.lines = ["No run selected. Press esc to go back to the runs list."]
            self.cursor = -1
            return
        tab = self.TABS[self.tab]
        self.cursor = -1
        rows: list[Any]  # one of the model's table row types, per tab
        if tab == "overview":
            self.lines = model.overview_lines(self.storage, run_id)
        elif tab == "recovery":
            self.lines = model.recovery_lines(self.storage, run_id)
        elif tab == "checkpoints":
            rows = model.checkpoint_rows(self.storage, run_id)
            self.lines = [f"{'CHECKPOINT':<12} {'VERSION':<9} {'TRIGGER':<10} COMPLETED"]
            self.lines += [
                f"{r.checkpoint_id[:10]:<12} v{r.version:<8} {r.trigger:<10} {r.completed}"
                for r in rows
            ] or ["No checkpoints recorded. Press c to force one."]
            self.cursor = 1 if len(self.lines) > 1 else -1
        elif tab == "actions":
            rows = model.action_rows(self.storage, run_id)
            self.lines = [f"{'STATUS':<16} {'TYPE':<24} {'EXTERNAL ID':<20} KEY"]
            self.lines += [
                (
                    f"{'(!)' if r.uncertain else '   '} {r.status:<12} {r.action_type:<24} "
                    f"{r.external_id[:18]:<20} {r.key[:24]}"
                )
                for r in rows
            ] or ["No actions recorded."]
            self.cursor = 1 if len(self.lines) > 1 else -1
        elif tab == "events":
            rows = model.event_rows(self.storage, run_id)
            self.lines = [f"{'SEQ':>5}  {'TYPE':<26} PAYLOAD"]
            self.lines += [f"{r.sequence:>5}  {r.type:<26} {r.summary}" for r in rows] or [
                "No events."
            ]
            self.cursor = 1 if len(self.lines) > 1 else -1
        elif tab == "family":
            self.lines = model.family_lines(self.storage, run_id)
        elif tab == "budget":
            rows = model.budget_rows(self.storage, run_id)
            self.lines = [f"{'ACTION TYPE':<28} {'ATTEMPTS':>8} {'MAX':>4} {'REMAINING':>10}"]
            self.lines += [
                f"{r.action_type:<28} {r.attempts:>8} {r.max_attempts:>4} {r.remaining:>10}"
                for r in rows
            ] or ["No budgets configured."]
            self.cursor = 1 if len(self.lines) > 1 else -1
        if self.scroll > max(0, len(self.lines) - 1):
            self.scroll = 0

    def _selected_action(self) -> model.ActionRow | None:
        """The action row under the cursor, on the actions tab only."""
        if self.view != "detail" or self.TABS[self.tab] != "actions" or self.cursor < 1:
            return None
        rows = model.action_rows(self.storage, self._run_id() or "")
        offset = self.cursor - 1
        if 0 <= offset < len(rows):
            return rows[offset]
        return None

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #

    def header(self) -> str:
        """The top line: where we are and what view is active."""
        if self.view == "landing":
            return ""
        if self.view == "runs":
            return "CONTINUUM  runs  (enter: open, r: refresh, ?: help, q: quit)"
        run_id = self._run_id() or "-"
        tab = self.TABS[self.tab]
        return (
            f"CONTINUUM  run {run_id}  "
            f"[{self.tab + 1}/{len(self.TABS)} {tab}]  "
            "(left/right or 1-7: tabs, esc: runs, r: refresh, q: quit)"
        )

    def _landing_lines(self) -> list[str]:
        """The splash page: logo, version, how many runs the store holds."""

        def center(text: str) -> str:
            return " " * max(0, (self.width - len(text)) // 2) + text

        if self.width >= _LOGO_WIDTH + 2:
            art: list[str] = list(_LOGO_LINES)
        else:  # too narrow for the logo: a banner that fits, not one that clips
            art = ["C O N T I N U U M"]
        runs_line = f"{len(self.rows)} run(s) recorded" if self.rows else "no runs recorded yet"
        return (
            [""]
            + [center(line) for line in art]
            + ["", ""]
            + [center(_LANDING_TAGLINE), center(f"v{__version__}   {runs_line}")]
            + ["", "", center("press any key to open the dashboard")]
        )

    def body_lines(self) -> list[str]:
        """The body: the help overlay when asked for, the view otherwise."""
        if self.show_help:
            return list(_HELP_LINES)
        if self.view == "landing":
            return self._landing_lines()
        if self.view == "runs":
            if not self.rows:
                return ['No runs recorded. Start one with: continuum start <id> --goal "..."']
            lines = [f"{'RUN':<20} {'STATUS':<10} {'EVT':>5}  {'MODE':<14} {'SAFE':<7} GOAL"]
            for i, row in enumerate(self.rows):
                marker = ">" if i == self.index else " "
                lines.append(
                    f"{marker} {row.run_id:<19} {row.status:<10} {row.events:>5}  "
                    f"{row.mode:<14} {row.safe:<7} {row.goal}"
                )
            return lines
        marked = []
        for i, line in enumerate(self.lines):
            if self.cursor >= 0:
                marked.append(("> " if i == self.cursor else "  ") + line)
            else:
                marked.append(line)
        return marked

    def footer(self) -> str:
        """The bottom line: a pending confirmation outranks everything, and a
        result message outranks the key hints. The hints are the longest text
        here, so on an 80-column terminal they would otherwise clip the one
        line the operator most needs to read."""
        if self.pending is not None:
            return f"{self.pending[0]}  [y = do it, anything else = cancel]"
        if self.message:
            return self.message
        if self.show_help:
            return "? hides help"
        if self.view == "landing":
            return "press any key to open the dashboard   q quits"
        if self.view == "runs":
            return "runs: enter open | r refresh | c checkpoint | x complete | y confirm"
        return "1-7 tabs | esc back | y/n reconcile (actions tab) | c checkpoint | x complete"

    # ------------------------------------------------------------------ #
    # keys
    # ------------------------------------------------------------------ #

    def handle_key(self, key: str) -> bool:
        """Apply one key. Returns False when the app should quit."""
        if self.pending is not None:
            self._resolve_pending(key)
            return True
        if key == "q":
            return False
        if self.view == "landing":
            if key == "resize":  # a resize is not a keystroke: keep the splash
                return True
            # the splash promises "press any key", so any key (except q above)
            # opens the dashboard; there is nothing else to do on this screen
            self.view = "runs"
            self.message = ""
            self.scroll = 0
            self.refresh()
            return True
        if key == "?":
            self.show_help = not self.show_help
            return True
        if key == "r":
            self.refresh()
            return True
        if self.show_help:
            return True  # any other key just leaves help on screen

        if self.view == "runs":
            return self._handle_runs_key(key)
        return self._handle_detail_key(key)

    def _resolve_pending(self, key: str) -> None:
        assert self.pending is not None
        prompt, action = self.pending
        if key == "y":
            try:
                self.message = action()
            except Exception as exc:
                self.message = f"error: {exc}"
        else:
            self.message = f"cancelled: {prompt.split('?')[0].strip()}"
        self.pending = None
        self.refresh()

    def _handle_runs_key(self, key: str) -> bool:
        if key in ("up", "k") and self.rows:
            self.index = (self.index - 1) % len(self.rows)
        elif key in ("down", "j") and self.rows:
            self.index = (self.index + 1) % len(self.rows)
        elif key in ("enter", "o", "right") and self.rows:
            self.view = "detail"
            self.tab = 0
            self.scroll = 0
            self.message = ""
            self._refresh_detail()
        elif key == "c":
            self._queue_checkpoint()
        elif key == "x":
            self._queue_complete()
        elif key == "y":
            self._queue_confirm()
        return True

    def _handle_detail_key(self, key: str) -> bool:
        tab = self.TABS[self.tab]
        if key == "esc" or (key == "left" and tab == "overview"):
            self.view = "runs"
            self.scroll = 0
            self.message = ""
            self.refresh()
        elif key in ("left", "h"):
            self.tab = (self.tab - 1) % len(self.TABS)
            self.scroll = 0
            self._refresh_detail()
        elif key in ("right", "l"):
            self.tab = (self.tab + 1) % len(self.TABS)
            self.scroll = 0
            self._refresh_detail()
        elif key.isdigit() and 1 <= int(key) <= len(self.TABS):
            self.tab = int(key) - 1
            self.scroll = 0
            self._refresh_detail()
        elif key in ("up", "k"):
            self._move_selection(-1)
        elif key in ("down", "j"):
            self._move_selection(1)
        elif key == "c":
            self._queue_checkpoint()
        elif key == "x":
            self._queue_complete()
        elif key == "y":
            if tab == "actions":
                self._queue_reconcile(occurred=True)
            else:
                self._queue_confirm()
        elif key == "n":
            if tab == "actions":
                self._queue_reconcile(occurred=False)
        return True

    def _move_selection(self, delta: int) -> None:
        """Move the cursor in table tabs, the scroll in text tabs."""
        if self.cursor >= 0:
            self.cursor = max(1, min(len(self.lines) - 1, self.cursor + delta))
        else:
            self.scroll = max(0, min(max(0, len(self.lines) - 1), self.scroll + delta))

    def _queue_checkpoint(self) -> None:
        run_id = self._run_id()
        if run_id is None:
            self.message = "no run selected"
            return
        self.pending = (
            f"force a checkpoint on run {run_id} now?",
            lambda: model.force_checkpoint(self.storage, run_id),
        )

    def _queue_complete(self) -> None:
        run_id = self._run_id()
        if run_id is None:
            self.message = "no run selected"
            return
        self.pending = (
            f"close run {run_id} as completed? (REVIEW_CONFIRMED + RUN_COMPLETED)",
            lambda: model.complete_run(self.storage, run_id),
        )

    def _queue_confirm(self) -> None:
        run_id = self._run_id()
        if run_id is None:
            self.message = "no run selected"
            return
        self.pending = (
            f"confirm the self-reported goal and progress of run {run_id}? (REVIEW_CONFIRMED)",
            lambda: model.confirm_state(self.storage, run_id),
        )

    def _queue_reconcile(self, *, occurred: bool) -> None:
        run_id = self._run_id()
        row = self._selected_action()
        if run_id is None or row is None:
            self.message = "select an action row first (cursor is on the actions list)"
            return
        if not row.uncertain:
            self.message = f"{row.key[:24]} is {row.status}; only uncertain actions can be settled"
            return
        self.pending = (
            f"settle {row.action_type} on run {run_id} as "
            f"{'OCCURRED' if occurred else 'NOT OCCURRED'}? (ACTION_RECONCILED)",
            lambda: model.reconcile_action(self.storage, run_id, row.key, occurred=occurred),
        )


_HELP_LINES = [
    "CONTINUUM tui keys",
    "",
    "  q            quit                     r        refresh the view",
    "  ?            toggle this help",
    "",
    "  runs list:   up/down or j/k move      enter/o  open the run",
    "               y confirm state          c        force a checkpoint",
    "               x complete the run",
    "",
    "  run detail:  left/right or 1-7 tabs   esc      back to the runs list",
    "               up/down move or scroll   y        confirm state",
    "               c force a checkpoint     x        complete the run",
    "  actions tab: y settle as occurred     n        settle as not occurred",
    "",
    "  Every mutating key asks first: the footer shows the exact write and",
    "  only a further y performs it. Anything else cancels. Reads never",
    "  write: refreshing and browsing are always safe on a live run.",
]


# --------------------------------------------------------------------------- #
# curses driver
# --------------------------------------------------------------------------- #


def _key_name(curses: Any, ch: int) -> str | None:
    """Map one curses key code to the plain name TuiApp understands."""
    special = {
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right",
        curses.KEY_ENTER: "enter",
        curses.KEY_RESIZE: "resize",
        curses.KEY_BACKSPACE: "esc",
        10: "enter",
        13: "enter",
        27: "esc",
    }
    if ch in special:
        return special[ch]
    if 0 < ch < 256:
        return chr(ch)
    return None


def _addline(screen: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write one line, clipping to the screen. The bottom-right cell raises
    on a full write, so failures are swallowed: a clipped dashboard beats a
    dead one."""
    with contextlib.suppress(Exception):
        screen.addnstr(y, x, text, screen.getmaxyx()[1] - x - 1, attr)


def _driver(curses: Any, screen: Any, app: TuiApp, refresh_seconds: float) -> int:
    """Draw, wait for one key, repeat. Auto-refresh ticks arrive as -1."""
    screen.keypad(True)
    # terminals without a cursor control still render fine
    with contextlib.suppress(Exception):
        curses.curs_set(0)
    screen.timeout(int(refresh_seconds * 1000) if refresh_seconds > 0 else -1)

    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        app.width = width  # the landing screen centres the logo on this
        _addline(screen, 0, 0, app.header(), curses.A_BOLD)
        body = app.body_lines()
        available = max(1, height - 3)
        if app.cursor >= 0:  # keep the selected line inside the window
            if app.cursor < app.scroll:
                app.scroll = app.cursor
            if app.cursor >= app.scroll + available:
                app.scroll = app.cursor - available + 1
        start = min(app.scroll, max(0, len(body) - available))
        for row, line in enumerate(body[start : start + available], start=start):
            attr = curses.A_REVERSE if row == app.cursor else 0
            _addline(screen, row - start + 2, 0, line, attr)
        _addline(screen, height - 1, 0, app.footer())
        screen.refresh()

        ch = screen.getch()
        if ch == -1:  # refresh tick (or no key yet on a nonblocking screen)
            app.refresh()
            continue
        key = _key_name(curses, ch)
        if key is None:
            continue
        if not app.handle_key(key):
            return ExitCode.OK


def run_tui(storage: Storage, *, refresh_seconds: float = 0.0, err: Any = None) -> int:
    """Open the full-screen dashboard; returns a process exit status.

    Refuses rather than half-rendering when curses is unavailable or stdout
    is not a terminal: the recovery data on screen deserves a whole screen,
    and a mangled scrape of one is worse than a clear refusal pointing at
    ``continuum dashboard``.
    """
    err = err if err is not None else sys.stderr
    try:
        import curses
    except ImportError:
        print(
            "error: the curses module is not available on this platform; "
            "use `continuum dashboard` for the browser dashboard",
            file=err,
        )
        return ExitCode.ERROR
    if not sys.stdout.isatty():
        print(
            "error: continuum tui needs an interactive terminal; stdout is not a TTY",
            file=err,
        )
        return ExitCode.ERROR
    try:
        return _run(curses, storage, refresh_seconds)
    except curses.error as exc:  # a terminal too small or too alien for curses
        print("error: this terminal cannot run the tui:", exc, file=err)
        print("use `continuum dashboard` for the browser dashboard", file=err)
        return ExitCode.ERROR


def _run(curses: Any, storage: Storage, refresh_seconds: float) -> int:
    """Enter curses mode; the wrapper restores the terminal on the way out."""

    def inner(screen: Any) -> int:
        return _driver(curses, screen, TuiApp(storage), refresh_seconds)

    result: int = curses.wrapper(inner)
    return result
