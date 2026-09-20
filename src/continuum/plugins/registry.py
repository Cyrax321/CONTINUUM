"""A small dependency-injected plugin registry for CONTINUUM extensions.

Adapted from the Cordis-style "everything is a plugin" model described in
references/integration-architecture.md. Plugins register named services;
consumers resolve them by name and type. Registration is reversible: the
returned handle can tear down exactly what a plugin contributed, so loading a
plugin at runtime cannot leak state into the host.
"""

from __future__ import annotations

from typing import Any, TypeVar

__all__ = [
    "Registration",
    "Registry",
]

T = TypeVar("T")


class Registration:
    """Handle to a single registration, reversible via ``unregister``."""

    __slots__ = ("_name", "_registry")

    def __init__(self, registry: Registry, name: str) -> None:
        self._registry = registry
        self._name = name

    def unregister(self) -> None:
        self._registry.unregister(self._name)


class Registry:
    """Maps names to plugin services and resolves them by type."""

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any) -> Registration:
        """Register ``service`` under ``name``. Returns a reversible handle."""
        if not name:
            raise ValueError("a plugin service needs a non-empty name")
        if name in self._services:
            raise ValueError(f"service {name!r} is already registered")
        self._services[name] = service
        return Registration(self, name)

    def get(self, name: str, type_: type[T]) -> T:
        """Resolve ``name`` as ``type_``. Raises ``KeyError``/``TypeError``."""
        if name not in self._services:
            raise KeyError(f"no service registered under {name!r}")
        service = self._services[name]
        if not isinstance(service, type_):
            raise TypeError(
                f"service {name!r} is {type(service).__name__}, expected {type_.__name__}"
            )
        return service

    def get_optional(self, name: str, type_: type[T]) -> T | None:
        try:
            return self.get(name, type_)
        except (KeyError, TypeError):
            return None

    def unregister(self, name: str) -> None:
        self._services.pop(name, None)

    def all_of(self, type_: type[T]) -> list[T]:
        """Every registered service that is an instance of ``type_``."""
        return [s for s in self._services.values() if isinstance(s, type_)]

    def all_matching(self, protocol: Any) -> list[Any]:
        """Every registered service that is an instance of ``protocol``.

        ``all_of`` wants a concrete class for its return type, and a seam is a
        runtime-checkable :class:`~typing.Protocol`, which is not one. This is
        the Protocol-shaped spelling, and the services it returns still need an
        ``isinstance`` narrowing at the call site because a Protocol is all
        structure and no identity (issue #761).
        """
        return [s for s in self._services.values() if isinstance(s, protocol)]

    def __contains__(self, name: str) -> bool:
        return name in self._services

    def __len__(self) -> int:
        return len(self._services)
