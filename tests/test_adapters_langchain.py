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


def test__extract_run_id_treats_a_null_kwarg_as_absent() -> None:
    """A ``None`` id is absence, not a run named ``"None"``.

    Stringifying the null value would point a side effect at a run that does
    not exist, so the wrapped call would fail instead of running where it
    belongs. Both keyword spellings must obey the rule.
    """
    assert _extract_run_id((), {"continuum_run_id": None}) is None
    assert _extract_run_id((), {"run_id": None}) is None


def test__extract_run_id_falls_through_a_null_continuum_id_to_run_id() -> None:
    """An explicitly null ``continuum_run_id`` does not shadow a real one."""
    assert _extract_run_id((), {"continuum_run_id": None, "run_id": "r2"}) == "r2"


def test__extract_run_id_treats_an_empty_kwarg_as_absent() -> None:
    """An empty string id would ledger onto a run named ``""``."""
    assert _extract_run_id((), {"continuum_run_id": ""}) is None
    assert _extract_run_id((), {"run_id": ""}) is None


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
    """A tool called with no run context is un-identified, so it runs un-intercepted."""
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

        # The return value and the call list pass whether or not the run id
        # was honoured, so the ledger is the evidence that it was: the action
        # is recorded under this run, and a repeat is served from the ledger
        # instead of executing the tool a second time.
        from continuum.actions.ledger import ActionLedger
        from continuum.models import ActionStatus

        ledger = ActionLedger(store, run_id="r_pos")
        recorded = ledger.all()
        assert [a.action_type for a in recorded] == ["notify.customer"]
        assert recorded[0].run_id == "r_pos"
        assert recorded[0].status is ActionStatus.COMPLETED

        assert notify({"continuum_run_id": "r_pos", "order_id": "O-2"}) == "sent"
        assert calls == ["O-2"], "a repeat must be served from the ledger, not re-executed"

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


