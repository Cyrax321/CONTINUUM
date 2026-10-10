"""Install, check and remove generated instruction targets.

The contract this module keeps is the same one
:func:`continuum.clienthooks._install_hook` and
:func:`continuum.mcp.install._load_object` keep, for the same reason. A file a
human wrote is a statement of intent. Saving someone a reformat is not worth
destroying it over, so a target that exists without our fingerprint is
refused, not overwritten, and ``remove`` deletes only what the fingerprint
says we wrote.

The third half is the drift check. Refusing to overwrite keeps two writers
apart, but it does nothing about one writer drifting: an edited instruction
file, or a source that changed without the target being regenerated. So every
rendered file carries the digest of the source it came from, and
:func:`check` fails when the committed bytes no longer match what the current
source renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .render import fingerprint_of, render, source_digest
from .targets import InstructionTarget, target_for

__all__ = [
    "CheckResult",
    "DEFAULT_SOURCE",
    "InstallResult",
    "REGENERATE_COMMAND",
    "check",
    "install",
    "remove",
    "resolve_source",
]

#: The shipped instruction source, used unless the caller names another one.
DEFAULT_SOURCE = Path(__file__).with_name("instructions.md")

#: The command every rendered banner names, so a reader who edits the file is
#: told the exact thing to run rather than left to guess.
REGENERATE_COMMAND = "continuum agents install"


@dataclass(frozen=True)
class InstallResult:
    """What an install did to one target."""

    target: InstructionTarget
    path: Path
    status: str
    """``installed``, ``updated`` or ``present``."""


@dataclass(frozen=True)
class CheckResult:
    """Whether one target still matches the source it was rendered from."""

    target: InstructionTarget
    path: Path
    state: str
    """``current``, ``drifted``, ``unmanaged`` or ``absent``."""

    detail: str


def resolve_source(source: Path | None) -> tuple[Path, str]:
    """Return the ``(path, text)`` of the instruction source to render.

    A source that does not exist raises rather than rendering an empty file.
    Rendering nothing would look like a successful install of no guidance,
    which is the one outcome a user cannot notice.
    """

    path = DEFAULT_SOURCE if source is None else source
    if not path.is_file():
        raise FileNotFoundError(f"instruction source not found: {path}")
    return path, path.read_text(encoding="utf-8")


def _render_for(entry: InstructionTarget, text: str) -> str:
    return render(entry, text, command=f"{REGENERATE_COMMAND} --target {entry.id}")


def _entry(target: str | InstructionTarget) -> InstructionTarget:
    return target if isinstance(target, InstructionTarget) else target_for(target)


def install(
    target: str | InstructionTarget,
    *,
    root: Path,
    source: Path | None = None,
) -> InstallResult:
    """Render ``target`` under ``root``, refusing to clobber a hand-written file.

    Returns an :class:`InstallResult`. Raises ``ValueError`` when the target
    exists and carries no fingerprint of ours, and ``FileNotFoundError`` when
    the source is missing.
    """

    entry = _entry(target)
    _, text = resolve_source(source)
    destination = root / entry.path
    rendered = _render_for(entry, text)

    if destination.exists():
        existing = destination.read_text(encoding="utf-8")
        if fingerprint_of(existing) is None:
            raise ValueError(
                f"{destination} already exists and was not written by "
                f"`{REGENERATE_COMMAND}`; refusing to overwrite it. Merge the "
                "guidance into your own instruction source by hand, or delete "
                "the file first if you are sure it is disposable."
            )
        if existing == rendered:
            return InstallResult(entry, destination, "present")
        destination.write_text(rendered, encoding="utf-8")
        return InstallResult(entry, destination, "updated")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    return InstallResult(entry, destination, "installed")


def remove(target: str | InstructionTarget, *, root: Path) -> bool:
    """Delete ``target`` only when the fingerprint says we wrote it.

    True when a file went. A hand-written target is left alone and reported as
    untouched, which is the whole point of the fingerprint check.
    """

    entry = _entry(target)
    destination = root / entry.path
    if not destination.exists():
        return False
    if fingerprint_of(destination.read_text(encoding="utf-8")) is None:
        return False
    destination.unlink()
    return True


def check(
    target: str | InstructionTarget, *, root: Path, source: Path | None = None
) -> CheckResult:
    """Report whether a committed target still matches the current source.

    ``drifted`` is the interesting one: the file carries our fingerprint but
    does not match a fresh render, which is exactly the committed-copy-drift
    case this exists to catch. ``unmanaged`` means the file is there and is
    not ours, which is a conflict rather than drift, and it is reported
    separately so nobody is told to regenerate a file they wrote.
    """

    entry = _entry(target)
    destination = root / entry.path
    if not destination.exists():
        return CheckResult(entry, destination, "absent", "not generated")

    existing = destination.read_text(encoding="utf-8")
    marks = fingerprint_of(existing)
    if marks is None:
        return CheckResult(
            entry,
            destination,
            "unmanaged",
            "exists but was not written by this generator",
        )

    _, text = resolve_source(source)
    if existing == _render_for(entry, text):
        return CheckResult(entry, destination, "current", "matches the source")

    recorded, current = marks.source_sha256, source_digest(text)
    if recorded == current:
        detail = "the source digest still matches, but the body was edited by hand"
    else:
        detail = f"the source changed since this was rendered ({recorded[:12]} vs {current[:12]})"
    return CheckResult(
        entry,
        destination,
        "drifted",
        f"{detail}; run `{REGENERATE_COMMAND} --target {entry.id}`",
    )
