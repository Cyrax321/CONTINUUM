"""Terminal colour, applied only when it is safe to do so.

Colour is presentation. It must never change *what* the CLI says, only how it
looks, so this module wraps text in ANSI codes and nothing else. Exit codes,
JSON payloads and the wording of every line are untouched.

When colour is suppressed
-------------------------

* the stream is not a TTY (piped, redirected, captured in a test)
* ``NO_COLOR`` is set to anything, per https://no-color.org
* ``TERM=dumb``
* ``--no-color`` was passed

``--color`` forces it on for the case where you *do* want codes through a pipe,
such as ``continuum resume run | less -R``.

The default matters: piped output is the machine-readable path, and a stray
escape sequence in a log or a ``grep`` is a real bug. So the rule is
colour-off-unless-proven-safe, not the reverse.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["Palette", "should_colour"]

_RESET = "\033[0m"
_CODES = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
    "bold": "\033[1m",
    "dim": "\033[2m",
}


def should_colour(stream: Any, *, force: bool | None = None) -> bool:
    """Decide whether ``stream`` may carry ANSI codes.

    ``force`` short-circuits the environment checks: ``True`` for ``--color``,
    ``False`` for ``--no-color``.
    """
    if force is not None:
        return force
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):  # closed or detached stream
        return False


class Palette:
    """Applies colour, or passes text through untouched.

    Every method returns the input unchanged when colour is disabled, so call
    sites need no conditionals and cannot accidentally emit codes.
    """

    __slots__ = ("enabled",)

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    @classmethod
    def for_stream(cls, stream: Any, *, force: bool | None = None) -> Palette:
        """Construct a palette enabled or suppressed for ``stream``."""
        return cls(should_colour(stream, force=force))

    def _wrap(self, text: str, code: str) -> str:
        if not self.enabled or not text:
            return text
        return f"{code}{text}{_RESET}"

    def red(self, text: str) -> str:
        """Wrap text in red ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["red"])

    def green(self, text: str) -> str:
        """Wrap text in green ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["green"])

    def yellow(self, text: str) -> str:
        """Wrap text in yellow ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["yellow"])

    def blue(self, text: str) -> str:
        """Wrap text in blue ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["blue"])

    def cyan(self, text: str) -> str:
        """Wrap text in cyan ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["cyan"])

    def grey(self, text: str) -> str:
        """Wrap text in grey ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["grey"])

    def bold(self, text: str) -> str:
        """Wrap text in bold ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["bold"])

    def dim(self, text: str) -> str:
        """Wrap text in dim ANSI escape codes when colour is enabled."""
        return self._wrap(text, _CODES["dim"])

    def heading(self, text: str) -> str:
        """Wrap text in bold cyan ANSI escape codes for section headings."""
        return self._wrap(text, _CODES["bold"] + _CODES["cyan"])

    # -- domain-aware helpers -------------------------------------------- #

    def ok(self, text: str) -> str:
        """A verified, trustworthy result."""
        return self.green(text)

    def warn(self, text: str) -> str:
        """Something recoverable that needs attention."""
        return self.yellow(text)

    def bad(self, text: str) -> str:
        """Something unsafe, invalid or blocking."""
        return self.red(text)

    def status(self, text: str, state: str) -> str:
        """Colour by state name, e.g. ``valid``, ``stale``, ``conflicted``.

        Unrecognised states are left uncoloured rather than guessed at: a state
        nobody has classified should not be dressed up as reassuring green.
        """
        state = state.lower()
        if state in ("valid", "completed", "granted", "safe_to_resume", "resume"):
            return self.ok(text)
        if state in ("stale", "requires_review", "unknown", "pending", "started"):
            return self.warn(text)
        if state in (
            "invalid",
            "conflicted",
            "expired",
            "failed",
            "revoked",
            "unsafe",
            "blocked",
        ):
            return self.bad(text)
        return text
