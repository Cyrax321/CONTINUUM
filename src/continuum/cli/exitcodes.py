"""Exit codes, chosen so shell pipelines are safe by default.

A recovery tool is most often invoked from automation::

    continuum resume "$RUN" && ./start-agent.sh

If the exit code did not reflect whether resuming is *safe*, that line would
launch an agent onto stale state or an unreconciled side effect. So the rule is
absolute: **only a fully verified, safe-to-resume run exits 0.** Every other
outcome, repairable, uncertain, blocked, missing, corrupted, is non-zero, and
the `&&` short-circuits.

Distinct codes let a script react proportionately (retry a repair, page a human
on an unknown side effect) without parsing text. Text output is for people;
exit codes and ``--json`` are for machines.
"""

from __future__ import annotations

from continuum.models import RecoveryMode

__all__ = ["ExitCode", "exit_code_for"]


class ExitCode:
    """Meaningful process exit statuses."""

    OK = 0
    """Verified safe. The only code that permits a pipeline to continue."""

    ERROR = 1
    """Usage error or unexpected failure."""

    NOT_FOUND = 2
    """No such run, version or checkpoint."""

    CORRUPTED = 3
    """Stored data failed an integrity check."""

    NOT_IMPLEMENTED = 4
    """A command that exists in the roadmap but not yet in the build."""

    REQUIRES_REPAIR = 10
    """State is recoverable but must be repaired first."""

    REQUIRES_HUMAN = 20
    """A person must decide, typically an unreconciled side effect."""

    UNSAFE = 30
    """Resuming is not safe at all."""


_MODE_CODES: dict[RecoveryMode, int] = {
    RecoveryMode.RESUME: ExitCode.OK,
    RecoveryMode.REPAIR_AND_RESUME: ExitCode.REQUIRES_REPAIR,
    RecoveryMode.REPLAN: ExitCode.REQUIRES_REPAIR,
    RecoveryMode.WAIT: ExitCode.REQUIRES_HUMAN,
    RecoveryMode.REQUEST_HUMAN: ExitCode.REQUIRES_HUMAN,
    RecoveryMode.ROLLBACK: ExitCode.UNSAFE,
    RecoveryMode.ABORT: ExitCode.UNSAFE,
}


def exit_code_for(mode: RecoveryMode) -> int:
    """Map a recovery decision to a process exit status.

    Unmapped modes fall through to ``UNSAFE`` rather than ``OK``: a mode nobody
    has classified must never be mistaken for permission to proceed.
    """
    return _MODE_CODES.get(mode, ExitCode.UNSAFE)
