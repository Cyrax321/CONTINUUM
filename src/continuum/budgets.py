"""Run-level retry budgets (issue #240).

Agent loops invent retries: a failing upstream gets hammered because the
model re-plans after every failure, and each attempt opens a fresh ledger
slot. RetryGuard (arXiv:2511.23278) shows local retry policies amplify cost;
the fix here is a *run-level budget* evaluated at claim time.

Registries live in `.continuum/budgets.json` (JSON, matching the other
registries):

    {"default_max_attempts": 3,
     "action_types": {"send_invoice": {"max_attempts": 5}}}

`evaluate_budget` counts prior attempts for an action type from the folded
ledger and returns whether another claim may proceed. CONTINUUM never retries
anything itself - it counts and gates - so the enforcement surface stays a
single pure function plus thin wiring at claim sites.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_BUDGETS_PATH",
    "attempts_for_type",
    "BudgetConfigError",
    "load_budgets",
    "attempts_for_type",
    "evaluate_budget",
    "backoff_delay",
]

DEFAULT_BUDGETS_PATH = ".continuum/budgets.json"

#: Fallback when neither the action type nor the registry sets a limit.
FALLBACK_MAX_ATTEMPTS = 3


class BudgetConfigError(ValueError):
    """The budget registry exists but cannot be honoured."""


def load_budgets(path: Path) -> dict[str, Any]:
    """Read the budget registry. ``{}`` when absent; raise when malformed."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BudgetConfigError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise BudgetConfigError(f"{path}: expected a JSON object")
    action_types = raw.get("action_types", {})
    if not isinstance(action_types, dict):
        raise BudgetConfigError(f"{path}: 'action_types' must be an object")
    for name, spec in action_types.items():
        entry = (
            spec
            if isinstance(spec, int)
            else (spec.get("max_attempts") if isinstance(spec, dict) else None)
        )
        if not isinstance(entry, int) or entry < 1:
            raise BudgetConfigError(
                f"{path}: action type {name!r} needs a positive integer 'max_attempts'"
            )
    default_max = raw.get("default_max_attempts")
    if default_max is not None and (not isinstance(default_max, int) or default_max < 1):
        raise BudgetConfigError(f"{path}: 'default_max_attempts' must be >= 1")
    return raw


def _max_for(action_type: str, raw: Mapping[str, Any]) -> int:
    per_type = raw.get("action_types", {})
    spec = per_type.get(action_type)
    if isinstance(spec, int):
        return spec
    if isinstance(spec, dict) and isinstance(spec.get("max_attempts"), int):
        return int(spec["max_attempts"])
    fallback = raw.get("default_max_attempts", FALLBACK_MAX_ATTEMPTS)
    return int(fallback)


def attempts_for_type(events: Any, action_type: str) -> int:
    """Count claim slots opened for ``action_type`` from raw events.

    A claim slot (an ACTION_RECORDED whose action status is STARTED) is one
    attempt. Settlement events (completed/failed/unknown) are updates, not
    new attempts - so retries count but their bookkeeping does not.
    """
    from continuum.events import EventType
    from continuum.models import ActionStatus

    return sum(
        1
        for e in events
        if e.type is EventType.ACTION_RECORDED
        and isinstance(e.payload.get("action"), Mapping)
        and e.payload["action"].get("action_type") == action_type
        and e.payload["action"].get("status") == ActionStatus.STARTED.value
    )


def evaluate_budget(
    raw_config: Mapping[str, Any] | None,
    action_type: str,
    attempts_so_far: int,
) -> tuple[bool, int, int]:
    """Return ``(allowed, attempts_so_far, max_attempts)``.

    Pure so claim sites can call it with nothing but the folded attempt count.
    """
    _ = raw_config  # kept in signature for symmetry with other registries
    cfg = raw_config or {}
    maximum = _max_for(action_type, cfg)
    return attempts_so_far < maximum, attempts_so_far, maximum


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
) -> float:
    """Exponential backoff with a ceiling. Pure; jitter is the caller's job."""
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return float(min(base * (2 ** (attempt - 1)), cap))
