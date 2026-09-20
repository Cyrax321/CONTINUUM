"""Frame math for the landing splash animation.

Everything here is a pure function of a monotonic frame counter. No curses, no
storage, no side effects, so the animation is unit-testable without a terminal,
the same contract as the rest of the TUI's state machine.

The emphasis flags are presentation only: they never change *what* a line says,
only how it is emphasised. That is the rule the CLI's ``Palette`` already follows
for colour, and it is why ``body_lines`` keeps returning plain strings while
``body_attrs`` describes the emphasis separately.

A frame of ``SETTLED`` means "nothing moves": every function resolves to its
complete, unemphasised result. ``CONTINUUM_NO_ANIMATION`` pins the splash there.
"""

from __future__ import annotations

import os

__all__ = [
    "ACCENT",
    "BRIGHT",
    "DIM",
    "PLAIN",
    "SETTLED",
    "SPINNER_FRAMES",
    "animation_enabled",
    "pulse",
    "shimmer_span",
    "spinner",
    "typewriter",
]

#: Emphasis flags, OR'd together; the driver maps them to terminal attributes.
PLAIN = 0
DIM = 1 << 0
BRIGHT = 1 << 1
ACCENT = 1 << 2  # colour, where the terminal can render it

#: The frame the splash rests on when animation is off. Negative on purpose:
#: every function below treats "frame < 0" as "settled", so a single sentinel
#: composes with all of them.
SETTLED = -1

#: Seconds per animation frame. The driver uses this as its ``getch`` timeout on
#: the landing screen, giving ~11 fps without a store read per tick.
TICK_SECONDS = 0.09

_NO_ANIMATION_ENV = "CONTINUUM_NO_ANIMATION"
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
SPINNER_FRAMES = frozenset(_SPINNER_FRAMES)


def animation_enabled() -> bool:
    """Whether the splash may move.

    Off when ``CONTINUUM_NO_ANIMATION`` is set to anything, mirroring the
    CLI's ``NO_COLOR`` rule, and off on ``TERM=dumb``: redrawing an identical
    frame there would only burn CPU, since dumb terminals cannot render the
    emphasis that makes the redraw visible.
    """
    if os.environ.get(_NO_ANIMATION_ENV) is not None:
        return False
    return os.environ.get("TERM", "").lower() != "dumb"


def typewriter(text: str, frame: int, *, chars_per_second: float = 28.0) -> str:
    """The prefix of ``text`` visible at ``frame``.

    Settles on the whole line and never grows past it, so the completed splash
    is exactly the static one. Frame 0 is the empty start: the line types in
    from nothing over roughly a second and a half.
    """
    if frame < 0:
        return text
    visible = int(chars_per_second * TICK_SECONDS * frame)
    return text[: max(0, min(visible, len(text)))]


def cursor_visible(frame: int) -> bool:
    """Whether the typing cursor blinks on at ``frame``.

    Blinking is the only motion that survives a monochrome terminal, so it is
    kept independent of the colour emphasis.
    """
    if frame < 0:
        return False
    # a ~0.7s blink, on slightly longer than off, so the cursor reads as a
    # cursor rather than a flicker
    return (frame % 16) < 9


def shimmer_span(
    frame: int, *, art_width: int, band: int = 8, period: float = 1.8
) -> tuple[int, int] | None:
    """The ``(start, end)`` columns of the sheen band sweeping the art.

    The band ping-pongs rather than jumping back to the left edge, and the
    result is always clamped inside ``[0, art_width)``: the sheen is emphasis
    on the logo, never a glyph of its own. ``None`` when there is no room for a
    sweep, or when the frame is settled.
    """
    if frame < 0 or art_width <= 0:
        return None
    span = art_width - band
    if span <= 0:  # art narrower than the band: nothing to sweep
        return None
    frames_per_sweep = max(1, round(period / TICK_SECONDS))
    half = frames_per_sweep
    step = frame % (2 * half)
    if step >= half:
        step = 2 * half - 1 - step
    progress = step / max(1, half - 1)  # 0 at the left, 1 at the right
    start = round(progress * span)
    return (start, min(start + band, art_width))


def pulse(frame: int, *, period: float = 1.6) -> int:
    """Emphasis for a line that breathes: ``DIM`` and ``BRIGHT`` by turns.

    Settles on ``PLAIN``. An animation-free screen shows the line exactly as
    it rendered before animation existed.
    """
    if frame < 0:
        return PLAIN
    frames_per_cycle = max(2, round(period / TICK_SECONDS))
    return BRIGHT if (frame % frames_per_cycle) * 2 < frames_per_cycle else DIM


def spinner(frame: int) -> str:
    """One braille frame of an in-progress indicator.

    Held in reserve: it is only honest to show a spinner for a read that is
    genuinely in flight. Landing reads today are synchronous, so the splash
    shows real counts instead of a pretend one.
    """
    return _SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)] if frame >= 0 else " "
