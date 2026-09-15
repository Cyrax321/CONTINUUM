"""Observability: the Phase 14 recovery dashboard.

This module renders the read-only recovery dashboard built from an existing
:class:`~continuum.recovery.engine.RecoveryDecision`.

The process-wide metrics collector that used to live here (``Metrics``,
``get_metrics``/``set_metrics``/``reset_metrics``, ``collect_from_decision`` and
the counter constants) had no caller outside the tests, so it was removed
(issue #1032). What an operator actually needs to know — how many runs resumed
and how many blocked — is answerable from the recovery ledger
(:mod:`continuum.recovery.ledger`), which is an append-only, tamper-evident
audit that survives a crash; a process-global counter is not. The collector is
recoverable from git history if a future surface needs it.
"""

from __future__ import annotations

from continuum.models import StateStatus
from continuum.recovery.engine import RecoveryDecision

__all__ = ["render_dashboard"]


_STATUS_SYMBOL = {
    StateStatus.VALID: "[ok]",
    StateStatus.STALE: "[--]",
    StateStatus.CONFLICTED: "[xx]",
    StateStatus.UNKNOWN: "[??]",
    StateStatus.INVALID: "[!!]",
    StateStatus.REQUIRES_REVIEW: "[??]",
    StateStatus.EXPIRED: "[--]",
}


def render_dashboard(decision: RecoveryDecision) -> str:
    """Render the Phase 14 recovery dashboard for ``decision``.

    Read-only over the existing :class:`RecoveryDecision`; emits no state.
    """
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("CONTINUUM RECOVERY DASHBOARD")
    lines.append("=" * 64)
    lines.append(f"run_id:            {decision.run_id}")
    lines.append(f"checkpoint:        v{decision.contract.checkpoint_version}")
    lines.append(f"recovery mode:     {decision.mode.value}")
    lines.append(f"safe to resume:     {'yes' if decision.safe else 'no'}")
    # Same rule as render_contract: "continue" only when resuming is actually
    # permitted; a blocked verdict must not render permission as prose.
    permitted = decision.next_allowed_action or (
        "continue" if decision.safe else "none (settle required_actions first)"
    )
    lines.append(f"next allowed:      {permitted}")

    lines.append("")
    lines.append("STATE COMPONENTS")
    lines.append("-" * 64)
    if decision.validation.report.statuses:
        for entry in decision.validation.report.statuses:
            symbol = _STATUS_SYMBOL.get(entry.status, "[??]")
            label = entry.component.value.replace("_", " ")
            identifier = f" {entry.component_id}" if entry.component_id else ""
            detail = f" -- {entry.detail}" if entry.detail else ""
            lines.append(f"  {symbol} {label}{identifier}: {entry.status.value}{detail}")
    else:
        lines.append("  (no components validated)")

    if decision.uncertain_actions:
        lines.append("")
        lines.append("UNCERTAIN SIDE EFFECTS")
        lines.append("-" * 64)
        for action in decision.uncertain_actions:
            lines.append(f"  [??] {action.action_type} ({action.status.value})")

    if decision.plan and decision.plan.steps:
        lines.append("")
        lines.append("REPAIRS REQUIRED")
        lines.append("-" * 64)
        lines.append(decision.plan.render())

    if decision.rationale:
        lines.append("")
        lines.append("RATIONALE")
        lines.append("-" * 64)
        for reason in decision.rationale:
            lines.append(f"  - {reason}")

    lines.append("")
    lines.append("=" * 64)
    return "\n".join(lines)
