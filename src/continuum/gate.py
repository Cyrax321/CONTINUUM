"""Pre-action gating for external side effects (issue #217).

The two-phase action protocol is only as strong as the model's willingness to
follow it. Observation (#210) records what happened; it cannot stop an
unclaimed side effect from firing. Harnesses that support pre-tool-use hooks
which can *deny* a call make enforcement possible at the host layer, outside
the model's control.

This module holds the pure decision logic so it can be tested exhaustively
without a harness:

- :func:`load_gate_config` reads ``.continuum/gate.json``, which registers
  side-effect tools and the stable-key templates that identify their
  operations.
- :func:`render_key` derives the idempotency key from the call's structured
  arguments using the configured template. Keys come from configuration, never
  from LLM-authored strings.
- :func:`decide` answers one question: may this tool call proceed?

The decision rule mirrors the ledger's semantics exactly. A gated call is
allowed only when a live claim (status ``STARTED``) already exists for its
derived key. Anything else is denied with instructions: unclaimed calls are
told how to claim, completed calls are told the effect already happened (this
is the dedup verdict made physical), uncertain calls are told to reconcile,
and closed attempts are told to claim again. When the config file exists but
is malformed the gate fails closed: a file someone wrote is a statement of
intent, and silently letting everything through would defeat it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from continuum.actions.idempotency import idempotency_key
from continuum.events import EventType

__all__ = [
    "DEFAULT_GATE_CONFIG_PATH",
    "Decision",
    "load_gate_config",
    "normalize_key_value",
    "render_key",
    "decide",
    "collect_consumed_authorities",
    "is_authority_consumed",
]

#: Where the gate configuration lives, relative to the project root the hook
#: runs in. JSON, matching the existing ``.continuum/mcp-policy.json``
#: convention.
DEFAULT_GATE_CONFIG_PATH = ".continuum/gate.json"


class GateConfigError(ValueError):
    """The gate configuration exists but cannot be honoured."""


def collect_consumed_authorities(events: Any) -> dict[str, Any]:
    """Scan events for AUTHORITY_CONSUMED and return map authority_id to event.

    First consumption wins, the log is append-only, but a later
    AUTHORITY_RECONCILED with valid true clears the consumed mark, which
    is how an external probe can re-validate an authority. Valid false
    keeps it blocked, unknown leaves it blocked.
    """
    consumed: dict[str, Any] = {}
    for ev in events:
        ev_type = getattr(ev, "type", None)
        payload = getattr(ev, "payload", {}) or {}
        if ev_type is EventType.AUTHORITY_CONSUMED:
            aid = payload.get("authority_id")
            if isinstance(aid, str) and aid not in consumed:
                consumed[aid] = ev
        elif ev_type is EventType.AUTHORITY_RECONCILED:
            aid = payload.get("authority_id")
            valid = payload.get("valid")
            if isinstance(aid, str) and aid in consumed and valid is True:
                # External probe says authority is still valid, unblock
                del consumed[aid]
    return consumed


def is_authority_consumed(authority_id: str, consumed: Any) -> bool:
    """True when authority_id is in the consumed map."""
    if not consumed:
        return False
    return authority_id in consumed


@dataclass(frozen=True)
class Decision:
    """The outcome of one gate evaluation."""

    allow: bool
    reason: str
    #: Fork candidates (#259): journalled same-type actions this denied call
    #: resembles by resource tokens. Non-empty only on unclaimed denials that
    #: look like deliberate divergence after a restore, never on allow.
    fork_candidates: tuple[Any, ...] = ()


def load_gate_config(path: Path) -> dict[str, dict[str, Any]] | None:
    """Read the gate registry. Returns None when no configuration exists.

    A missing file means "no gate configured", which is distinct from a file
    that exists and is broken: the latter raises rather than degrading into
    silently passing every call.
    """
    if not path.exists():
        return None
    # Resolved once, and used by every message below. The point of #333 is that
    # an operator debugging a gate refusal needs to know which file to open, and
    # the relative form depends on the cwd of whatever invoked the hook. Two of
    # these four messages were still relative, so the same error could name the
    # file two different ways depending on which validation tripped.
    location = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GateConfigError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tools", {}), dict):
        raise GateConfigError(f"{location}: expected {{'tools': {{...}}}}")
    tools = raw.get("tools") or {}
    for tool, spec in tools.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("key_template"), str):
            raise GateConfigError(f"{location}: tool {tool!r} needs a string 'key_template'")
        if spec.get("action_type") is not None and not isinstance(spec.get("action_type"), str):
            raise GateConfigError(f"{location}: tool {tool!r} 'action_type' must be a string")
    return tools


def normalize_key_value(value: Any) -> Any:
    """Surrounding whitespace stripped from a string value, others untouched.

    Two calls that name the same resource must derive the same key, and an
    LLM-authored argument routinely arrives as ``" 123 "`` or ``"123\\n"``. Left
    alone, ``invoice: 123 `` and ``invoice:123`` are different ledger keys, so
    the second call finds no live claim of its own and the dedup verdict the
    gate exists to deliver never fires -- the same side effect can happen twice.

    Only ``str`` is touched. A template may carry a format spec (``{amount:.2f}``),
    and stringifying the value first would make that spec raise on the way to a
    key, turning a working configuration into a hard error.

    Public because the enforcing gateway derives keys from HTTP bodies the same
    way (issue #361): one rule in one place, since two copies of an identity
    rule drift into two different identities.
    """
    return value.strip() if isinstance(value, str) else value


def render_key(template: str, tool_input: Mapping[str, Any]) -> str:
    """Substitute ``{field}`` placeholders from the call's arguments.

    Only top-level argument fields are supported in v1. A placeholder with no
    matching argument raises: a template the current call cannot satisfy is a
    configuration problem worth surfacing, not something to paper over with a
    weaker identity. String values are stripped of surrounding whitespace
    (:func:`normalize_key_value`); the template itself is used verbatim.
    """
    import string

    fields = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    missing = [f for f in fields if f not in tool_input]
    if missing:
        raise GateConfigError(
            f"key template {template!r} needs argument(s) {missing} "
            f"but the call supplied {sorted(tool_input)!r}"
        )
    values = {f: normalize_key_value(tool_input[f]) for f in fields}
    return template.format(**values)


def _expected_key(action_type: str, run_id: str, rendered: str) -> str:
    """The exact ledger key a claim of this operation must have produced."""
    return str(idempotency_key(action_type, None, scope=run_id, key=rendered))


def decide(
    config: Mapping[str, Mapping[str, Any]] | None,
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    run_id: str,
    actions_by_key: Mapping[str, Any],
    consumed_authorities: Mapping[str, Any] | None = None,
) -> Decision:
    """Decide whether one tool call may proceed.

    ``actions_by_key`` maps ledger keys to Action records (the output of
    ``fold_action_events`` over the run's event log). Ungated tools pass
    immediately without touching anything else.
    """
    # Authority resurrection check (issue #289b): if any string value in the
    # tool input matches a consumed authority, refuse before any ledger check.
    # The check is value-based rather than field-name based so that drift in
    # argument names does not resurrect spent authority. The message names the
    # original consumption event so the operator can audit the lineage.
    if consumed_authorities:
        for _key, _value in tool_input.items():
            if isinstance(_value, str) and _value in consumed_authorities:
                ev = consumed_authorities[_value]
                seq = (
                    getattr(ev, "sequence", "?")
                    if hasattr(ev, "sequence")
                    else ev.get("sequence", "?")
                )
                payload = getattr(ev, "payload", {}) or {}
                if hasattr(ev, "payload"):
                    seq = ev.sequence
                    payload = ev.payload
                else:
                    payload = ev.get("payload", {})
                consumer = payload.get("consumer_run_id", "?")
                return Decision(
                    False,
                    f"Authority {_value!r} consumed at seq {seq} by run {consumer!r}. Obtain a fresh authority.",
                )
        # Also check string values that may be nested as authority_id field
        # is sometimes the whole value; the loop above already covers top-level
        # values, which is sufficient for the tested shapes.

    if config is None:
        return Decision(True, "no gate configured")
    spec = config.get(tool_name)
    if spec is None:
        return Decision(True, "tool is not gated")

    action_type = spec.get("action_type") or tool_name
    try:
        rendered = render_key(spec["key_template"], tool_input)
        _expected_key(action_type, run_id, rendered)
    except GateConfigError as exc:
        return Decision(False, f"gate configuration error: {exc}")

    from continuum.replayguard import GuardKind
    from continuum.replayguard import evaluate as core_evaluate

    # Single source of truth (#237): the gate classifies through the shared
    # replayguard core, then renders its own registry-aware messages.
    decision = core_evaluate(
        action_type=action_type,
        rendered_key=rendered,
        run_id=run_id,
        actions_by_key=actions_by_key,
    )
    action = actions_by_key.get(decision.key) if decision.key else None

    if decision.kind is GuardKind.ALLOW:
        return Decision(True, f"live claim {rendered!r}")
    if decision.kind is GuardKind.DENY_UNCLAIMED:
        # Fork detection (#259): an unclaimed call whose resource tokens
        # overlap journalled same-type work is what deliberate divergence
        # looks like after a restore. Surface the neighbours and the exact
        # approval command instead of a bare denial.
        from continuum.recovery.fork import detect_fork_candidates

        candidates = tuple(
            detect_fork_candidates(
                action_type=action_type,
                tool_input=tool_input,
                actions_by_key=actions_by_key,
            )
        )
        message = (
            f"side effect {action_type!r} with key {rendered!r} has no ledger claim. "
            f"Call the MCP tool continuum_intercept_action with run_id={run_id!r}, "
            f"action_type={action_type!r}, key={rendered!r} first, then repeat this call."
        )
        if candidates:
            neighbour = candidates[0]
            message += (
                f" This call resembles journalled action {neighbour.action_id[:14]} "
                f"({neighbour.status}, shared tokens: {', '.join(neighbour.shared_tokens[:3])}). "
                f"If it is a deliberate new direction, branch it with: "
                f"continuum fork {run_id} --reason '<why>' --child <new-run-id>"
            )
        return Decision(False, message, fork_candidates=candidates)
    if decision.kind is GuardKind.SKIP_DUPLICATE or (decision.kind is GuardKind.DENY_DUPLICATE):
        return Decision(
            False,
            f"{action_type!r} with key {rendered!r} was already completed"
            + (f" (external id {action.external_id!r})" if action and action.external_id else "")
            + ". Do not repeat it.",
        )
    if decision.kind is GuardKind.BLOCK_UNCERTAIN:
        return Decision(
            False,
            f"{action_type!r} with key {rendered!r} has an unknown outcome. Call "
            f"continuum_reconcile_action for it before attempting anything further.",
        )
    return Decision(
        False,
        f"the previous attempt of {action_type!r} with key {rendered!r} is closed "
        f"(status {action.status.value if action else 'unknown'}). Claim it again "
        f"through continuum_intercept_action before retrying.",
    )
