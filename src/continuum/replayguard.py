"""Replay-safety as a portable primitive (issue #237).

ACRFence (arXiv:2603.20625) formalises the semantic-rollback hazard:
frameworks re-execute node code on resume (LangGraph issue #6208), and any
side effect inside that code fires twice. CONTINUUM's gate already solves
this for registered tools; this module extracts the decision table into a
portable core that any framework binding can share - and adds memoisation,
so a completed operation's recorded result is returned instead of re-fired.

The core is pure over the folded ledger:

- ALLOW            - live claim; execute now, settle from reality afterwards
- SKIP_DUPLICATE   - COMPLETED already; return the recorded result, do nothing
- BLOCK_UNCERTAIN  - outcome unknown; reconcile before anything else
- DENY_UNCLAIMED   - no claim; the caller must route through intercept first
- DENY_RECLAIM     - closed attempt; claim again before retrying

`protected_call` is the reference execution wrapper (claim -> fn ->
complete/fail with result memoised). `langgraph_protected_node` turns it
into a graph-node decorator so interrupt-resume replays become cache hits.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "GuardKind",
    "GuardDecision",
    "RESULT_ENVELOPE_KEY",
    "evaluate",
    "protected_call",
    "langgraph_protected_node",
]

# Journal envelope, same literal adapters.generic uses for the same reason
# (issue #44): a stored result must be distinguishable from the caller's own
# dict, or the replay path unwraps one member of it instead of the dict
# (issue #736). Defined locally so the core stays importable without the
# adapter stack.
RESULT_ENVELOPE_KEY = "__return_value__"


class GuardKind(StrEnum):
    """Enumeration of replay-guard verdicts for intended side effects.

    Classifies an action against the folded ledger into one of:
    ``ALLOW`` (live claim, proceed with execution),
    ``SKIP_DUPLICATE`` (already completed, return memoized result),
    ``DENY_UNCLAIMED`` (no claim registered yet),
    ``DENY_DUPLICATE`` (explicit duplicate refusal),
    ``BLOCK_UNCERTAIN`` (outcome in doubt, requires reconciliation), or
    ``DENY_RECLAIM`` (previous attempt closed, requires new claim).
    """

    ALLOW = "allow"
    SKIP_DUPLICATE = "skip_duplicate"
    DENY_UNCLAIMED = "deny_unclaimed"
    DENY_DUPLICATE = "deny_duplicate"
    BLOCK_UNCERTAIN = "block_uncertain"
    DENY_RECLAIM = "deny_reclaim"


@dataclass(frozen=True)
class GuardDecision:
    """Verdict and rationale produced by evaluating an action against the ledger.

    Carries the evaluated :class:`GuardKind`, an explanatory human-readable
    reason, and the optional resolved idempotency key string.
    """

    kind: GuardKind
    reason: str
    key: str | None = None


def evaluate(
    *,
    action_type: str,
    rendered_key: str,
    run_id: str,
    actions_by_key: Mapping[str, Any],
) -> GuardDecision:
    """Classify one intended side effect against the folded ledger."""
    from continuum.actions.idempotency import idempotency_key
    from continuum.models import ActionStatus

    key = str(idempotency_key(action_type, None, scope=run_id, key=rendered_key))
    action = actions_by_key.get(key)
    if action is None or action.action_type != action_type:
        return GuardDecision(
            GuardKind.DENY_UNCLAIMED,
            f"{action_type!r} {rendered_key!r} has no ledger claim",
            key=key,
        )
    status = action.status
    if status is ActionStatus.STARTED:
        return GuardDecision(GuardKind.ALLOW, "live claim", key=key)
    if status is ActionStatus.COMPLETED:
        return GuardDecision(
            GuardKind.SKIP_DUPLICATE,
            f"{action_type!r} {rendered_key!r} already completed",
            key=key,
        )
    if status is ActionStatus.UNKNOWN:
        return GuardDecision(
            GuardKind.BLOCK_UNCERTAIN,
            f"{action_type!r} {rendered_key!r} has an unknown outcome; reconcile first",
            key=key,
        )
    return GuardDecision(
        GuardKind.DENY_RECLAIM,
        f"previous attempt of {action_type!r} {rendered_key!r} is closed "
        f"({status.value}); claim again before retrying",
    )


def _fold(storage: Any, run_id: str) -> dict[str, Any]:
    # Via the ledger, not a raw event fold: the ledger includes events that
    # compaction (issue #239) moved into the archive, so a completed action
    # stays a cache hit instead of re-firing after compaction.
    from continuum.actions.ledger import ActionLedger

    return ActionLedger(storage, run_id).folded()


def protected_call(
    storage: Any,
    run_id: str,
    *,
    action_type: str,
    key: str,
    fn: Callable[..., Any],
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> tuple[GuardKind, Any]:
    """Execute ``fn`` under a claim keyed by ``key``, with memoisation.

    Returns ``(kind, result)``. On ALLOW the effect runs exactly once and its
    dict-shaped result is journaled; a later identical call returns
    (SKIP_DUPLICATE, prior_result) without executing. Uncertain or unclaimed
    states raise ReplayBlocked rather than guessing.
    """
    from continuum.actions import ActionLedger

    actions = _fold(storage, run_id)
    decision = evaluate(
        action_type=action_type,
        rendered_key=key,
        run_id=run_id,
        actions_by_key=actions,
    )
    ledger = ActionLedger(storage, run_id)

    # Open a slot on first sight (graph nodes are their own claim point);
    # reuse the live slot when a previous attempt was interrupted mid-call.
    if decision.kind is GuardKind.DENY_UNCLAIMED:
        outcome = ledger.claim(action_type, {}, key=key)
        key_to_use: str = outcome.key
    else:
        assert decision.key is not None, decision
        key_to_use = decision.key

    if decision.kind is GuardKind.ALLOW or decision.kind is GuardKind.DENY_UNCLAIMED:
        try:
            result = fn(*(args or ()), **(kwargs or {}))
        except Exception as exc:
            ledger.fail(key_to_use, str(exc), certain=False)
            raise
        # A non-dict has to be wrapped to be stored. So does any dict carrying
        # the envelope key or the legacy "return" marker: the replay path below
        # unwraps those, and storing a caller's own dict with such a key as-is
        # would make replay return one member of it rather than the whole dict,
        # so a completed operation would answer differently on its second call
        # than on its first (issue #736).
        needs_envelope = (
            not isinstance(result, dict) or RESULT_ENVELOPE_KEY in result or "return" in result
        )
        journal = {RESULT_ENVELOPE_KEY: result} if needs_envelope else result
        ledger.complete(key_to_use, external_id=key, result=journal)
        return GuardKind.ALLOW, result

    if decision.kind is GuardKind.SKIP_DUPLICATE:
        assert decision.key is not None
        cached_action = actions[decision.key]
        cached = cached_action.result
        if isinstance(cached, dict) and RESULT_ENVELOPE_KEY in cached:
            value = cached[RESULT_ENVELOPE_KEY]
        elif isinstance(cached, dict) and set(cached) == {"return"}:
            # Legacy journal from before the envelope: a non-dict result
            # wrapped as {"return": ...}. The write path now envelopes any
            # dict containing "return", so this shape is historical only.
            value = cached["return"]
        else:
            value = cached
        return GuardKind.SKIP_DUPLICATE, value

    raise ReplayBlocked(decision)


class ReplayBlocked(RuntimeError):
    """Raised when a protected call is refused by the guard."""

    def __init__(self, decision: GuardDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision
        self.kind = decision.kind


# --- LangGraph binding --------------------------------------------------------- #


def langgraph_protected_node(
    storage: Any,
    run_id: str,
    *,
    action_type: str | None = None,
    key_fields: list[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a LangGraph node so it executes at most once per identity.

    The identity defaults to the node function's name plus a stable hash of
    the JSON-serialisable subset of its input state. On replay after an
    interrupt/crash, a completed node is skipped and its journalled result is
    returned to the graph instead of re-firing the node's side effects.
    """
    import hashlib

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a node function with identity calculation and replay protection."""
        node_name = getattr(fn, "__name__", "node")

        def identity(state: dict[str, Any]) -> str:
            """Compute a stable idempotency key for node execution from state."""
            if key_fields:
                basis = {k: state.get(k) for k in key_fields}
            else:
                basis = {
                    k: v for k, v in sorted(state.items()) if isinstance(v, (str, int, float, bool))
                }
            blob = json.dumps(basis, sort_keys=True, default=str)
            digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
            return f"node:{node_name}:{digest}"

        def wrapped(state: dict[str, Any]) -> dict[str, Any]:
            """Execute or short-circuit node invocation using memoized results."""
            kind, value = protected_call(
                storage,
                run_id,
                action_type=action_type or f"node.{node_name}",
                key=identity(state),
                fn=lambda: fn(state),
            )
            if kind is GuardKind.SKIP_DUPLICATE:
                marker = {"replayed": True}
                if isinstance(value, dict):
                    marker.update(value)
                    return marker
                return {"replayed_output": value}
            if isinstance(value, dict):
                return value
            return {"output": value}

        wrapped.__name__ = node_name
        wrapped.__continuum_protected__ = True  # type: ignore[attr-defined]
        return wrapped

    return decorator
