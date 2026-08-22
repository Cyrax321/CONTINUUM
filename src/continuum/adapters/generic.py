"""Concrete GenericAgentAdapter for standard Python agent loops."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from continuum.actions.ledger import ActionLedger, ActionOutcome
from continuum.adapters.base import AgentAdapter
from continuum.checkpoint.manager import CheckpointManager
from continuum.events import EventType
from continuum.models import (
    EnvironmentSnapshot,
    Origin,
    Run,
    SemanticState,
    StateCheckpoint,
)
from continuum.recovery.engine import RecoveryDecision, RecoveryEngine
from continuum.state.semantic import ProjectionError, project
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

    def __init__(
        self,
        storage: Storage,
        *,
        engine: RecoveryEngine | None = None,
        auto_file: str | None = None,
        auto_total: int | None = None,
    ) -> None:
        self.storage = storage
        self.manager = CheckpointManager(storage)
        self.engine = engine or RecoveryEngine(storage)
        self.auto_file = auto_file
        self.auto_total = auto_total

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
        # A snapshot alone cannot invalidate a checkpoint: the validator decides
        # staleness per declared dependency and returns early when a state has
        # none, so a checkpoint carrying only a snapshot would report
        # safe_to_resume even after the resource underneath it moved. Declaring
        # the pinned environment as dependencies is what gives drift something
        # to invalidate. Mirrors the MCP server and serve sidecar (issue #25).
        if environment is not None:
            self._declare_dependencies(run_id, environment)
        # Auto mode: the file is ground truth for progress. The derived events
        # are appended before the checkpoint so the checkpoint captures them,
        # and record_file_progress is a no-op when the count is unchanged.
        if self.auto_file is not None and self.auto_total is not None:
            from continuum.hooks import record_file_progress

            record_file_progress(self.manager, run_id, self.auto_file, self.auto_total)
            # Reproject so the checkpoint captures the derived progress
            state = project(run_id, self.storage.read_events(run_id))
        return self.manager.checkpoint(
            run_id,
            state=state,
            reason=reason or "adapter state capture",
            environment=environment,
        )

    def _declare_dependencies(self, run_id: str, environment: EnvironmentSnapshot) -> None:
        """Record a captured environment as declared dependencies of the run.

        Stored as ``DEPENDENCY_DECLARED`` events (not written onto the
        checkpoint state) so the declaration survives projection and restore, is
        covered by the hash chain, and carries the same external-agent provenance
        as the rest of the adapter's writes. Only new or re-pinned resources are
        appended, so a scheduled checkpoint with an unchanged environment adds
        nothing.
        """
        env_map = {name: str(res.version) for name, res in environment.resources.items()}
        if not env_map:
            return
        # Projecting prior declarations is an optimization (skip re-pinning the
        # same version). If the run has no goal yet, projection is impossible, so
        # fall back to declaring everything; project folds duplicates later.
        try:
            declared = {
                dependency.resource: dependency.version
                for dependency in project(
                    run_id, self.storage.read_events(run_id)
                ).external_dependencies
            }
        except ProjectionError:
            declared = {}
        for name, version in env_map.items():
            if declared.get(name) == version:
                continue
            self.storage.append_event(
                run_id,
                EventType.DEPENDENCY_DECLARED,
                {"resource": name, "version": version},
                source=Origin.EXTERNAL_AGENT,
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
        key: str | None = None,
        dep_scope: str | None = None,
    ) -> Any:
        """Intercept and safely execute an external side effect.

        1. Claims the action in the ActionLedger.
        2. If already completed (fresh=False), returns the cached result without executing action_fn.
        3. If fresh=True, executes action_fn().
        4. If action_fn() succeeds, completes the ledger claim and returns the result.
        5. If action_fn() raises, records the outcome as *uncertain* and re-raises.

        ``key`` is an explicit, Stripe-style idempotency key (e.g. ``"notify:O-9"``)
        that identifies the operation independently of its argument text. Prefer it
        over argument-hash dedup whenever the caller (an LLM especially) may render
        equivalent operations with drifting argument spellings, since a changed
        argument string produces a different hash and silently defeats dedup.

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
            key=key,
            dep_scope=dep_scope,
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
        self._auto_progress(run_id)
        return val

    def _auto_progress(self, run_id: str) -> None:
        """Fire the opt-in auto hooks after a turn without blocking the caller.

        Mirrors the file into the log when the derived count changed (cheap:
        one small read plus an event-log comparison), then lets the policy
        decide whether a checkpoint write is due. The write itself goes to the
        shared background executor, so the agent's turn is never blocked on
        SQLite I/O. This is what makes durability automatic for every harness
        using the adapter: no model tool call and no prompt mention of
        CONTINUUM required (issue 191).
        """
        if self.auto_file is None or self.auto_total is None:
            return
        from continuum.hooks import make_async_file_derived_progress_hook

        hook = make_async_file_derived_progress_hook(
            self.manager, run_id, self.auto_file, self.auto_total
        )
        with contextlib.suppress(Exception):  # durability must never break the turn
            hook()

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
