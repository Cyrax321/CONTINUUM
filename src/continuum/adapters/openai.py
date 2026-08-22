"""OpenAI Agents SDK adapter for CONTINUUM.

Integrates CONTINUUM's durability, checkpointing, action ledger, and recovery
with the OpenAI Agents SDK (``openai-agents`` package).

The adapter is optional — ``openai-agents`` is not installed by default. Import
this module only after installing the ``openai`` extra.

Usage
-----

.. code-block:: python

    from continuum.adapters.openai import OpenAIAgentAdapter, ContinuumContext
    from agents import Agent, Runner, function_tool

    adapter = OpenAIAgentAdapter(storage)

    # Define a context object carrying the run ID
    ctx = ContinuumContext(continuum_run_id="run_42", goal="Analyze documents")

    # Wrap external tools for idempotency
    @adapter.wrap_function_tool("github.create_issue")
    def create_issue(title: str, body: str) -> dict:
        ...

    # Create a checkpoint hook for the agent lifecycle
    hooks = adapter.create_run_hooks()

    result = await Runner.run(
        starting_agent=agent,
        input="...",
        context=ctx,
        hooks=hooks,
    )

Design
------
The OpenAI Agents SDK uses:
- **ToolContext** (auto-injected) — we use it to carry run metadata
- **RunHooks** — lifecycle callbacks we implement for checkpointing
- **RunContextWrapper.context** — our custom ``ContinuumContext`` carries the run ID
- **function_tool** — we wrap these with action ledger interception

CONTINUUM does NOT replace the SDK's session/compaction mechanisms. It adds:
- Idempotent side effects via the action ledger
- Semantic checkpoint validation against the environment
- Recovery decisions before resuming interrupted runs
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from continuum.adapters.generic import GenericAgentAdapter
from continuum.models import (
    EnvironmentSnapshot,
    SemanticState,
    StateCheckpoint,
)
from continuum.recovery.engine import RecoveryDecision
from continuum.storage.base import RunNotFound, Storage

__all__ = [
    "OpenAIAgentAdapter",
    "ContinuumContext",
    "openai_agents_available",
]

try:
    from agents import RunContextWrapper, RunHooks, function_tool  # noqa: F401
    from agents.tool_context import ToolContext  # noqa: F401

    openai_agents_available = True
except ImportError:
    openai_agents_available = False


@dataclass
class ContinuumContext:
    """Context object passed to ``Runner.run(context=...)``.

    Carries the CONTINUUM run ID and goal through the agent lifecycle.
    Accessible in tools and hooks via ``RunContextWrapper.context``.
    """

    continuum_run_id: str
    goal: str = ""
    metadata: dict[str, Any] | None = None

    def to_semantic_state(self) -> SemanticState:
        """Convert this context to a minimal SemanticState."""
        from continuum.models import Goal, Progress

        return SemanticState(
            run_id=self.continuum_run_id,
            goal=Goal(description=self.goal),
            progress=Progress(),
        )


def _ensure_openai_agents() -> None:
    if not openai_agents_available:
        raise ImportError(
            "openai-agents is required for OpenAIAgentAdapter. "
            "Install it with: pip install continuum-agent[openai]"
        )


def _format_annotation(annotation: Any) -> str:
    """Render a parameter annotation as source text for the generated wrapper.

    The wrapper is built with ``exec``, so its source must carry the original
    annotations: typing every parameter as ``Any`` (the previous behaviour)
    drops the real types, and the OpenAI Agents SDK then emits a tool JSON
    schema with no ``type`` key, which strict schema validators (OpenRouter)
    reject. ``inspect.formatannotation`` renders ``str`` as ``'str'`` and
    ``ToolContext`` as ``'ToolContext'`` (the latter is imported into the
    generated namespace), so the SDK can derive a valid schema.
    """
    if annotation is inspect.Parameter.empty:
        return "Any"
    return inspect.formatannotation(annotation)


class OpenAIAgentAdapter(GenericAgentAdapter):
    """CONTINUUM adapter for the OpenAI Agents SDK.

    Extends :class:`GenericAgentAdapter` with OpenAI Agents SDK-specific
    helpers: function tool wrapping for idempotent side effects, run hooks
    for checkpointing, and context-based run tracking.

    Parameters
    ----------
    storage:
        CONTINUUM storage backend.
    state_to_semantic:
        Optional callable that converts ``ContinuumContext`` to
        :class:`SemanticState`. If not provided, uses
        ``ContinuumContext.to_semantic_state()``.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        state_to_semantic: Callable[[ContinuumContext], SemanticState] | None = None,
        auto_file: str | None = None,
        auto_total: int | None = None,
    ) -> None:
        _ensure_openai_agents()
        super().__init__(storage, auto_file=auto_file, auto_total=auto_total)
        self._state_to_semantic = state_to_semantic

    def wrap_function_tool(
        self,
        action_type: str,
        *,
        volatile: Sequence[str] = (),
        scoped_to_run: bool = True,
        name_override: str | None = None,
        description_override: str | None = None,
        key: str | None = None,
        key_fn: Callable[..., str] | None = None,
    ) -> Callable[[Callable[..., Any]], Any]:
        """Decorate a function tool with action ledger interception.

        The decorated function must accept ``ToolContext`` as its first
        parameter (the SDK auto-injects it). The ``continuum_run_id`` is
        read from ``ToolContext.tool_input`` if it's a dict containing the key,
        otherwise from ``RunContextWrapper.context.continuum_run_id``.

        Parameters
        ----------
        action_type:
            Stable identifier for the action (e.g. ``"github.create_issue"``).
        volatile:
            Argument keys excluded from idempotency hashing.
        scoped_to_run:
            Whether the idempotency key is scoped to the current run.
        name_override:
            Override the tool name registered with the SDK.
        description_override:
            Override the tool description registered with the SDK.
        key:
            A fixed explicit idempotency key (e.g. ``"issue:42"``). Use when the
            operation's identity is known up front and must not depend on the
            (possibly drifting) argument text an LLM produces.
        key_fn:
            Computes the explicit key from the tool call (``ctx`` followed by the
            tool's positional arguments). Used when the key depends on the call.
            Mutually exclusive with ``key``.

        Example
        -------
        .. code-block:: python

            @adapter.wrap_function_tool("github.create_issue", key="issue:42")
            def create_issue(ctx: ToolContext, title: str, body: str) -> dict:
                return github_client.create_issue(title=title, body=body)

            # If the action already completed, the cached result is returned
            # without calling the external system.
        """
        if key is not None and key_fn is not None:
            raise ValueError("wrap_function_tool accepts 'key' or 'key_fn', not both")
        from agents import function_tool

        def decorator(fn: Callable[..., Any]) -> Any:
            tool_name = name_override or fn.__name__
            tool_desc = description_override or fn.__doc__ or ""

            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            if not params or params[0].name != "ctx":
                raise ValueError(
                    f"Function {fn.__name__!r} must have 'ctx: ToolContext' as its "
                    f"first parameter for CONTINUUM interception."
                )

            tool_params = params[1:]

            # The OpenAI Agents SDK detects a context parameter by inspecting the
            # function signature: the first parameter must be annotated as
            # ``RunContextWrapper`` (or ``ToolContext``) for the SDK to pass the
            # run context and drop it from the tool's JSON schema. If we override
            # ``__signature__`` without keeping ``ctx`` first, the SDK concludes
            # the tool takes no context and feeds the raw tool-input string as the
            # first positional argument instead, which breaks run-id extraction
            # and silently bypasses interception. So ``ctx`` stays the first
            # parameter, annotated ``RunContextWrapper``.
            ctx_param = inspect.Parameter(
                "ctx",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=RunContextWrapper,
            )
            dynamic_params = [ctx_param] + [
                inspect.Parameter(
                    p.name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=p.annotation
                    if p.annotation is not inspect.Parameter.empty
                    else inspect.Parameter.empty,
                    default=p.default,
                )
                for p in tool_params
            ]
            dynamic_sig = inspect.Signature(dynamic_params)

            def make_wrapper(params: list[inspect.Parameter]) -> Callable[..., Any]:
                param_names = [p.name for p in params]
                param_str = ", ".join(param_names)
                param_decls = ", ".join(
                    f"{p.name}: {_format_annotation(p.annotation)} = None" for p in params
                )

                code = f"""
def wrapped_tool(ctx: RunContextWrapper, {param_decls}):
    arguments = {{{", ".join(f'"{p.name}": {p.name}' for p in params)}}}
    run_id = _extract_run_id_from_tool_context(ctx)
    if run_id is None:
        return _call_original(ctx, {param_str})
    explicit_key = _key_fn(ctx, {param_str}) if _key_fn is not None else _key
    return _adapter_ref.intercept_action(
        run_id,
        _action_type,
        lambda: _call_original(ctx, {param_str}),
        arguments=arguments,
        volatile=_volatile,
        scoped_to_run=_scoped_to_run,
        key=explicit_key,
    )
"""
                namespace: dict[str, Any] = {
                    "RunContextWrapper": RunContextWrapper,
                    "_extract_run_id_from_tool_context": _extract_run_id_from_tool_context,
                    "_call_original": fn,
                    "_adapter_ref": self,
                    "_action_type": action_type,
                    "_volatile": volatile,
                    "_scoped_to_run": scoped_to_run,
                    "_key": key,
                    "_key_fn": key_fn,
                    "Any": Any,
                }
                exec(code, namespace)
                return cast(Callable[..., Any], namespace["wrapped_tool"])

            wrapped_fn = make_wrapper(tool_params)
            wrapped_fn.__signature__ = dynamic_sig  # type: ignore[attr-defined]
            wrapped_fn.__name__ = tool_name
            wrapped_fn.__doc__ = tool_desc

            result = function_tool(
                name_override=tool_name,
                description_override=tool_desc,
            )(wrapped_fn)
            return result

        return decorator

    def create_run_hooks(
        self,
        *,
        checkpoint_on_agent_end: bool = True,
        checkpoint_on_tool_end: bool = False,
        on_tool_start_fn: Callable[[Any, Any], None] | None = None,
        on_tool_end_fn: Callable[[Any, Any, Any], None] | None = None,
    ) -> Any:
        """Create a ``RunHooks`` instance that integrates with CONTINUUM.

        Returns a ``RunHooks`` subclass that:
        - On agent end: creates a semantic checkpoint (if ``checkpoint_on_agent_end``)
        - On tool end: optionally creates a checkpoint (if ``checkpoint_on_tool_end``)

        Parameters
        ----------
        checkpoint_on_agent_end:
            Whether to checkpoint when an agent finishes.
        checkpoint_on_tool_end:
            Whether to checkpoint after each tool execution.
        on_tool_start_fn:
            Optional callback invoked on tool start, receives (context, tool).
        on_tool_end_fn:
            Optional callback invoked on tool end, receives (context, tool, result).
        """
        from agents import RunHooks

        adapter = self

        class ContinuumRunHooks(RunHooks):
            """RunHooks that create CONTINUUM checkpoints at agent lifecycle events."""

            async def on_agent_start(self, context: Any, agent: Any) -> None:
                run_id = _extract_run_id(context)
                if run_id:
                    adapter._ensure_run_exists(run_id, agent)

            async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
                if not checkpoint_on_agent_end:
                    return
                run_id = _extract_run_id(context)
                if run_id:
                    adapter._checkpoint_from_context(run_id, context, agent, output)

            async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
                if on_tool_start_fn is not None:
                    on_tool_start_fn(context, tool)

            async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
                if checkpoint_on_tool_end:
                    run_id = _extract_run_id(context)
                    if run_id:
                        adapter._checkpoint_from_context(run_id, context, agent, result)
                if on_tool_end_fn is not None:
                    on_tool_end_fn(context, tool, result)

        return ContinuumRunHooks()

    def create_semantic_state(
        self,
        context: ContinuumContext,
        *,
        extra_state: SemanticState | None = None,
    ) -> SemanticState:
        """Create a SemanticState from a ContinuumContext.

        Uses the ``state_to_semantic`` callable provided at construction,
        or falls back to ``ContinuumContext.to_semantic_state()``.
        """
        if self._state_to_semantic is not None:
            return self._state_to_semantic(context)
        state = context.to_semantic_state()
        if extra_state is not None:
            state = extra_state.model_copy(update={"run_id": state.run_id})
        return state

    def assess_agent_recovery(
        self,
        run_id: str,
        *,
        current_environment: EnvironmentSnapshot | None = None,
        expected_model: str | None = None,
    ) -> RecoveryDecision:
        """Assess whether an OpenAI Agents SDK run can safely resume.

        Use this before re-running an agent after a crash or interruption.
        """
        return self.resume(
            run_id,
            current_environment=current_environment,
            expected_model=expected_model,
        )

    def _ensure_run_exists(self, run_id: str, agent: Any) -> None:
        """Create the run record and its ``RUN_STARTED`` event if absent.

        ``get_run`` raises ``RunNotFound`` for an absent run (it does not
        return ``None``), so the missing-run case is caught here rather than
        checked with an ``is not None`` guard. This is what lets a fresh
        OpenAI agent run auto-provision instead of failing on first contact
        (see issue #21).

        The run row and the ``RUN_STARTED`` event are separate facts: projection
        needs the event, and without it ``restore``/``project``/``replay`` fail
        with "the log never recorded RUN_STARTED". So, mirroring
        ``ContinuumMCP.ensure_run``, the event is backfilled only when the log
        is empty, and a non-empty log whose first event is not ``RUN_STARTED``
        is refused rather than silently misordered.
        """
        from continuum.events import EventType
        from continuum.models import Origin, Run, RunStatus

        goal = getattr(agent, "name", "OpenAI agent task")
        try:
            run = self.storage.get_run(run_id)
        except RunNotFound:
            run = Run(run_id=run_id, goal=goal, status=RunStatus.STARTED)
            self.storage.create_run(run)

        first = self.storage.read_events(run_id, upto=1)
        if not first:
            self.storage.append_event(
                run_id,
                EventType.RUN_STARTED,
                {"goal": run.goal},
                source=Origin.EXTERNAL_AGENT,
            )
        elif first[0].type is not EventType.RUN_STARTED:
            raise ValueError(
                f"run {run_id!r} does not begin with RUN_STARTED "
                f"(first event is {first[0].type.value}). CONTINUUM cannot backfill it "
                f"after the fact without misordering the run's history; recreate the "
                f"run, or record RUN_STARTED before any other event."
            )

    def _checkpoint_from_context(
        self,
        run_id: str,
        context: Any,
        agent: Any,
        output: Any,
    ) -> StateCheckpoint | None:
        """Create a checkpoint from the current agent context."""
        semantic = self._build_semantic_state(run_id, context, agent, output)
        return self.capture_state(run_id, semantic, reason="openai agent lifecycle")

    def _build_semantic_state(
        self,
        run_id: str,
        context: Any,
        agent: Any,
        output: Any,
    ) -> SemanticState:
        """Build SemanticState from agent runtime state."""
        from continuum.models import Goal, Progress

        goal_desc = getattr(agent, "name", "OpenAI agent task")
        ctx = _extract_continuum_context(context)
        if ctx and ctx.goal:
            goal_desc = ctx.goal

        completed = 0
        if ctx and ctx.metadata:
            completed = ctx.metadata.get("completed_count", 0)

        return SemanticState(
            run_id=run_id,
            goal=Goal(description=goal_desc),
            progress=Progress(completed=completed),
        )


def _extract_run_id(wrapper: Any) -> str | None:
    """Extract continuum_run_id from a RunContextWrapper or similar."""
    if wrapper is None:
        return None
    ctx = _extract_continuum_context(wrapper)
    if ctx is not None:
        return ctx.continuum_run_id
    return None


def _extract_continuum_context(wrapper: Any) -> ContinuumContext | None:
    """Extract ContinuumContext from a RunContextWrapper."""
    if isinstance(wrapper, ContinuumContext):
        return wrapper
    if hasattr(wrapper, "context"):
        inner = wrapper.context
        if isinstance(inner, ContinuumContext):
            return inner
    return None


def _extract_run_id_from_tool_context(ctx: Any) -> str | None:
    """Extract run_id from a ToolContext.

    Checks ``tool_input`` dict first, then the parent context.
    """
    if ctx is None:
        return None
    # Check tool_input for a dict with continuum_run_id
    tool_input = getattr(ctx, "tool_input", None)
    if isinstance(tool_input, dict):
        run_id = tool_input.get("continuum_run_id")
        if run_id:
            return cast(str, run_id)
    # Fall back to agent context
    return _extract_run_id(ctx)
