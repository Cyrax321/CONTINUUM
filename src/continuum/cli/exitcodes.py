"""Exit codes, chosen so shell pipelines are safe by default.

A recovery tool is most often invoked from automation::

    continuum resume "$RUN" && ./start-agent.sh

If the exit code did not reflect whether resuming is *safe*, that line would
launch an agent onto stale state or an unreconciled side effect. So the rule is
absolute: **only a fully verified, safe-to-resume run exits 0.** Every other
outcome (repairable, uncertain, blocked, missing, corrupted) is non-zero, and
the `&&` short-circuits.

Distinct codes let a script react proportionately (retry a repair, wait for a
condition, page a human on an unknown side effect, roll back, or abort) without
parsing text. Every recovery mode maps to its own code, so the two escalation
pairs a coarser scheme collapsed -- WAIT versus REQUEST_HUMAN, ROLLBACK versus
ABORT -- are now distinguishable (issue #1170). Text output is for people; exit
codes and ``--json`` are for machines.
"""

from __future__ import annotations

from continuum.models import RecoveryMode

__all__ = ["ExitCode", "exit_code_for"]


class ExitCode:
    """Meaningful process exit statuses.

    The recovery band runs 10-31: the tens digit is the escalation level
    (repair / hold / unsafe) and the ones digit separates the two modes that
    share a level, so a coarse consumer can still branch on the band while a
    precise one can tell the pair apart (issue #1170).
    """

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
    """State is recoverable once an automatic repair runs (REPAIR_AND_RESUME)."""

    REQUIRES_REPLAN = 11
    """State is recoverable but the plan itself must change (REPLAN)."""

    WAIT = 20
    """Hold and retry once a condition clears; no person needed yet (WAIT)."""

    REQUIRES_HUMAN = 21
    """A person must decide, typically an unreconciled side effect (REQUEST_HUMAN)."""

    ROLLBACK = 30
    """Unsafe as-is, but a rollback to a safe point is available (ROLLBACK)."""

    UNSAFE = 31
    """Resuming is not safe at all; abort (ABORT), and the fail-closed default."""


_MODE_CODES: dict[RecoveryMode, int] = {
    RecoveryMode.RESUME: ExitCode.OK,
    RecoveryMode.REPAIR_AND_RESUME: ExitCode.REQUIRES_REPAIR,
    RecoveryMode.REPLAN: ExitCode.REQUIRES_REPLAN,
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
