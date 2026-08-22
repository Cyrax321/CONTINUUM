"""LangGraph adapter for CONTINUUM.

Integrates CONTINUUM's durability, checkpointing, action ledger, and recovery
with LangGraph's graph-based agent runtime.

The adapter is optional — ``langgraph`` is not installed by default. Import
this module only after installing the ``langgraph`` extra.

Usage
-----

.. code-block:: python

    from continuum.adapters.langgraph import LangGraphAgentAdapter
    from langgraph.graph import StateGraph

    adapter = LangGraphAgentAdapter(storage, graph=my_graph)

    # Wrap external tools for idempotency
    @adapter.wrap_tool("github.create_issue")
    def create_issue(title: str, body: str) -> dict:
        ...

    # Add a checkpoint node to the graph
    builder = StateGraph(MyState)
    builder.add_node("checkpoint", adapter.checkpoint_node)
    ...

Design
------
LangGraph manages its own state via its checkpointer. CONTINUUM's role here is
to add *semantic durability* — the action ledger prevents duplicate side effects
across graph invocations, and the recovery engine validates that resumed state
is still consistent with the environment.

The adapter does NOT replace LangGraph's checkpointer. The two serve different
purposes:

* **LangGraph checkpointer** — full state snapshots for graph resumption.
* **CONTINUUM** — verified semantic state with environment validation and
  exactly-once side effect guarantees.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast, runtime_checkable

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
    "LangGraphAgentAdapter",
    "LangGraphState",
    "langgraph_available",
]

try:
    from langgraph.graph.state import StateGraph  # noqa: F401

    langgraph_available = True
except ImportError:
    langgraph_available = False


@runtime_checkable
class LangGraphState(Protocol):
    """Protocol for LangGraph state objects that carry CONTINUUM run metadata.

    LangGraph state is a ``TypedDict``. To integrate with CONTINUUM, the state
    should include at minimum a ``continuum_run_id`` field. The adapter uses
    this to associate graph state with a CONTINUUM run.

    Example::

        class MyState(TypedDict):
            continuum_run_id: str
            messages: list[Any]
            current_step: str
            results: dict[str, Any]
    """

    def __getitem__(self, key: str) -> Any: ...
    def __setitem__(self, key: str, value: Any) -> None: ...
    def __contains__(self, key: str) -> bool: ...
    def get(self, key: str, default: Any = None) -> Any: ...


def _ensure_langgraph() -> None:
    if not langgraph_available:
        raise ImportError(
            "langgraph is required for LangGraphAgentAdapter. "
            "Install it with: pip install continuum-agent[langgraph]"
        )


class LangGraphAgentAdapter(GenericAgentAdapter):
    """CONTINUUM adapter for LangGraph agent runtimes.

    Extends :class:`GenericAgentAdapter` with LangGraph-specific helpers:
    tool wrapping for idempotent side effects, checkpoint node creation, and
    state extraction from graph snapshots.

    Parameters
    ----------
    storage:
        CONTINUUM storage backend.
    graph:
        Optional LangGraph ``StateGraph`` instance. Used by
        :meth:`checkpoint_node` to read graph state.
    state_to_semantic:
        Optional callable that converts LangGraph state dicts to
        :class:`SemanticState`. If not provided, a default extractor is used
        that reads ``continuum_run_id`` and ``goal`` from state.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        graph: Any = None,
        state_to_semantic: Callable[[dict[str, Any]], SemanticState] | None = None,
        auto_file: str | None = None,
        auto_total: int | None = None,
    ) -> None:
        _ensure_langgraph()
        super().__init__(storage, auto_file=auto_file, auto_total=auto_total)
        self.graph = graph
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
        """Decorator that wraps a LangGraph tool with action ledger interception.

        The wrapped function will be idempotent across graph invocations —
        if the action already completed, the cached result is returned without
        re-executing the tool.

        Parameters
        ----------
        action_type:
            Stable identifier for the action (e.g. ``"github.create_issue"``).
        arguments_fn:
            Extracts a deterministic argument dict from the tool's kwargs.
            Defaults to passing all kwargs as arguments.
        volatile:
            Argument keys excluded from idempotency hashing.
        scoped_to_run:
            Whether the idempotency key is scoped to the current run.
        key:
            A fixed explicit idempotency key (e.g. ``"issue:42"``). Use when the
            operation's identity is known up front and must not depend on the
            (possibly drifting) argument text an LLM produces.
        key_fn:
            Computes the explicit key from the tool's ``(*args, **kwargs)``. Used
            when the key depends on the call. Mutually exclusive with ``key``.

        Example
        -------
        .. code-block:: python

            @adapter.wrap_tool("github.create_issue")
            def create_issue(title: str, body: str) -> dict:
                return github_client.create_issue(title=title, body=body)

            # Inside a LangGraph node:
            result = create_issue(title="Bug", body="...")
            # On replay, returns cached result without calling GitHub.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if key is not None and key_fn is not None:
                raise ValueError("wrap_tool accepts 'key' or 'key_fn', not both")

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                run_id = _extract_run_id(args, kwargs)
                if run_id is None:
                    return fn(*args, **kwargs)

                arguments = (
                    arguments_fn(*args, **kwargs) if arguments_fn is not None else dict(kwargs)
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

    def checkpoint_node(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """LangGraph node function that creates a CONTINUUM checkpoint.

        Add this node to your graph at points where you want durable semantic
        state. It reads the run ID from state, projects semantic state, and
        writes a checkpoint — then returns state unchanged so the graph can
        continue.

        Parameters
        ----------
        state:
            The current LangGraph state dict. Must contain
            ``continuum_run_id``.

        Returns
        -------
        dict
            State updates (empty dict — this node is side-effect only on
            CONTINUUM's storage).

        Example
        -------
        .. code-block:: python

            builder.add_node("checkpoint", adapter.checkpoint_node)
            builder.add_edge("process_items", "checkpoint")
            builder.add_edge("checkpoint", "decide_next")
        """
        run_id = state.get("continuum_run_id")
        if not run_id:
            return {}

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
        self.capture_state(run_id, semantic_state, reason="langgraph checkpoint node")
        return {}

    def extract_semantic_state(self, state: dict[str, Any]) -> SemanticState:
        """Convert LangGraph state to CONTINUUM SemanticState.

        Uses the ``state_to_semantic`` callable provided at construction,
        or falls back to a default extraction that reads common fields.
        """
        if self._state_to_semantic is not None:
            return self._state_to_semantic(state)
        return self._extract_semantic_state(state)

    def _extract_semantic_state(self, state: dict[str, Any]) -> SemanticState:
        from continuum.models import Goal, Progress

        run_id = state.get("continuum_run_id", "unknown")
        goal_desc = state.get("goal", "LangGraph agent task")
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

    def assess_graph_recovery(
        self,
        run_id: str,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
    ) -> RecoveryDecision:
        """Assess whether a LangGraph run can safely resume.

        Use this before re-invoking a graph after a crash or interruption.
        The returned decision includes the recovery mode and a repair plan.

        Example
        -------
        .. code-block:: python

            decision = adapter.assess_graph_recovery("run_42")
            if decision.safe:
                graph.invoke(input, config={"configurable": {"thread_id": "run_42"}})
            else:
                print(decision.render())
        """
        return self.resume(
            run_id,
            current_environment=current_environment,
            expected_model=expected_model,
        )

    def revalidate_environment(
        self,
        run_id: str,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
    ) -> RecoveryDecision:
        """Revalidate a resumed checkpoint against the current environment.

        This is the "keep your LangGraph checkpointer, add the validator" entry
        point from issue #25. A LangGraph user keeps their own
        ``SqliteSaver``/``PostgresSaver`` for faithful state replay; this method
        adds CONTINUUM's staleness propagation on top of it, without replacing
        the checkpointer. Call it before resuming a graph that was restored from
        a checkpointer, passing the environment as it is *now*.

        Staleness propagates ``dependency -> evidence -> finding -> decision``
        and ``UNKNOWN`` degrades toward unsafe, so a resource that moved between
        the checkpoint and the present surfaces as a non-RESUME decision rather
        than a silent resume. The verdict is identical to :meth:`assess_graph_recovery`
        (a read-only :class:`RecoveryDecision`); this name makes the
        checkpointer-companion use case discoverable.
        """
        return self.engine.assess(
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
        return cast(str, kwargs["continuum_run_id"])
    if "run_id" in kwargs:
        return cast(str, kwargs["run_id"])
    if args and isinstance(args[0], dict):
        run_id = args[0].get("continuum_run_id")
        if run_id:
            return cast(str, run_id)
    return None
