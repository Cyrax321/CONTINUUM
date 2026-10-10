"""Rendering and fingerprinting an instruction target.

A generated file has to be recognisable, and the recognition has to survive
someone editing the body. So the banner is three lines: a machine marker the
generator looks for, the command that regenerates the file, and a digest of
the source it was rendered from. The digest is what turns drift from a habit
into a test failure (:func:`continuum.agents.generator.check`).

Nothing rendered here carries a timestamp. A timestamp would make every
regeneration a diff and would break the round-trip guarantee the tests pin,
and the source digest already answers the only question a timestamp would be
asked.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .targets import InstructionTarget

__all__ = [
    "GENERATED_MARKER",
    "Fingerprint",
    "fingerprint_of",
    "normalize_source",
    "render",
    "source_digest",
]

#: The literal a rendered file must contain before the generator will touch it
#: again. Absent it, the file belongs to someone else.
GENERATED_MARKER = "continuum:generated"

#: Prefix of the line carrying the source digest.
_SOURCE_LINE = re.compile(r"<!--\s*continuum-source-sha256:\s*(?P<digest>[0-9a-f]{64})\s*-->")
_COMMAND_LINE = re.compile(r"<!--\s*Regenerate with `(?P<command>[^`]+)`\s*-->")

#: The YAML fence Cursor's ``.mdc`` rules format requires.
_FENCE = "---"


@dataclass(frozen=True)
class Fingerprint:
    """What a rendered file says about itself."""

    source_sha256: str
    command: str | None


def normalize_source(text: str) -> str:
    """Normalise source text so the digest measures content, not whitespace.

    Trailing blank lines and CRLF endings are transport details: a checkout on
    Windows must not report every target as drifted. Trailing spaces on a line
    are normalised too, since no Markdown renderer shows them.
    """

    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def source_digest(text: str) -> str:
    """The SHA-256 of ``text`` as :func:`normalize_source` leaves it."""

    return hashlib.sha256(normalize_source(text).encode("utf-8")).hexdigest()


def render(target: InstructionTarget, source_text: str, *, command: str) -> str:
    """Render ``source_text`` as ``target``, banner and all.

    Pure: the same inputs always produce the same bytes, which is what makes
    ``check`` able to call a committed copy stale.
    """

    body = normalize_source(source_text)
    digest = source_digest(source_text)
    blocks: list[str] = []
    if target.frontmatter:
        blocks.append(
            _FENCE
            + "\n"
            + "".join(f"{key}: {value}\n" for key, value in target.frontmatter)
            + _FENCE
        )
    blocks.append(
        "\n".join(
            (
                f"<!-- {GENERATED_MARKER} -->",
                "<!-- Generated file. Do not edit by hand. -->",
                f"<!-- Regenerate with `{command}` -->",
                f"<!-- continuum-source-sha256: {digest} -->",
            )
        )
    )
    blocks.append(body)
    return "\n".join(blocks)


def fingerprint_of(text: str) -> Fingerprint | None:
    """Read the fingerprint out of ``text``, or ``None`` if it carries none.

    Both the marker and the digest are required. The marker alone would let a
    file that merely mentions this generator be adopted and overwritten, and
    the digest alone is a hash that any file could happen to contain.
    """

    if GENERATED_MARKER not in text:
        return None
    match = _SOURCE_LINE.search(text)
    if match is None:
        return None
    command_match = _COMMAND_LINE.search(text)
    return Fingerprint(
        source_sha256=match.group("digest"),
        command=command_match.group("command") if command_match else None,
    )
