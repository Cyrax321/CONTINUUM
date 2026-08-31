"""Colour must be purely presentational.

The guarantee under test: enabling colour changes how output *looks* and
nothing else. Piped output carries no escape sequences, JSON is never
decorated, and exit codes are identical in every colour mode. A stray escape
in a log file or a `grep` would be a real bug, so the default is
colour-off-unless-proven-safe.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.cli import main
from continuum.cli.colour import Palette, should_colour
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage

ANSI = re.compile(r"\033\[[0-9;]*m")


class FakeTTY(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def db(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "demo.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        store.append_event(
            "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
        )
        store.append_event(
            "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
        )
        for i in range(20):
            store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})
        CheckpointManager(store).checkpoint(
            "run_1", environment=capture("run_1", StaticProvider(dataset="v3"))
        )
    yield path


def run(*argv: str, tty: bool = False) -> tuple[int, str, str]:
    out = FakeTTY() if tty else io.StringIO()
    err = io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# --- when colour is suppressed --------------------------------------------- #


def test_piped_output_carries_no_escape_sequences(db: str) -> None:
    for argv in (
        ("inspect", "run_1"),
        ("resume", "run_1", "--env", "dataset=v4"),
        ("verify", "run_1"),
        ("history", "run_1"),
        ("actions", "run_1"),
    ):
        _, out, _ = run("--db", db, *argv)
        assert not ANSI.search(out), f"{argv[0]} leaked colour into piped output"


def test_no_color_env_var_is_respected(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    _, out, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v4", tty=True)
    assert not ANSI.search(out)


def test_no_color_wins_even_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """no-color.org: presence is what counts, not the value."""
    monkeypatch.setenv("NO_COLOR", "")
    assert not should_colour(FakeTTY())


def test_dumb_terminals_get_no_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert not should_colour(FakeTTY())


def test_the_no_color_flag_overrides_a_terminal(db: str) -> None:
    _, out, _ = run("--db", db, "--no-color", "inspect", "run_1", tty=True)
    assert not ANSI.search(out)


def test_json_is_never_colourised(db: str) -> None:
    """An escape sequence in JSON is a parse error, not a decoration."""
    _, out, _ = run("--db", db, "--json", "--color", "inspect", "run_1", tty=True)
    assert not ANSI.search(out)
    json.loads(out)


def test_emit_routes_json_around_the_colouriser_even_with_a_live_palette() -> None:
    """The second, independent guard.

    JSON is protected twice: the palette is disabled for --json, *and* _emit
    never passes the JSON branch through the colouriser. Either alone suffices,
    so this asserts the lower layer directly — otherwise a refactor could drop
    it while the upper guard masked the loss.
    """
    from continuum.cli.main import _emit

    stream = io.StringIO()
    _emit(
        {"status": "valid", "safe": True},
        "[ok] this text would be coloured",
        as_json=True,
        stream=stream,
        palette=Palette(enabled=True),
    )
    written = stream.getvalue()
    assert not ANSI.search(written)
    assert json.loads(written) == {"status": "valid", "safe": True}


def test_a_non_stream_object_gets_no_colour() -> None:
    assert not should_colour(object())


def test_a_closed_stream_does_not_raise() -> None:
    class Closed:
        def isatty(self) -> bool:
            raise ValueError("I/O operation on closed file")

    assert not should_colour(Closed())


# --- when colour is enabled ------------------------------------------------ #


def test_a_terminal_gets_colour(db: str) -> None:
    _, out, _ = run("--db", db, "resume", "run_1", "--env", "dataset=v4", tty=True)
    assert ANSI.search(out)


def test_the_color_flag_forces_it_through_a_pipe(db: str) -> None:
    """For `continuum resume run | less -R`."""
    _, out, _ = run("--db", db, "--color", "resume", "run_1", "--env", "dataset=v4")
    assert ANSI.search(out)


# --- the core guarantee ---------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ("inspect", "run_1"),
        ("resume", "run_1", "--env", "dataset=v4"),
        ("resume", "run_1", "--env", "dataset=v3"),
        ("validate", "run_1", "--env", "dataset=v4"),
        ("verify", "run_1"),
        ("history", "run_1"),
        ("actions", "run_1"),
        ("show-contract", "run_1"),
        ("runs",),
    ],
)
def test_stripping_colour_reproduces_plain_output_exactly(db: str, argv: tuple[str, ...]) -> None:
    """Colour is presentational: strip the codes and you get the plain text."""
    _, plain, _ = run("--db", db, *argv)
    _, coloured, _ = run("--db", db, "--color", *argv)
    assert _normalize_liveness(ANSI.sub("", coloured)) == _normalize_liveness(plain)


@pytest.mark.parametrize(
    "argv",
    [
        ("resume", "run_1", "--env", "dataset=v4"),
        ("resume", "run_1", "--env", "dataset=v3"),
        ("verify", "run_1"),
        ("inspect", "ghost"),
        ("actions", "run_1"),
    ],
)
def test_exit_codes_are_identical_in_every_colour_mode(db: str, argv: tuple[str, ...]) -> None:
    """Colour must never influence the safety contract."""
    plain, _, _ = run("--db", db, *argv)
    forced, _, _ = run("--db", db, "--color", *argv)
    disabled, _, _ = run("--db", db, "--no-color", *argv)
    terminal, _, _ = run("--db", db, *argv, tty=True)
    assert plain == forced == disabled == terminal


def test_colour_and_no_colour_cannot_both_be_given(db: str) -> None:
    with pytest.raises(SystemExit):
        main(["--db", db, "--color", "--no-color", "runs"], out=io.StringIO(), err=io.StringIO())


# --- palette behaviour ------------------------------------------------------ #


def test_a_disabled_palette_returns_text_untouched() -> None:
    palette = Palette(enabled=False)
    for method in (palette.ok, palette.warn, palette.bad, palette.bold, palette.heading):
        assert method("hello") == "hello"


def test_an_enabled_palette_wraps_and_resets() -> None:
    palette = Palette(enabled=True)
    coloured = palette.ok("hello")
    assert coloured.startswith("\033[")
    assert coloured.endswith("\033[0m")
    assert ANSI.sub("", coloured) == "hello"


def test_empty_strings_are_not_wrapped() -> None:
    assert Palette(enabled=True).ok("") == ""


def test_states_map_to_intuitive_colours() -> None:
    palette = Palette(enabled=True)
    assert palette.status("x", "valid") == palette.green("x")
    assert palette.status("x", "stale") == palette.yellow("x")
    assert palette.status("x", "conflicted") == palette.red("x")


def test_an_unclassified_state_is_left_uncoloured() -> None:
    """A state nobody has classified should not be dressed up as reassuring."""
    palette = Palette(enabled=True)
    assert palette.status("x", "some_future_state") == "x"


def test_every_palette_colour_round_trips() -> None:
    palette = Palette(enabled=True)
    for method in (
        palette.red,
        palette.green,
        palette.yellow,
        palette.blue,
        palette.cyan,
        palette.grey,
        palette.bold,
        palette.dim,
        palette.heading,
    ):
        assert ANSI.sub("", method("text")) == "text"


def test_for_stream_infers_from_the_stream() -> None:
    assert Palette.for_stream(FakeTTY()).enabled
    assert not Palette.for_stream(io.StringIO()).enabled
    assert Palette.for_stream(io.StringIO(), force=True).enabled
    assert not Palette.for_stream(FakeTTY(), force=False).enabled


# --- as a real process ------------------------------------------------------ #


def _normalize_liveness(text: str) -> str:
    """Normalize wall-clock liveness line for byte-equality checks.

    Liveness is ``now - last_event_ts`` against a cadence contract, so two
    back-to-back renders legitimately differ by a few hundred milliseconds.
    The invariant under test is colour presentationality, not clock stability,
    so normalize the variable ``silence X.Xs`` fragment before comparing.
    Breach status and threshold remain asserted elsewhere.
    """
    return re.sub(r"silence \d+(?:\.\d+)?s", "silence X.Xs", text)


def test_a_real_piped_process_emits_no_colour(db: str) -> None:
    # The uncoloured in-process render is the reference: a real piped process
    # must produce it byte for byte, exit code included.
    expected_code, expected_out, _ = run("--db", db, "resume", "run_1")

    result = subprocess.run(
        [sys.executable, "-m", "continuum.cli", "--db", db, "resume", "run_1"],
        # Inherit the parent environment; only PYTHONPATH is added, so the
        # subprocess imports continuum from src/. A bare env= drops SystemRoot on
        # Windows and the process dies on `import _overlapped` during startup.
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True,
        text=True,
    )
    # Assert the CLI actually ran. Checking only for absent escape sequences
    # would pass for a process that died before producing any output at all.
    assert result.returncode == expected_code, result.stderr
    assert _normalize_liveness(result.stdout) == _normalize_liveness(expected_out)
    assert not ANSI.search(result.stdout)
    assert not ANSI.search(result.stderr)
