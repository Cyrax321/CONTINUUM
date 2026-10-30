from __future__ import annotations

import pytest

from continuum.adapters import (
    AdapterRegistry,
    GenericAgentAdapter,
    LangChainAgentAdapter,
    LangGraphAgentAdapter,
    OpenAIAgentAdapter,
    get_adapter,
    list_adapters,
    recover,
    register_adapter,
)
from continuum.adapters import registry as registry_module
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import RecoveryMode, Run
from continuum.storage import SQLiteStorage


def test_builtin_adapters_are_registered() -> None:
    names = list_adapters()
    for name in ("generic", "langchain", "langgraph", "openai"):
        assert name in names


def test_get_adapter_returns_the_class() -> None:
    assert get_adapter("generic") is GenericAgentAdapter


def test_get_adapter_resolves_the_lazy_framework_factories() -> None:
    # "generic" is the only eager factory; the other three are the lazy
    # imports the registry exists to defer, and nothing else in the suite
    # resolves them.
    assert get_adapter("langchain") is LangChainAgentAdapter
    assert get_adapter("langgraph") is LangGraphAgentAdapter
    assert get_adapter("openai") is OpenAIAgentAdapter


def test_unknown_adapter_raises() -> None:
    with pytest.raises(ValueError):
        get_adapter("does-not-exist")


def test_registry_register_and_get_is_local() -> None:
    reg = AdapterRegistry()
    reg.register("demo", lambda: GenericAgentAdapter)
    assert reg.get("demo") is GenericAgentAdapter
    assert "demo" in reg.names()


def test_registry_all_keys_every_registered_class_by_name() -> None:
    reg = AdapterRegistry()
    assert reg.all() == {}

    class Alpha:
        pass

    class Beta:
        pass

    # Registration order must not leak into the returned mapping.
    reg.register("beta", lambda: Beta)
    reg.register("alpha", lambda: Alpha)
    assert reg.all() == {"alpha": Alpha, "beta": Beta}


def test_register_adapter_writes_the_module_level_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standalone = AdapterRegistry()
    monkeypatch.setattr(registry_module, "_REGISTRY", standalone)
    register_adapter("demo", lambda: GenericAgentAdapter)
    assert standalone.names() == ["demo"]
    assert get_adapter("demo") is GenericAgentAdapter


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
