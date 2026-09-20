"""Uniform action representation across adapters.

Different environment adapters (shell, in-process python, container, browser,
k8s, filesystem) ultimately do the same thing: perform a named operation with
some parameters, possibly scoped to a dependency. To recover any
agent-environment interaction uniformly, the recovery pipeline works with
:class:`AdapterAction` and :class:`AdapterResult` rather than each adapter's
native call shape. Adapters translate their own requests into an
``AdapterAction`` and return an ``AdapterResult``; nothing about the existing
adapter interfaces changes.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from continuum.adapters.base import AgentAdapter

__all__ = [
    "AdapterAction",
    "AdapterResult",
    "TelemetryHook",
    "run_action",
]


@dataclass(frozen=True)
class AdapterAction:
    """A single, dependency-aware operation an adapter can perform."""

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    dep_scope: str | None = None
    action_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": dict(self.params),
            "dep_scope": self.dep_scope,
            "action_id": self.action_id,
        }


@dataclass
class AdapterResult:
    """The outcome of running an :class:`AdapterAction`."""

    status: str
    output: Any = None
    error: str | None = None


TelemetryHook = Callable[[AdapterAction, AdapterResult], None]


def run_action(
    adapter: AgentAdapter,
    run_id: str,
    action: AdapterAction,
    fn: Callable[[], Any],
    *,
    on_event: TelemetryHook | None = None,
) -> AdapterResult:
    """Execute ``action`` through ``adapter`` and return an ``AdapterResult``.

    Thin, uniform facade over :meth:`AgentAdapter.intercept_action`: the adapter
    still owns idempotency and side-effect safety; this only normalizes the
    request/response shape so the recovery pipeline treats every adapter the
    same. Failures are captured into ``AdapterResult`` instead of raised.

    ``on_event`` is an opt-in telemetry hook (issue #162), disabled by default.
    It is invoked once with the action and the final result, whether the action
    completed or failed. An exception escaping the hook is swallowed:
    observability must never be able to break the action it observes. The
    lower-level ``intercept_action`` deliberately bypasses telemetry; hooks
    attach at this facade, where the uniform shape exists.
    """
    try:
        output = adapter.intercept_action(
            run_id,
            action.name,
            fn,
            arguments=dict(action.params),
            dep_scope=action.dep_scope,
        )
    except Exception as exc:  # adapter already recorded the attempt uncertain
        result = AdapterResult(status="failed", error=f"{type(exc).__name__}: {exc}")
        _emit(on_event, action, result)
        return result
    result = AdapterResult(status="completed", output=output)
    _emit(on_event, action, result)
    return result


def _emit(hook: TelemetryHook | None, action: AdapterAction, result: AdapterResult) -> None:
    if hook is None:
        return
    # A failing observer must not fail the action it observes.
    with contextlib.suppress(Exception):
        hook(action, result)
