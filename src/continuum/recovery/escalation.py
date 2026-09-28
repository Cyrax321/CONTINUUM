"""Escalation policy for the attention-budgeted human gate (issue #1409).

Every action the ledger cannot settle on its own lands at
``REQUIRES_REVIEW`` and interrupts a human at once. On a run that lasts weeks
that floods the reviewer, and a flooded reviewer approves without reading
(arXiv:2606.08919, arXiv:2606.22721), which makes the gate worth less than no
gate at all. The counter-measure is to spend the interruption budget where it
matters: score each action, batch the cheap ones behind a window and a prompt
cap, and escalate immediately only the ones whose blast radius clears an
operator-set threshold.

The policy is declarative, in ``.continuum/escalation.json``::

    {"hourly_prompt_cap": 10,
     "batch_window_seconds": 3600,
     "blast_radius_threshold": 0.8,
     "risk_weights": {"mem_delete": 0.9, "mem_write": 0.4, "default": 0.2}}

Absent the file, a fail-safe default applies. Present but unreadable, the
policy refuses to load rather than guessing at an operator's risk tolerance:
an escalation engine that quietly falls back to permissive numbers when its
config is broken spends an interruption budget nobody authorised, which is
the same reason ``load_budgets`` raises instead of defaulting (issue #427).

This sub-issue ships the schema, the loader and the scorer. Wiring the scorer
into the deferred review queue is #1410 and the fatigue telemetry is #1411.
Nothing here reads or writes the event log, so the module stays pure and
deterministic, and its consumers can be tested against fixed scores instead
of mocks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

__all__ = [
    "ACTION_TYPE_WEIGHT_FALLBACK",
    "DEFAULT_ESCALATION_POLICY",
    "DEFAULT_ESCALATION_POLICY_PATH",
    "ActionRisk",
    "EscalationPolicyError",
    "evaluate_action_risk",
    "load_escalation_policy",
]

DEFAULT_ESCALATION_POLICY_PATH = Path(".continuum/escalation.json")

#: Fail-safe posture used when the policy file is absent. The cap and window
#: are ordinary numbers rather than anything permissive: the default is "ask
#: often enough to be safe", and an operator who finds it noisy is meant to
#: widen it deliberately, not discover that silence was the default.
DEFAULT_ESCALATION_POLICY: dict[str, Any] = {
    "hourly_prompt_cap": 10,
    "batch_window_seconds": 3600,
    "blast_radius_threshold": 0.8,
    "risk_weights": {"default": 0.0},
}

#: Weight key every unrecognised action type falls back to. A policy that
#: names no ``default`` scores unknown actions 0.0, which is fail-safe: an
#: unrecognised action is never escalated immediately on its own.
ACTION_TYPE_WEIGHT_FALLBACK = "default"

#: Arguments key holding a resource class ("pgvector", "mem0"), for weighting
#: by the thing touched rather than the verb that touched it. Action types are
#: coarse ("mem_write"); an operator may trust one store more than another.
RESOURCE_CLASS_KEY = "resource_class"

#: Risk scores are closed-interval, so a weight can never be "more than certain".
_SCORE_MIN = 0.0
_SCORE_MAX = 1.0


class EscalationPolicyError(ValueError):
    """The escalation policy exists but cannot be honoured."""


class ActionRisk(NamedTuple):
    """A deterministic risk score for one action.

    ``score`` is within ``[0.0, 1.0]``. ``immediate`` is set when the score
    reaches the policy's blast radius threshold, meaning the item skips
    batching and interrupts at once; the queue batches everything else.
    """

    score: float
    immediate: bool


def _is_int(value: Any) -> TypeGuard[int]:
    """Whether ``value`` is an integer *and not* a JSON boolean.

    ``isinstance(True, int)`` holds in Python, so a plain int check passes for
    JSON ``true``: an ``hourly_prompt_cap`` of ``true`` would read as a cap of
    1, and ``batch_window_seconds`` of ``true`` as a 1-second window. Booleans
    are refused rather than coerced, matching ``budgets.py`` (issue #429).
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> TypeGuard[float]:
    """Whether ``value`` is a real number and not a JSON boolean."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _offending(value: Any) -> str:
    """Name the value and its type, so a rejection points at the token to fix.

    A hand-converted policy arrives with ``0.9`` where ``90`` was meant and the
    bare "must be a number" the operator used to get is the same sentence for
    a missing field, a string and a boolean, and never names the entry.
    """
    return f", got {value!r} ({type(value).__name__})"


def _as_score(value: Any, location: str) -> float:
    """Validate a closed-interval score, naming the offender if it is not one."""
    if not _is_number(value):
        raise EscalationPolicyError(f"{location} must be a number{_offending(value)}")
    score = float(value)
    if not _SCORE_MIN <= score <= _SCORE_MAX:
        raise EscalationPolicyError(
            f"{location} must be within [{_SCORE_MIN}, {_SCORE_MAX}]{_offending(value)}"
        )
    return score


def load_escalation_policy(path: Path | None = None) -> dict[str, Any]:
    """Load the escalation policy, or the fail-safe default when it is absent.

    Raises :class:`EscalationPolicyError` when the file exists but cannot be
    honoured: malformed JSON, a non-object root, or a value outside its
    contract. A broken policy never degrades to the default silently, because
    the default is a risk posture the operator never chose.

    The returned mapping is fresh, so mutating its ``risk_weights`` cannot
    reach the module-level default and leak into the next caller.
    """
    target = Path(path) if path is not None else DEFAULT_ESCALATION_POLICY_PATH
    if not target.exists():
        return {
            "hourly_prompt_cap": DEFAULT_ESCALATION_POLICY["hourly_prompt_cap"],
            "batch_window_seconds": DEFAULT_ESCALATION_POLICY["batch_window_seconds"],
            "blast_radius_threshold": DEFAULT_ESCALATION_POLICY["blast_radius_threshold"],
            "risk_weights": dict(DEFAULT_ESCALATION_POLICY["risk_weights"]),
        }
    # Absolute, so the message names a file the operator can open: the relative
    # form depends on the cwd of whatever loaded the policy (a hook, the
    # sidecar, a CI step). Matches gate.py and budgets.py.
    location = str(target.resolve())
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EscalationPolicyError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, Mapping):
        raise EscalationPolicyError(f"{location}: expected a JSON object")

    cap = raw.get("hourly_prompt_cap", DEFAULT_ESCALATION_POLICY["hourly_prompt_cap"])
    if not _is_int(cap) or cap < 1:
        raise EscalationPolicyError(
            f"{location}: 'hourly_prompt_cap' must be an integer >= 1{_offending(cap)}"
        )

    window = raw.get("batch_window_seconds", DEFAULT_ESCALATION_POLICY["batch_window_seconds"])
    if not _is_int(window) or window < 0:
        raise EscalationPolicyError(
            f"{location}: 'batch_window_seconds' must be an integer >= 0{_offending(window)}"
        )

    threshold = _as_score(
        raw.get("blast_radius_threshold", DEFAULT_ESCALATION_POLICY["blast_radius_threshold"]),
        f"{location}: 'blast_radius_threshold'",
    )

    weights = raw.get("risk_weights", DEFAULT_ESCALATION_POLICY["risk_weights"])
    if not isinstance(weights, Mapping):
        raise EscalationPolicyError(f"{location}: 'risk_weights' must be an object")
    validated: dict[str, float] = {}
    for key, score in weights.items():
        if not isinstance(key, str) or not key.strip():
            raise EscalationPolicyError(f"{location}: risk weight keys must be non-empty strings")
        validated[key.strip()] = _as_score(score, f"{location}: risk_weights[{key!r}]")

    return {
        "hourly_prompt_cap": int(cap),
        "batch_window_seconds": int(window),
        "blast_radius_threshold": threshold,
        "risk_weights": validated,
    }


def evaluate_action_risk(
    action_type: str,
    arguments: Mapping[str, Any] | None,
    policy: Mapping[str, Any] | None = None,
) -> ActionRisk:
    """Score one action against the policy, deterministically.

    The score is the highest applicable weight: the action's own type, a
    resource class named in its arguments, or the policy's ``default``. Taking
    the maximum rather than the average is deliberate: when two
    classifications both apply and disagree, the more dangerous one governs,
    so a low generic weight can never dull a specific high one.

    ``immediate`` marks a score at or above the blast radius threshold: the
    item bypasses batching and interrupts at once. A threshold of ``0.0``
    escalates everything, which is the operator's explicit choice, not a
    fallback the scorer invents.

    An unrecognised action in a policy with no ``default`` scores ``0.0`` and
    is never immediate. The scorer never raises on a hand-built policy: a
    missing or malformed section degrades to the fail-safe floor rather than
    breaking the gate that asked for a score.
    """
    pol = policy if isinstance(policy, Mapping) else DEFAULT_ESCALATION_POLICY
    raw_weights = pol.get("risk_weights", {})
    weights: Mapping[str, Any] = raw_weights if isinstance(raw_weights, Mapping) else {}
    raw_threshold = pol.get(
        "blast_radius_threshold", DEFAULT_ESCALATION_POLICY["blast_radius_threshold"]
    )
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        threshold = float(DEFAULT_ESCALATION_POLICY["blast_radius_threshold"])

    candidates: list[str] = []
    if isinstance(action_type, str) and action_type.strip():
        candidates.append(action_type.strip())
    if isinstance(arguments, Mapping):
        resource_class = arguments.get(RESOURCE_CLASS_KEY)
        if isinstance(resource_class, str) and resource_class.strip():
            candidates.append(resource_class.strip())
    candidates.append(ACTION_TYPE_WEIGHT_FALLBACK)

    best = _SCORE_MIN
    for key in candidates:
        hit = weights.get(key)
        if _is_number(hit):
            best = max(best, float(hit))
    return ActionRisk(score=best, immediate=best >= threshold)
