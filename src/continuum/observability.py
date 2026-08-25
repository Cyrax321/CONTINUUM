"""Observability and the Phase 14 recovery dashboard.

This module is additive. It provides a process-wide metrics collector that core
modules can emit to without taking a hard dependency on storage or the recovery
engine, plus a read-only renderer for the Phase 14 recovery dashboard built
from an existing :class:`~continuum.recovery.engine.RecoveryDecision`.
"""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from continuum.models import StateStatus
from continuum.recovery.engine import RecoveryDecision

__all__ = [
    "Metrics",
    "get_metrics",
    "set_metrics",
    "reset_metrics",
    "render_dashboard",
    "collect_from_decision",
    "CHECKPOINTS_CREATED",
    "VALIDATIONS_RUN",
    "ACTIONS_CLAIMED",
    "ACTIONS_COMPLETED",
    "UNKNOWN_SIDE_EFFECTS",
    "RECOVERIES_RESUMED",
    "RECOVERIES_BLOCKED",
]


# Standard counter names --------------------------------------------------- #
CHECKPOINTS_CREATED = "checkpoints.created"
VALIDATIONS_RUN = "validations.run"
ACTIONS_CLAIMED = "actions.claimed"
ACTIONS_COMPLETED = "actions.completed"
UNKNOWN_SIDE_EFFECTS = "actions.unknown_side_effects"
RECOVERIES_RESUMED = "recoveries.resumed"
RECOVERIES_BLOCKED = "recoveries.blocked"


class _Timer:
    """Context manager that records elapsed seconds into a :class:`Metrics`."""

    def __init__(self, metrics: Metrics, name: str) -> None:
        self._metrics = metrics
        self._name = name
        self._started: float | None = None

    def __enter__(self) -> _Timer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        assert self._started is not None
        self._metrics.record_time(self._name, time.perf_counter() - self._started)


@dataclass
class Metrics:
    """A process-wide accumulator for recovery signals.

    Counters are monotonic; timers accumulate elapsed seconds per name. The
    collector is intentionally dependency-free so core modules can emit metrics
    without importing storage or the recovery engine.
    """

    counters: dict[str, int] = field(default_factory=dict)
    timers: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)

    def increment(self, name: str, by: int = 1) -> None:
        if by < 0:
            raise ValueError("counters may only increase")
        self.counters[name] = self.counters.get(name, 0) + by

    def set_gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def record_time(self, name: str, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("elapsed time may not be negative")
        self.timers[name] = self.timers.get(name, 0.0) + seconds

    def timer(self, name: str) -> _Timer:
        return _Timer(self, name)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "timers": dict(self.timers),
            "gauges": dict(self.gauges),
        }

    def reset(self) -> None:
        self.counters.clear()
        self.timers.clear()
        self.gauges.clear()


_DEFAULT_METRICS: ContextVar[Metrics | None] = ContextVar("continuum_metrics", default=None)


def get_metrics() -> Metrics:
    """Return the active collector, creating a default one if none is set."""
    metrics = _DEFAULT_METRICS.get()
    if metrics is None:
        metrics = Metrics()
        _DEFAULT_METRICS.set(metrics)
    return metrics


def set_metrics(metrics: Metrics) -> Token[Metrics | None]:
    """Install ``metrics`` as the active collector for this context."""
    return _DEFAULT_METRICS.set(metrics)


def reset_metrics() -> None:
    """Replace the active collector with a fresh one."""
    _DEFAULT_METRICS.set(Metrics())


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


def collect_from_decision(decision: RecoveryDecision) -> None:
    """Update the active metrics collector from a recovery ``decision``."""
    metrics = get_metrics()
    metrics.increment(VALIDATIONS_RUN)
    statuses = decision.validation.report.statuses
    metrics.set_gauge("validation.components", len(statuses))
    metrics.set_gauge(
        "validation.invalid",
        sum(1 for e in statuses if e.status is not StateStatus.VALID),
    )
    metrics.increment(RECOVERIES_RESUMED if decision.safe else RECOVERIES_BLOCKED)
    metrics.increment(UNKNOWN_SIDE_EFFECTS, len(decision.uncertain_actions))
