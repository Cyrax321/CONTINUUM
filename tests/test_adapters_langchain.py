"""Unit tests for the LangChain adapter's SDK-free helpers (#1747).

Every test in ``tests/test_integration_langchain.py`` sits behind
``pytest.importorskip("langchain_core")``, so in an environment without
``langchain-core`` -- which is what a bare ``pip install -e .`` gives you --
none of them run and the pure helpers have no test at all. This file is the
SDK-free twin of the LangGraph one (``tests/test_adapters_langgraph.py``):
the helper tests run unconditionally, and the four that need an adapter
instance (the constructor calls ``_ensure_langchain``) are skipped when the
SDK is absent, so the file passes either way.
"""

from __future__ import annotations

import pytest

import continuum.adapters.langchain as langchain_module
from continuum.adapters.langchain import (
    _ensure_langchain,
    _extract_run_id,
    langchain_available,
)
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> SQLiteStorage:
    return SQLiteStorage(":memory:")


# --- importability, and the fail-closed gate every SDK entry shares ----------


def test_the_adapter_module_imports_without_langchain_installed() -> None:
    """``continuum.adapters.langchain`` must import on a minimal environment.

    The adapter is optional machinery; a project that only needs the storage
    layer must never hit an import error because it lacks a vendor SDK.
    """
    assert callable(_extract_run_id)


def test__ensure_langchain_raises_when_the_sdk_is_absent(monkeypatch) -> None:
    """The refusal must tell a contributor exactly how to get un-stuck.

    ``_ensure_langchain`` is the gate ``__init__`` and every SDK-only entry
    share, so the message naming the extra is the only actionable signal on
    a bare install.
    """
    monkeypatch.setattr(langchain_module, "langchain_available", False)
    with pytest.raises(ImportError, match=r"pip install continuum-agent\[langchain\]"):
        _ensure_langchain()


def test__ensure_langchain_passes_when_the_sdk_is_present(monkeypatch) -> None:
    monkeypatch.setattr(langchain_module, "langchain_available", True)
    _ensure_langchain()  # must not raise


# --- _extract_run_id --------------------------------------------------------


def test__extract_run_id_prefers_the_explicit_continuum_kwarg() -> None:
    assert _extract_run_id((), {"continuum_run_id": "r1"}) == "r1"


def test__extract_run_id_reads_the_plain_run_id_kwarg() -> None:
    assert _extract_run_id((), {"run_id": "r2"}) == "r2"


def test__extract_run_id_prefers_continuum_run_id_over_run_id() -> None:
    """Both keys present is a contradiction, and the explicit one wins."""
    assert _extract_run_id((), {"run_id": "plain", "continuum_run_id": "explicit"}) == "explicit"


def test__extract_run_id_reads_the_first_positional_dict() -> None:
    assert _extract_run_id(({"continuum_run_id": "r3"},), {}) == "r3"


def test__extract_run_id_ignores_a_positional_dict_without_the_key() -> None:
    """A dict with no ``continuum_run_id`` is a plain argument, not a claim."""
    assert _extract_run_id(({"order_id": "O-1"},), {}) is None


def test__extract_run_id_ignores_a_null_value_in_the_positional_dict() -> None:
    """``{"continuum_run_id": None}`` is absence, not the empty run.

    A falsy value would otherwise stringify to ``"None"`` and silently point a
    side effect at a run that does not exist.
    """
    assert _extract_run_id(({"continuum_run_id": None},), {}) is None


def test__extract_run_id_returns_none_with_no_arguments_at_all() -> None:
    assert _extract_run_id((), {}) is None


def test__extract_run_id_returns_none_for_plain_arguments() -> None:
    """A tool called with no run context is un-identified, so it runs unintecepted."""
    assert _extract_run_id(("arg",), {"order_id": "O-1"}) is None


def test__extract_run_id_stringifies_a_non_string_id() -> None:
    """The ledger keys on ``str``; an int id must survive the round trip."""
    assert _extract_run_id((), {"continuum_run_id": 42}) == "42"


# --- adapter behaviour these helpers feed -----------------------------------


@pytest.mark.skipif(not langchain_available, reason="langchain-core not installed")
class TestWrapToolAndStartRun:
    """The raise branches the integration suite reaches only on the happy path.

    ``LangChainAgentAdapter.__init__`` calls ``_ensure_langchain``, so nothing
    here can run without the SDK; they are gathered in one place so the rest
    of the file is independent of it.
    """

    def test_wrap_tool_rejects_key_and_key_fn_together(self, store: SQLiteStorage) -> None:
        """``key`` and ``key_fn`` are two spellings of one identity, never both."""
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        with pytest.raises(ValueError, match="not both"):

            @adapter.wrap_tool("notify.customer", key="fixed", key_fn=lambda *a, **k: "computed")
            def notify(order_id: str) -> str:
                return "sent"

    def test_wrap_tool_without_a_run_id_calls_the_tool_unchanged(
        self, store: SQLiteStorage
    ) -> None:
        """A call with no run context must reach the wrapped function untouched.

        ``run_id is None`` returns ``fn``, so a tool used outside a CONTINUUM
        run behaves exactly as it did before wrapping.
        """
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        calls: list[str] = []

        @adapter.wrap_tool("notify.customer")
        def notify(order_id: str) -> str:
            calls.append(order_id)
            return f"sent {order_id}"

        assert notify(order_id="O-1") == "sent O-1"
        assert calls == ["O-1"]

    def test_wrap_tool_accepts_a_positional_run_id_in_the_first_dict(
        self, store: SQLiteStorage
    ) -> None:
        """The state-dict form is the shape a pipeline node passes, not just kwargs."""
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        adapter.start_run(goal="notify", run_id="r_pos")
        calls: list[str] = []

        @adapter.wrap_tool("notify.customer")
        def notify(state: dict) -> str:
            calls.append(state["order_id"])
            return "sent"

        assert notify({"continuum_run_id": "r_pos", "order_id": "O-2"}) == "sent"
        assert calls == ["O-2"]

    def test_start_run_refuses_a_run_that_does_not_begin_with_run_started(
        self, store: SQLiteStorage
    ) -> None:
        """A run already carrying history cannot be backfilled.

        The first event is the anchor of the whole hash-chained history; prepending
        a ``RUN_STARTED`` would re-order events that were already witnessed. The
        adapter refuses rather than invent a different past.
        """
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        from continuum.events import EventType
        from continuum.models import Origin, Run

        store.create_run(Run(run_id="r_backfill", goal="original"))
        store.append_event(
            "r_backfill",
            EventType.TASK_UPDATED,
            {"completed": 1},
            source=Origin.DETERMINISTIC,
        )

        with pytest.raises(ValueError, match="does not begin with RUN_STARTED"):
            adapter.start_run(goal="late entry", run_id="r_backfill")
