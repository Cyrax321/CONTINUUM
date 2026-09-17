"""Exit codes, chosen so shell pipelines are safe by default.

A recovery tool is most often invoked from automation::

    continuum resume "$RUN" && ./start-agent.sh

If the exit code did not reflect whether resuming is *safe*, that line would
launch an agent onto stale state or an unreconciled side effect. So the rule is
absolute: **only a fully verified, safe-to-resume run exits 0.** Every other
outcome (repairable, uncertain, blocked, missing, corrupted) is non-zero, and
the `&&` short-circuits.

Each recovery mode has its own code, so a script can react proportionately
without parsing text: retry a repair, poll a wait, page a human on an unknown
side effect, give up on an abort. Codes are grouped into bands of ten, where
the band names the *reaction* and the offset distinguishes two modes that demand
the same reaction but read differently: the 10s need repair before resuming, the
20s need a person or a clock, the 30s are unsafe to resume from as-is. Modes in
the same band that call for identical handling (repairing state versus
re-planning the goal) still share a code; the two bands a script must not
conflate, human versus wait and rollback versus abort, do not. A consumer that
wants only the reaction can range-check (``20 <= code < 30``), and one that wants
the mode reads the exact value. Text output is for people; exit codes and
``--json`` are for machines.
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

    WAIT = 25
    """A condition must clear on its own (a liveness breach) before resuming."""

    UNSAFE = 30
    """Resuming is not safe at all."""

    ROLLBACK = 35
    """Not safe as-is; resuming requires rolling back to a prior checkpoint."""


_MODE_CODES: dict[RecoveryMode, int] = {
    RecoveryMode.RESUME: ExitCode.OK,
    RecoveryMode.REPAIR_AND_RESUME: ExitCode.REQUIRES_REPAIR,
    RecoveryMode.REPLAN: ExitCode.REQUIRES_REPAIR,
    RecoveryMode.WAIT: ExitCode.WAIT,
    RecoveryMode.REQUEST_HUMAN: ExitCode.REQUIRES_HUMAN,
    RecoveryMode.ROLLBACK: ExitCode.ROLLBACK,
    RecoveryMode.ABORT: ExitCode.UNSAFE,
}


def exit_code_for(mode: RecoveryMode) -> int:
    """Map a recovery decision to a process exit status.

    Unmapped modes fall through to ``UNSAFE`` rather than ``OK``: a mode nobody
    has classified must never be mistaken for permission to proceed.
    """
    return _MODE_CODES.get(mode, ExitCode.UNSAFE)