@pytest.mark.skipif(not langchain_available, reason="langchain-core not installed")
class TestCheckpointNodeAndStateExtraction:
    """The checkpoint node's arms and the two state converters (issue #1986).

    ``checkpoint_node`` carries three distinct arms: an unidentified state
    returns untouched, a run with no event log gets its state extracted from
    the dict fields, and a run whose log already carries work gets the
    authoritative projection. This class also drives the converter hook
    (``state_to_semantic``), the default extractor, and
    ``assess_langchain_recovery`` direct to the recovery engine.
    """

    def test_checkpoint_node_returns_an_unidentified_state_unchanged(
        self, store: SQLiteStorage
    ) -> None:
        """No ``continuum_run_id`` means nothing is claimed, written or thrown."""
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        state = {"other_field": 123}
        assert adapter.checkpoint_node(state) is state
        assert list(store.list_runs()) == []

    def test_checkpoint_node_on_a_fresh_run_extracts_from_the_state_dict(
        self, store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run without events has no projection; the dict is the only truth."""
        from continuum.adapters.langchain import LangChainAgentAdapter
        from continuum.models import Run, SemanticState

        store.create_run(Run(run_id="r_fresh", goal="original"))
        adapter = LangChainAgentAdapter(store)
        seen: list[tuple[str, SemanticState, str]] = []
        monkeypatch.setattr(
            adapter,
            "capture_state",
            lambda run_id, semantic_state, *, reason: seen.append((run_id, semantic_state, reason)),
            raising=False,
        )
        state = {
            "continuum_run_id": "r_fresh",
            "goal": "carry the patients",
            "completed_count": 2,
            "total_count": 5,
        }
        returned = adapter.checkpoint_node(state)
        assert returned is state
        assert [run_id for run_id, _, _ in seen] == ["r_fresh"]
        assert [reason for _, _, reason in seen] == ["langchain checkpoint node"]
        assert seen[0][1].goal.description == "carry the patients"
        assert seen[0][1].progress.completed == 2
        assert seen[0][1].progress.total == 5

    def test_checkpoint_node_on_a_run_with_events_projects_the_log(
        self, store: SQLiteStorage
    ) -> None:
        """Once the log carries work, projection is the source of truth."""
        from continuum.adapters.langchain import LangChainAgentAdapter
        from continuum.events import EventType

        adapter = LangChainAgentAdapter(store)
        run = adapter.start_run(goal="project me")

        state = {"continuum_run_id": run.run_id, "goal": "stale dict goal"}
        returned = adapter.checkpoint_node(state)
        assert returned is state
        checkpointed = list(store.read_events(run.run_id))
        assert checkpointed[-1].type is EventType.STATE_CHECKPOINTED
        assert checkpointed[-1].payload["reason"] == "langchain checkpoint node"

    def test_extract_semantic_state_prefers_the_custom_converter(
        self, store: SQLiteStorage
    ) -> None:
        """The injected ``state_to_semantic`` hook replaces the default."""
        from continuum.adapters.langchain import LangChainAgentAdapter
        from continuum.models import Goal, SemanticState

        marker = SemanticState(run_id="r_custom", goal=Goal(description="custom"))
        adapter = LangChainAgentAdapter(store, state_to_semantic=lambda state: marker)
        assert adapter.extract_semantic_state({"goal": "ignored"}) is marker

    def test__extract_semantic_state_defaults_each_field(self, store: SQLiteStorage) -> None:
        """Missing fields fall back per field, not as a whole."""
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        semantic = adapter._extract_semantic_state({})
        assert semantic.run_id == "unknown"
        assert semantic.goal.description == "LangChain agent task"
        assert semantic.progress.completed == 0
        assert semantic.progress.total is None

    def test__extract_semantic_state_parses_counts_from_the_dict(
        self, store: SQLiteStorage
    ) -> None:
        from continuum.adapters.langchain import LangChainAgentAdapter

        adapter = LangChainAgentAdapter(store)
        semantic = adapter._extract_semantic_state(
            {"goal": "ship it", "completed_count": "2", "total_count": 5}
        )
        assert semantic.run_id == "unknown"
        assert semantic.goal.description == "ship it"
        assert semantic.progress.completed == 2
        assert semantic.progress.total == 5

    def test_start_run_backfills_run_started_on_an_empty_log(self, store: SQLiteStorage) -> None:
        """The empty-log branch must seed the event projection needs."""
        from continuum.adapters.langchain import LangChainAgentAdapter
        from continuum.events import EventType

        adapter = LangChainAgentAdapter(store)
        run = adapter.start_run(goal="fresh start")
        events = store.read_events(run.run_id)
        assert [e.type for e in events] == [EventType.RUN_STARTED]
        assert events[0].payload["goal"] == "fresh start"

    def test_start_run_accepts_a_run_whose_first_event_sits_in_the_archive(
        self, store: SQLiteStorage
    ) -> None:
        """After compaction the first event lives in the archive only.

        The log begins with ``RUN_STARTED``, which under one of its later
        versions gets compacted into ``events_archive``. ``start_run`` must
        still honour it (no backfill, no refusal) rather than fail the run
        whose history it can no longer see in the live stream.
        """
        from continuum.adapters.langchain import LangChainAgentAdapter
        from continuum.events import EventType
        from continuum.models import Origin

        adapter = LangChainAgentAdapter(store)
        run = adapter.start_run(goal="compacted history")
        store.append_event(
            run.run_id, EventType.TASK_UPDATED, {"completed": 1}, source=Origin.DETERMINISTIC
        )
        store.compact_run(
            run.run_id, through_sequence=store.last_sequence(run.run_id)
        )  # compacts RUN_STARTED and the task update behind a fresh anchor
        assert not [e for e in store.read_events(run.run_id) if e.type is EventType.RUN_STARTED]

        reopened = adapter.start_run(goal="reopen", run_id=run.run_id)
        assert reopened.run_id == run.run_id
        assert not [e for e in store.read_events(run.run_id) if e.type is EventType.RUN_STARTED]

    def test_assess_langchain_recovery_returns_a_decision(self, store: SQLiteStorage) -> None:
        """The helper delegates straight to the recovery engine."""
        from continuum.adapters.langchain import LangChainAgentAdapter
        from continuum.recovery.engine import RecoveryDecision

        adapter = LangChainAgentAdapter(store)
        run = adapter.start_run(goal="assess me")
        decision = adapter.assess_langchain_recovery(run.run_id)
        assert isinstance(decision, RecoveryDecision)
        assert decision.safe is True
