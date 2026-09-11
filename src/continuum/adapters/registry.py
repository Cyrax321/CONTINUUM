"""Adapter registry and discovery for the recovery funnel.

CONTINUUM ships several agent-framework adapters (generic, langchain,
langgraph, openai). Importing every one up front would pull in heavy optional
dependencies, so the registry stores *lazy* factories and only imports an
adapter when it is actually requested. That keeps the funnel discoverable by
name while staying import-safe in minimal environments.

The single recovery entry point is :func:`recover`: given an adapter name and a
run, it looks the adapter up, instantiates it, and delegates to its
``resume`` method, returning a framework-agnostic :class:`RecoveryDecision`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from continuum.models import EnvironmentSnapshot
from continuum.recovery.engine import RecoveryDecision
from continuum.storage import Storage


class AdapterRegistry:
    """Maps adapter names to lazy factories that return adapter classes."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], type]] = {}

    def register(self, name: str, factory: Callable[[], type]) -> None:
        """Register ``factory`` under ``name`` (replaces any prior entry)."""
        self._factories[name] = factory

    def get(self, name: str) -> type:
        """Return the adapter class registered under ``name``.

        Raises ``ValueError`` if no adapter is registered under that name.
        """
        factory = self._factories.get(name)
        if factory is None:
            known = ", ".join(sorted(self._factories)) or "<none>"
            raise ValueError(f"no adapter registered under {name!r}; known adapters: {known}")
        return factory()

    def names(self) -> list[str]:
        """Sorted names of all registered adapters."""
        return sorted(self._factories)

    def all(self) -> Mapping[str, type]:
        """Instantiate every registered adapter, keyed by name."""
        return {name: self.get(name) for name in self.names()}


_REGISTRY = AdapterRegistry()


def _generic() -> type:
    from continuum.adapters.generic import GenericAgentAdapter

    return GenericAgentAdapter


def _langchain() -> type:
    from continuum.adapters.langchain import LangChainAgentAdapter

    return LangChainAgentAdapter


def _langgraph() -> type:
    from continuum.adapters.langgraph import LangGraphAgentAdapter

    return LangGraphAgentAdapter


def _openai() -> type:
    from continuum.adapters.openai import OpenAIAgentAdapter

    return OpenAIAgentAdapter


_REGISTRY.register("generic", _generic)
_REGISTRY.register("langchain", _langchain)
_REGISTRY.register("langgraph", _langgraph)
_REGISTRY.register("openai", _openai)


def register_adapter(name: str, factory: Callable[[], type]) -> None:
    """Register a lazy adapter factory (``() -> adapter class``) by name."""
    _REGISTRY.register(name, factory)


def get_adapter(name: str) -> type:
    """Return the adapter class registered under ``name``."""
    return _REGISTRY.get(name)


def list_adapters() -> list[str]:
    """Names of all registered adapters."""
    return _REGISTRY.names()


def recover(
    adapter_name: str,
    run_id: str,
    storage: Storage,
    *,
    current_environment: EnvironmentSnapshot | None = None,
    expected_model: str | None = None,
    replay: bool = True,
    **adapter_kwargs: Any,
) -> RecoveryDecision:
    """Recover ``run_id`` through the named adapter, framework-agnostically.

    Looks the adapter up by name, instantiates it with ``storage`` (plus any
    adapter-specific keyword arguments), and delegates to its ``resume`` method.
    This is the single recovery entry point of the funnel: the caller does not
    need to know which framework produced the run.
    """
    adapter_cls = get_adapter(adapter_name)
    adapter = adapter_cls(storage, **adapter_kwargs)
    return cast(
        RecoveryDecision,
        adapter.resume(
            run_id,
            current_environment=current_environment,
            expected_model=expected_model,
            replay=replay,
        ),
    )
