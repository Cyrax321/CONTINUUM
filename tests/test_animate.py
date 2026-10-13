"""Frame math for the landing splash animation (issue #1655).

``animate`` is a pure module: every function takes a monotonic frame counter
and returns a value, with no curses, storage, or side effects. The tests below
pin the two contracts the module docstring promises:

* a frame of ``SETTLED`` (negative) means "nothing moves" — every function
  resolves to its complete, unemphasised result;
* the functions never grow past their inputs, and the sheen band is always
  clamped inside ``[0, art_width)``.
"""

from __future__ import annotations

import pytest

from continuum.tui import animate
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

# --------------------------------------------------------------------------- #
# constants


def test_emphasis_flags_are_distinct_bits() -> None:
    assert {PLAIN, DIM, BRIGHT, ACCENT} == {0, 1, 2, 4}


def test_spinner_frames_match_the_private_tuple() -> None:
    assert frozenset(animate._SPINNER_FRAMES) == SPINNER_FRAMES
    assert len(SPINNER_FRAMES) == 10


# --------------------------------------------------------------------------- #
# animation_enabled


def test_animation_enabled_when_env_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTINUUM_NO_ANIMATION", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert animation_enabled() is True


def test_animation_disabled_when_env_var_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUUM_NO_ANIMATION", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert animation_enabled() is False


def test_animation_disabled_on_dumb_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTINUUM_NO_ANIMATION", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert animation_enabled() is False


# --------------------------------------------------------------------------- #
# typewriter


def test_typewriter_settled_returns_full_text() -> None:
    assert typewriter("hello", SETTLED) == "hello"


def test_typewriter_frame_zero_is_empty() -> None:
    assert typewriter("hello", 0) == ""


def test_typewriter_never_grows_past_the_text() -> None:
    assert typewriter("hi", 10_000) == "hi"


def test_typewriter_is_monotonic() -> None:
    text = "continuum"
    previous = ""
    for frame in range(0, 200):
        current = typewriter(text, frame)
        assert current.startswith(previous)
        previous = current


def test_typewriter_respects_chars_per_second() -> None:
    # visible = int(cps * TICK_SECONDS * frame)
    # frame=1 -> int(28 * 0.09 * 1) = int(2.52) = 2
    # frame=2 -> int(28 * 0.09 * 2) = int(5.04) = 5
    # frame=3 -> int(28 * 0.09 * 3) = int(7.56) = 7
    assert typewriter("abcdef", 1, chars_per_second=28.0) == "ab"
    assert typewriter("abcdef", 2, chars_per_second=28.0) == "abcde"
    assert typewriter("abcdef", 3, chars_per_second=28.0) == "abcdef"


# --------------------------------------------------------------------------- #
# cursor_visible


def test_cursor_hidden_when_settled() -> None:
    assert cursor_visible(SETTLED) is False


def test_cursor_blink_cycle() -> None:
    # On when frame % 16 in [0, 8], off when in [9, 15].
    for frame in range(0, 9):
        assert cursor_visible(frame) is True, frame
    for frame in range(9, 16):
        assert cursor_visible(frame) is False, frame
    assert cursor_visible(16) is True   # 16 % 16 == 0
    assert cursor_visible(25) is False  # 25 % 16 == 9
    assert cursor_visible(31) is False  # 31 % 16 == 15
    assert cursor_visible(32) is True   # 32 % 16 == 0


# --------------------------------------------------------------------------- #
# shimmer_span


def test_shimmer_settled_returns_none() -> None:
    assert shimmer_span(SETTLED, art_width=40) is None


@pytest.mark.parametrize("art_width", [0, -1, -100])
def test_shimmer_no_room_when_width_is_not_positive(art_width: int) -> None:
    assert shimmer_span(0, art_width=art_width) is None


def test_shimmer_returns_none_when_art_is_narrower_than_band() -> None:
    assert shimmer_span(0, art_width=8, band=8) is None
    assert shimmer_span(0, art_width=4, band=8) is None


def test_shimmer_band_is_clamped_inside_art() -> None:
    art_width, band = 40, 8
    for frame in range(0, 120):
        span = shimmer_span(frame, art_width=art_width, band=band)
        assert span is not None
        start, end = span
        assert start >= 0
        assert end <= art_width
        assert end - start == band


def test_shimmer_is_left_to_right_then_back() -> None:
    art_width, band = 40, 8
    half = max(1, round(1.8 / TICK_SECONDS))
    forward = [
        shimmer_span(frame, art_width=art_width, band=band)[0]  # type: ignore[index]
        for frame in range(0, half)
    ]
    assert forward == sorted(forward), "forward half must sweep left-to-right"
    backward = [
        shimmer_span(frame, art_width=art_width, band=band)[0]  # type: ignore[index]
        for frame in range(half, 2 * half)
    ]
    assert backward == sorted(backward, reverse=True), "return half must sweep right-to-left"


# --------------------------------------------------------------------------- #
# pulse


def test_pulse_settled_returns_plain() -> None:
    assert pulse(SETTLED) == PLAIN


def test_pulse_only_returns_bright_or_dim() -> None:
    for frame in range(0, 200):
        assert pulse(frame) in {BRIGHT, DIM}


def test_pulse_breathes_within_a_cycle() -> None:
    # period 1.6 s / 0.09 s per tick -> round(17.77...) == 18 frames per cycle.
    frames_per_cycle = max(2, round(1.6 / TICK_SECONDS))
    assert frames_per_cycle == 18
    seen = {pulse(frame) for frame in range(frames_per_cycle)}
    assert seen == {BRIGHT, DIM}


# --------------------------------------------------------------------------- #
# spinner


def test_spinner_settled_returns_space() -> None:
    assert spinner(SETTLED) == " "


def test_spinner_cycles_through_frames() -> None:
    for frame in range(0, 40):
        assert spinner(frame) in SPINNER_FRAMES


def test_spinner_wraps_around() -> None:
    assert spinner(0) == spinner(len(animate._SPINNER_FRAMES))
    assert spinner(3) == spinner(3 + len(animate._SPINNER_FRAMES))
