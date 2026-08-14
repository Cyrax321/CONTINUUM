"""Concrete GenericAgentAdapter for standard Python agent loops."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from continuum.actions.ledger import ActionLedger, ActionOutcome
from continuum.adapters.base import AgentAdapter
from continuum.checkpoint.manager import CheckpointManager
from continuum.models import (
    EnvironmentSnapshot,
    Run,
    SemanticState,
    StateCheckpoint,
)
from continuum.recovery.engine import RecoveryDecision, RecoveryEngine
from continuum.storage.base import Storage

#: Key under which a non-dict action result is stored, since the ledger records
#: results as mappings. A caller's own dict is wrapped in the same envelope when
#: it contains this key, so the presence of the key in a stored result always
#: means "envelope" and never "caller data".
RESULT_ENVELOPE_KEY = "__return_value__"

__all__ = ["GenericAgentAdapter", "RESULT_ENVELOPE_KEY"]


class GenericAgentAdapter(AgentAdapter):
    """Concrete adapter wrapping CONTINUUM primitives for generic Python agents.

    Provides a clean, high-level API so caller code does not need to construct
    or interact directly with storage handles, event log streams, or ledger objects.
    """

    def __init__(self, storage: Storage, *, engine: RecoveryEngine | None = None) -> None:
        self.storage = storage
        self.manager = CheckpointManager(storage)
        self.engine = engine or RecoveryEngine(storage)

    def start_run(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Run:
        """Helper to create and initialize a new task run."""
        run = (
            Run(goal=goal, metadata=dict(metadata or {}))
            if run_id is None
            else Run(run_id=run_id, goal=goal, metadata=dict(metadata or {}))
        )
        return self.storage.create_run(run)

    def capture_state(
        self,
        run_id: str,
        state: SemanticState,
        *,
        environment: EnvironmentSnapshot | None = None,
        reason: str = "",
    ) -> StateCheckpoint:
        return self.manager.checkpoint(
            run_id,
            state=state,
            reason=reason or "adapter state capture",
            environment=environment,
        )

    def restore_state(
        self,
        run_id: str,
        *,
        replay: bool = True,
    ) -> SemanticState:
        restored = self.manager.restore(run_id, replay=replay)
        return restored.state

    def intercept_action(
        self,
        run_id: str,
        action_type: str,
        action_fn: Callable[[], Any],
        arguments: Mapping[str, Any] | None = None,
        *,
        volatile: Sequence[str] = (),
        scoped_to_run: bool = True,
        on_unknown: Callable[[Any], ActionOutcome | None] | None = None,
    ) -> Any:
        """Intercept and safely execute an external side effect.

        1. Claims the action in the ActionLedger.
        2. If already completed (fresh=False), returns the cached result without executing action_fn.
        3. If fresh=True, executes action_fn().
        4. If action_fn() succeeds, completes the ledger claim and returns the result.
        5. If action_fn() raises, records the outcome as *uncertain* and re-raises.

        Step 5 is deliberately conservative. An exception escaping an external
        call does not prove the side effect failed to occur: a timeout or a
        dropped connection means the request may already have landed. Recording
        it as a definite failure would remove it from ``ledger.pending()``, hide
        it from reconciliation, and let a later retry duplicate the effect —
        exactly the hazard the ledger exists to prevent.

        There is no exception type in this layer that reliably proves nothing
        happened. ``action_fn`` is an opaque callable, so a ``ValueError`` may
        equally come from argument validation *before* any network call or from
        parsing a response *after* the server already acted. Distinguishing them
        requires knowledge only the caller has, so the default is uncertainty
        and the caller narrows it via ``reconcile_pending``.
        """
        ledger = ActionLedger(self.storage, run_id)
        outcome = ledger.claim(
            action_type,
            arguments=arguments,
            volatile=volatile,
            scoped_to_run=scoped_to_run,
            on_unknown=on_unknown,
        )

        if not outcome.fresh:
            res = outcome.result
            # One level of unwrapping, matching the one level the fresh path
            # below applies. A stored dict that carries the key is always an
            # envelope, never a caller's own dict, because the fresh path wraps
            # those too.
            if isinstance(res, dict) and RESULT_ENVELOPE_KEY in res:
                return res[RESULT_ENVELOPE_KEY]
            return res

        try:
            val = action_fn()
        except Exception as exc:
            # certain=False: the effect may or may not have occurred. This keeps
            # the action in ledger.pending() so recovery blocks until a probe
            # settles it, rather than silently permitting a duplicate retry.
            ledger.fail(outcome.key, f"{type(exc).__name__}: {exc}", certain=False)
            raise

        # A non-dict has to be wrapped to be stored. So does a dict that happens
        # to contain the envelope key: storing that one as-is would make the
        # cached path unwrap the caller's own dict and return only that member,
        # so a completed action would return a different value on its second
        # call than on its first.
        needs_envelope = not isinstance(val, dict) or RESULT_ENVELOPE_KEY in val
        result_dict = {RESULT_ENVELOPE_KEY: val} if needs_envelope else val
        ledger.complete(outcome.key, result=result_dict)
        return val

    def resume(
        self,
        run_id: str,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
        replay: bool = True,
    ) -> RecoveryDecision:
        return self.engine.assess(
            run_id,
            current_environment=current_environment,
            expected_model=expected_model,
            replay=replay,
        )
