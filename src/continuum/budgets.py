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

Attempts are counted per idempotency key, not per action type (issue #368). The
limit is still configured per type, because that is the unit an operator thinks
in, but what it caps is repetition of one operation. Counting per type made
distinct work compete for the same allowance: three different recipients each
failing once, with no retry anywhere, exhausted a budget of three and blocked a
fourth that had never been attempted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_BUDGETS_PATH",
    "attempts_by_key",
    "attempts_for_type",
    "BudgetConfigError",
    "load_budgets",
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
    # Absolute, so the message names a file the operator can open: the
    # relative form depends on the cwd of whatever loaded the registry
    # (a hook, the sidecar, a CI step). Matches gate.py per #333.
    location = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BudgetConfigError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise BudgetConfigError(f"{location}: expected a JSON object")
    action_types = raw.get("action_types", {})
    if not isinstance(action_types, dict):
        raise BudgetConfigError(f"{location}: 'action_types' must be an object")
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
        raise BudgetConfigError(f"{location}: 'default_max_attempts' must be >= 1")
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


def attempts_by_key(events: Any, action_type: str) -> dict[str, int]:
    """Unsettled claim attempts for ``action_type``, counted per idempotency key.

    A retry budget has to count retries of *one operation*. Counting per action
    type instead conflated distinct work with repetition: three different
    recipients each failing once, with no retry anywhere, exhausted a budget of
    three and blocked a fourth recipient that had never been attempted (issue
    #368). Any fan-out with more than ``max_attempts`` failures of one type
    deadlocked mid-run.

    The key is the right unit because it *is* the operation's identity, and it is
    stable across retries: re-claiming after FAILED or COMPENSATED copies the
    existing action, so successive attempts under one key accumulate here rather
    than each opening a fresh row.

    A claim slot (an ``ACTION_RECORDED`` whose action status is STARTED) is one
    attempt. Settlement events are updates, not new attempts, so retries count but
    their bookkeeping does not. Keys whose action went on to COMPLETE are omitted:
    an operation that succeeded was never retried (issue #309).
    """
    from continuum.events import EventType
    from continuum.models import ActionStatus

    slots: dict[str, int] = {}
    final: dict[str, str] = {}
    for event in events:
        if event.type is not EventType.ACTION_RECORDED:
            continue
        action = event.payload.get("action")
        if not isinstance(action, Mapping) or action.get("action_type") != action_type:
            continue
        key = str(event.payload.get("key", ""))
        if not key:
            continue
        status = str(action.get("status"))
        if status == ActionStatus.STARTED.value:
            slots[key] = slots.get(key, 0) + 1
        final[key] = status

    return {
        key: count for key, count in slots.items() if final.get(key) != ActionStatus.COMPLETED.value
    }


def attempts_for_type(events: Any, action_type: str) -> int:
    """The most attempts any single operation of ``action_type`` has used.

    Reports the figure the budget is actually compared against, so the ``continuum
    budget`` view agrees with what the claim site enforces. It is deliberately not
    the sum across keys: that total is a measure of how much distinct work a run
    did, which no limit here caps (issue #368).
    """
    per_key = attempts_by_key(events, action_type)
    return max(per_key.values(), default=0)


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
