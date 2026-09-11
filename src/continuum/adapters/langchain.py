"""LangChain adapter for CONTINUUM.

Integrates CONTINUUM's durability, checkpointing, action ledger, and recovery
with LangChain's LCEL runnable pipelines.

The adapter is optional: ``langchain-core`` is not installed by default. Import
this module only after installing the ``langchain`` extra.

Usage
-----

.. code-block:: python

    from continuum.adapters.langchain import LangChainAgentAdapter
    from langchain_core.runnables import RunnableLambda

    adapter = LangChainAgentAdapter(storage)

    # Wrap external tools for idempotency
    @adapter.wrap_tool("github.create_issue")
    def create_issue(title: str, continuum_run_id: str = "") -> dict:
        ...

    # A checkpoint node to drop into a runnable pipeline
    chain = RunnableLambda(process) | RunnableLambda(adapter.checkpoint_node)

Design
------
LangChain pipelines pass state as dicts between runnables. CONTINUUM's role is
to add *semantic durability*: the action ledger prevents duplicate side effects
across pipeline invocations, and the recovery engine validates that resumed
state is still consistent with the environment.

The adapter does NOT replace LangChain's own persistence (e.g. checkpointers).
The two serve different purposes:

* **LangChain checkpointer** (if used) snapshots pipeline state for resumption.
* **CONTINUUM** adds verified semantic state with environment validation and
  exactly-once side effect guarantees.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from continuum.adapters.generic import GenericAgentAdapter
from continuum.events import EventType
from continuum.models import (
    EnvironmentSnapshot,
    Origin,
    Run,
    SemanticState,
)
from continuum.recovery.engine import RecoveryDecision
from continuum.storage.base import RunNotFound, Storage

__all__ = [
    "LangChainAgentAdapter",
    "LangChainState",
    "langchain_available",
]

try:
    import langchain_core  # noqa: F401

    langchain_available = True
except ImportError:
    langchain_available = False


def _ensure_langchain() -> None:
    if not langchain_available:
        raise ImportError(
            "langchain-core is required for LangChainAgentAdapter. "
            "Install it with: pip install continuum-agent[langchain]"
        )


@runtime_checkable
class LangChainState(Protocol):
    """Protocol for LangChain state objects that carry CONTINUUM run metadata.

    LangChain pipelines pass state as dicts. To integrate with CONTINUUM, the
    state should include at minimum a ``continuum_run_id`` key. The adapter uses
    this to associate pipeline state with a CONTINUUM run.

    Example::

        state = {
            "continuum_run_id": "run_1",
            "goal": "process orders",
            "completed_count": 3,
            "total_count": 10,
        }
    """

    def __getitem__(self, key: str) -> Any: ...
    def __setitem__(self, key: str, value: Any) -> None: ...
    def __contains__(self, key: str) -> bool: ...
    def get(self, key: str, default: Any = None) -> Any: ...


class LangChainAgentAdapter(GenericAgentAdapter):
    """CONTINUUM adapter for LangChain agent runtimes.

    Extends :class:`GenericAgentAdapter` with LangChain-friendly helpers: tool
    wrapping for idempotent side effects and a checkpoint node that drops into
    an LCEL pipeline.

    Parameters
    ----------
    storage:
        CONTINUUM storage backend.
    state_to_semantic:
        Optional callable that converts a LangChain state dict to
        :class:`SemanticState`. If not provided, a default extractor is used
        that reads ``continuum_run_id`` and ``goal`` from state.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        state_to_semantic: Callable[[dict[str, Any]], SemanticState] | None = None,
        auto_file: str | None = None,
        auto_total: int | None = None,
    ) -> None:
        _ensure_langchain()
        super().__init__(storage, auto_file=auto_file, auto_total=auto_total)
        self._state_to_semantic = state_to_semantic

    def start_run(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Run:
        """Start a run and record its ``RUN_STARTED`` event.

        ``GenericAgentAdapter.start_run`` only creates the run row, but
        projection, replay, and restore require the ``RUN_STARTED`` event to
        exist as the log's first entry. Without it, ``checkpoint_node`` fails
        with "the log never recorded RUN_STARTED". The event is backfilled only
        when the log is empty; a non-empty log whose first event is not
        ``RUN_STARTED`` is refused to avoid misordering the run's history.
        """
        run = (
            Run(goal=goal, metadata=dict(metadata or {}))
            if run_id is None
            else Run(run_id=run_id, goal=goal, metadata=dict(metadata or {}))
        )
        try:
            existing = self.storage.get_run(run.run_id)
            run = existing
        except RunNotFound:
            run = self.storage.create_run(run)

        first = self.storage.read_events(run.run_id, upto=1)
        if not first:
            self.storage.append_event(
                run.run_id,
                EventType.RUN_STARTED,
                {"goal": run.goal},
                source=Origin.DETERMINISTIC,
            )
        elif first[0].type is not EventType.RUN_STARTED:
            raise ValueError(
                f"run {run.run_id!r} does not begin with RUN_STARTED "
                f"(first event is {first[0].type.value}). CONTINUUM cannot backfill "
                f"it after the fact without misordering the run's history; recreate "
                f"the run, or record RUN_STARTED before any other event."
            )
        return run

    def wrap_tool(
        self,
        action_type: str,
        *,
        arguments_fn: Callable[..., Mapping[str, Any]] | None = None,
        volatile: Sequence[str] = (),
        scoped_to_run: bool = True,
        key: str | None = None,
        key_fn: Callable[..., str] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator that wraps a LangChain tool with action ledger interception.

        The wrapped function will be idempotent across pipeline invocations: if
        the action already completed, the cached result is returned without
        re-executing the tool.

        Parameters
        ----------
        action_type:
            Stable identifier for the action (e.g. ``"github.create_issue"``).
        arguments_fn:
            Extracts a deterministic argument dict from the tool's kwargs.
            Defaults to passing all kwargs (minus the run id) as arguments.
        volatile:
            Argument keys excluded from idempotency hashing.
        scoped_to_run:
            Whether the idempotency key is scoped to the current run.
        key:
            A fixed explicit idempotency key (e.g. ``"notify:O-9"``). Use this
            when the operation's identity is known up front and must not depend
            on the (possibly drifting) argument text an LLM produces.
        key_fn:
            Computes the explicit key from the tool's ``(*args, **kwargs)``.
            Used when the key depends on the call (e.g. ``lambda *a, **k:
            f"notify:{k['order_id']}"``). Mutually exclusive with ``key``.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if key is not None and key_fn is not None:
                raise ValueError("wrap_tool accepts 'key' or 'key_fn', not both")

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                run_id = _extract_run_id(args, kwargs)
                if run_id is None:
                    return fn(*args, **kwargs)

                arguments = (
                    arguments_fn(*args, **kwargs)
                    if arguments_fn is not None
                    else {k: v for k, v in kwargs.items() if k != "continuum_run_id"}
                )

                explicit_key = key_fn(*args, **kwargs) if key_fn is not None else key

                return self.intercept_action(
                    run_id,
                    action_type,
                    lambda: fn(*args, **kwargs),
                    arguments=arguments,
                    volatile=volatile,
                    scoped_to_run=scoped_to_run,
                    key=explicit_key,
                )

            wrapper.__name__ = fn.__name__
            wrapper.__doc__ = fn.__doc__
            wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
            return wrapper

        return decorator

    def checkpoint_node(self, state: dict[str, Any]) -> dict[str, Any]:
        """LangChain runnable that creates a CONTINUUM checkpoint.

        Add this as a step in an LCEL pipeline at points where you want durable
        semantic state. It reads the run ID from state, projects semantic state,
        and writes a checkpoint, then returns the state unchanged so the
        pipeline can continue.

        Parameters
        ----------
        state:
            The current LangChain state dict. Must contain
            ``continuum_run_id``.

        Returns
        -------
        dict
            The same state dict, unchanged (side effect only on CONTINUUM's
            storage).
        """
        run_id = state.get("continuum_run_id")
        if not run_id:
            return state

        # Capture the run's authoritative projected state when its event log
        # already carries work. Building the state from the dict fields instead
        # (the old behaviour) left source_sequence=0, so restore(replay=False)
        # returned a synthetic state that contradicted the log, and
        # restore(replay=True) discarded the checkpoint entirely (it replayed
        # the whole log). A fresh run has no events yet, so the dict is the only
        # truth available and we fall back to extracting from it. See issue #46.
        try:
            has_events = bool(self.storage.read_events(run_id))
        except RunNotFound:
            has_events = False

        if has_events:
            semantic_state = self.manager.project_current(run_id)
        else:
            semantic_state = self._extract_semantic_state(state)

        self.capture_state(run_id, semantic_state, reason="langchain checkpoint node")
        return state

    def extract_semantic_state(self, state: dict[str, Any]) -> SemanticState:
        """Convert a LangChain state dict to CONTINUUM SemanticState."""
        if self._state_to_semantic is not None:
            return self._state_to_semantic(state)
        return self._extract_semantic_state(state)

    def _extract_semantic_state(self, state: dict[str, Any]) -> SemanticState:
        from continuum.models import Goal, Progress

        run_id = state.get("continuum_run_id", "unknown")
        goal_desc = state.get("goal", "LangChain agent task")
        completed = state.get("completed_count", 0)
        total = state.get("total_count")

        return SemanticState(
            run_id=run_id,
            goal=Goal(description=str(goal_desc)),
            progress=Progress(
                completed=int(completed),
                total=int(total) if total is not None else None,
            ),
        )

    def assess_langchain_recovery(
        self,
        run_id: str,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
    ) -> RecoveryDecision:
        """Assess whether a LangChain run can safely resume.

        Use this before re-invoking a pipeline after a crash or interruption.
        The returned decision includes the recovery mode and a repair plan.

        Example
        -------
        .. code-block:: python

            decision = adapter.assess_langchain_recovery("run_42")
            if decision.safe:
                chain.invoke(input)
            else:
                print(decision.render())
        """
        return self.resume(
            run_id,
            current_environment=current_environment,
            expected_model=expected_model,
        )


def _extract_run_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Try to find a continuum_run_id from function arguments.

    Checks kwargs first, then the first positional argument if it's a dict
    with the key.
    """
    if "continuum_run_id" in kwargs:
        return str(kwargs["continuum_run_id"])
    if "run_id" in kwargs:
        return str(kwargs["run_id"])
    if args and isinstance(args[0], dict):
        run_id = args[0].get("continuum_run_id")
        if run_id:
            return str(run_id)
    return None
