from __future__ import annotations

import pytest

from continuum.adapters import (
    AdapterRegistry,
    GenericAgentAdapter,
    get_adapter,
    list_adapters,
    recover,
    register_adapter,
)
from continuum.adapters.langchain import LangChainAgentAdapter
from continuum.adapters.langgraph import LangGraphAgentAdapter
from continuum.adapters.openai import OpenAIAgentAdapter
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import RecoveryMode, Run
from continuum.storage import SQLiteStorage


def test_builtin_adapters_are_registered() -> None:
    names = list_adapters()
    for name in ("generic", "langchain", "langgraph", "openai"):
        assert name in names


def test_get_adapter_resolves_all_builtin_classes() -> None:
    assert get_adapter("generic") is GenericAgentAdapter
    assert get_adapter("langchain") is LangChainAgentAdapter
    assert get_adapter("langgraph") is LangGraphAgentAdapter
    assert get_adapter("openai") is OpenAIAgentAdapter


def test_unknown_adapter_raises() -> None:
    with pytest.raises(
        ValueError,
        match="no adapter registered under 'does-not-exist'; known adapters: generic, langchain, langgraph, openai",
    ):
        get_adapter("does-not-exist")

    empty_reg = AdapterRegistry()
    with pytest.raises(ValueError, match="known adapters: <none>"):
        empty_reg.get("missing")


def test_registry_register_and_get_is_local() -> None:
    reg = AdapterRegistry()
    reg.register("demo", lambda: GenericAgentAdapter)
    assert reg.get("demo") is GenericAgentAdapter
    assert "demo" in reg.names()


def test_adapter_registry_all_on_empty_registry() -> None:
    reg = AdapterRegistry()
    assert reg.all() == {}


def test_adapter_registry_all_returns_classes_keyed_by_name() -> None:
    reg = AdapterRegistry()
    reg.register("beta", lambda: LangGraphAgentAdapter)
    reg.register("alpha", lambda: LangChainAgentAdapter)
    assert reg.all() == {
        "alpha": LangChainAgentAdapter,
        "beta": LangGraphAgentAdapter,
    }


def test_register_adapter_adds_to_global_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from continuum.adapters.registry import _REGISTRY

    monkeypatch.setattr(_REGISTRY, "_factories", dict(_REGISTRY._factories))
    register_adapter("custom", lambda: GenericAgentAdapter)
    assert get_adapter("custom") is GenericAgentAdapter
    assert "custom" in list_adapters()


def _seed(storage: SQLiteStorage) -> None:
    storage.create_run(Run(run_id="r1", goal="g"))
    storage.append_event("r1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    storage.append_event(
        "r1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
    )
    storage.append_event(
        "r1", EventType.EVIDENCE_ADDED, {"evidence_id": "e1", "summary": "s", "source": "dataset"}
    )
    CheckpointManager(storage).checkpoint(
        "r1", environment=capture("r1", StaticProvider(dataset="v3"))
    )


def test_recover_dispatches_through_the_generic_adapter() -> None:
    storage = SQLiteStorage(":memory:")
    _seed(storage)
    decision = recover(
        "generic",
        "r1",
        storage,
        current_environment=capture("r1", StaticProvider(dataset="v3")),
    )
    assert decision.mode is RecoveryMode.RESUME
    assert decision.safe


def test_recover_unknown_adapter_errors() -> None:
    storage = SQLiteStorage(":memory:")
    with pytest.raises(ValueError):
        recover("nope", "r1", storage)
