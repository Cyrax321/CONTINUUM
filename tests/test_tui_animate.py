"""Frame math for the landing splash animation (issue #1655).

Pure functions of a frame counter: no terminal, no storage, no sleeping. The
contract that makes them safe to test is the one in the module docstring. A
frame below zero is "settled", and every function resolves to its complete,
unemphasised result at ``SETTLED``.

The module is currently orphaned. Nothing in the package imports it. These tests
pin its behaviour so that wiring it up later is a choice rather than a
rewrite.
"""

from __future__ import annotations

import pytest

from continuum.tui.animate import (
    ACCENT,
    BRIGHT,
    DIM,
    PLAIN,
    SETTLED,
    SPINNER_FRAMES,
    TICK_SECONDS,
    animation_enabled,
    cursor_visible,
    pulse,
    shimmer_span,
    spinner,
    typewriter,
)

LINE = "verifiable semantic recovery"


# --- the exported constants ------------------------------------------------- #


def test_the_emphasis_flags_are_distinct_bits() -> None:
    assert PLAIN == 0
    assert DIM == 1
    assert BRIGHT == 2
    assert ACCENT == 4


def test_the_emphasis_flags_do_not_overlap() -> None:
    flags = [DIM, BRIGHT, ACCENT]

    for i, left in enumerate(flags):
        for right in flags[i + 1 :]:
            assert left & right == 0


def test_settled_is_negative_so_one_sentinel_composes_with_every_function() -> None:
    assert SETTLED < 0


def test_a_tick_is_a_ninetieth_of_a_second() -> None:
    assert TICK_SECONDS == 0.09


def test_the_spinner_frames_are_a_non_empty_cycle() -> None:
    assert len(SPINNER_FRAMES) == 10


# --- animation_enabled ------------------------------------------------------ #


def test_animation_is_on_without_the_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTINUUM_NO_ANIMATION", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert animation_enabled() is True


@pytest.mark.parametrize("value", ["1", "", "false", "no"])
def test_the_opt_out_wins_over_any_value(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CONTINUUM_NO_ANIMATION", value)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert animation_enabled() is False


def test_a_dumb_terminal_gets_no_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTINUUM_NO_ANIMATION", raising=False)
    monkeypatch.setenv("TERM", "DUMB")

    assert animation_enabled() is False


def test_a_missing_term_does_not_read_as_dumb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTINUUM_NO_ANIMATION", raising=False)
    monkeypatch.delenv("TERM", raising=False)

    assert animation_enabled() is True


# --- typewriter ------------------------------------------------------------- #


def test_frame_zero_types_nothing() -> None:
    assert typewriter(LINE, 0) == ""


def test_the_line_types_in_over_time() -> None:
    early = typewriter(LINE, 2)
    later = typewriter(LINE, 6)

    assert len(early) < len(later) <= len(LINE)
    assert LINE.startswith(later)


def test_the_line_never_grows_past_the_whole_text() -> None:
    assert typewriter(LINE, 10_000) == LINE


def test_the_line_settles_on_the_whole_text() -> None:
    assert typewriter(LINE, SETTLED) == LINE


def test_a_slow_cadence_types_less_per_frame() -> None:
    fast = typewriter(LINE, 10, chars_per_second=90.0)
    slow = typewriter(LINE, 10, chars_per_second=5.0)

    assert len(slow) < len(fast)


# --- cursor_visible --------------------------------------------------------- #


def test_the_cursor_is_hidden_when_settled() -> None:
    assert cursor_visible(SETTLED) is False


def test_the_cursor_is_on_at_frame_zero() -> None:
    assert cursor_visible(0) is True


def test_the_cursor_blinks_rather_than_staying_on() -> None:
    readings = {cursor_visible(frame) for frame in range(16)}

    assert readings == {True, False}


def test_the_cursor_repeats_on_its_period() -> None:
    assert [cursor_visible(f) for f in range(16)] == [cursor_visible(f + 16) for f in range(16)]


# --- shimmer_span ----------------------------------------------------------- #


def test_there_is_no_sheen_when_settled() -> None:
    assert shimmer_span(SETTLED, art_width=40) is None


def test_there_is_no_sheen_when_the_art_has_no_width() -> None:
    assert shimmer_span(0, art_width=0) is None


def test_there_is_no_sheen_when_the_art_is_narrower_than_the_band() -> None:
    assert shimmer_span(0, art_width=6, band=8) is None


def test_the_sheen_stays_inside_the_art_at_every_frame() -> None:
    width = 40

    for frame in range(120):
        span = shimmer_span(frame, art_width=width)
        assert span is not None
        start, end = span
        assert 0 <= start < width
        assert start < end <= width


def test_the_sheen_is_the_requested_width_when_there_is_room() -> None:
    span = shimmer_span(3, art_width=40, band=8)

    assert span is not None
    start, end = span
    assert end - start == 8


def test_the_sheen_ping_pongs_instead_of_snapping_back() -> None:
    width = 40
    starts = [shimmer_span(frame, art_width=width) for frame in range(120)]
    positions = [span[0] for span in starts if span is not None]

    # A ping-pong goes right and comes back left; a wrap would teleport from the
    # right edge to the left edge in one frame.
    assert positions != sorted(positions)
    assert max(positions) - min(positions) > 8


def test_the_sheen_repeats_on_its_period() -> None:
    period = 40

    assert shimmer_span(5, art_width=40) == shimmer_span(5 + period, art_width=40)


def test_a_settled_sheen_is_the_same_for_every_frame() -> None:
    assert shimmer_span(SETTLED, art_width=40) == shimmer_span(SETTLED - 5, art_width=40)


# --- pulse ------------------------------------------------------------------ #


def test_pulse_settles_on_plain() -> None:
    assert pulse(SETTLED) == PLAIN


def test_pulse_alternates_dim_and_bright() -> None:
    readings = {pulse(frame) for frame in range(60)}

    assert readings == {DIM, BRIGHT}


def test_pulse_repeats_on_its_period() -> None:
    cycle = max(2, round(1.6 / TICK_SECONDS))

    assert [pulse(f) for f in range(cycle)] == [pulse(f + cycle) for f in range(cycle)]


def test_a_longer_period_holds_each_emphasis_longer() -> None:
    def switches(period: float) -> int:
        return sum(1 for f in range(120) if pulse(f, period=period) != pulse(f - 1, period=period))

    assert switches(8.0) < switches(1.6)


# --- spinner ---------------------------------------------------------------- #


def test_the_spinner_is_blank_when_settled() -> None:
    assert spinner(SETTLED) == " "


def test_the_spinner_cycles_every_frame() -> None:
    frames = {spinner(frame) for frame in range(10)}

    assert frames == set(SPINNER_FRAMES)


def test_the_spinner_repeats_after_ten_frames() -> None:
    assert spinner(11) == spinner(1)


def test_a_negative_frame_other_than_settled_is_still_blank() -> None:
    assert spinner(-4) == " "


def test_the_spinner_frame_is_never_the_settled_blank() -> None:
    assert all(spinner(frame) != " " for frame in range(10))
