"""SDK-free tests for the LangChain adapter's module-level helpers.

Integration coverage lives in ``tests/test_integration_langchain.py``, which
``importorskip``s ``langchain-core`` and so never runs where the extra is not
installed. The pure helpers, the SDK guard, and the decorator's argument
contract are testable without the SDK at all, mirroring the LangGraph twin in
``tests/test_adapters_langgraph.py`` so a missing dependency stops meaning a
silent hole in coverage.
"""

from __future__ import annotations

from typing import Any

import pytest

from continuum.adapters import GenericAgentAdapter
from continuum.adapters.langchain import langchain_available
from continuum.events import EventType
from continuum.models import Goal, Origin, Run, SemanticState
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> SQLiteStorage:
    return SQLiteStorage(":memory:")


# --- _extract_run_id: every branch ----------------------------------------- #


def test_extracts_run_id_from_the_continuum_run_id_kwarg() -> None:
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id((), {"continuum_run_id": "run_1"}) == "run_1"


def test_extracts_run_id_from_the_run_id_kwarg() -> None:
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id((), {"run_id": "run_2"}) == "run_2"


def test_continuum_run_id_wins_when_both_kwargs_are_present() -> None:
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id((), {"continuum_run_id": "winner", "run_id": "loser"}) == "winner"


def test_extracts_run_id_from_a_positional_state_dict() -> None:
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id(({"continuum_run_id": "run_3"},), {}) == "run_3"


def test_returns_none_for_a_positional_dict_carrying_no_run_id() -> None:
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id(({"goal": "g"},), {}) is None


def test_returns_none_when_the_positional_run_id_is_empty() -> None:
    """An empty id is no id: falling back to it would ledger onto a run named ""."""
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id(({"continuum_run_id": ""},), {}) is None


def test_returns_none_for_positional_args_that_are_not_a_state_dict() -> None:
    from continuum.adapters.langchain import _extract_run_id

    assert _extract_run_id(("payload", "second"), {"topic": "t"}) is None


# --- the SDK guard ---------------------------------------------------------- #


def test_the_availability_flag_is_a_bool() -> None:
    assert isinstance(langchain_available, bool)


def test_ensure_langchain_names_the_install_command(monkeypatch) -> None:
    """The guard's message is the whole point of the guard: a missing extra must
    tell the operator how to get it rather than crash without a hint."""
    import continuum.adapters.langchain as lc

    monkeypatch.setattr(lc, "langchain_available", False)

    with pytest.raises(ImportError, match="pip install continuum-agent"):
        lc._ensure_langchain()


# --- the adapter, with availability forced so no install is needed ---------- #


@pytest.fixture
def adapter(store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch) -> GenericAgentAdapter:
    """Construct the adapter without depending on langchain-core at all.

    Nothing past the module-level import guard ever touches the SDK, so setting
    the flag exercises the adapter logic in an environment where the extra is
    absent exactly as well as in one where it is installed.
    """
    import continuum.adapters.langchain as lc

    monkeypatch.setattr(lc, "langchain_available", True)
    return lc.LangChainAgentAdapter(store)


def test_the_adapter_is_a_generic_adapter(adapter: GenericAgentAdapter) -> None:
    assert isinstance(adapter, GenericAgentAdapter)


def test_wrap_tool_refuses_key_and_key_fn_together(adapter: GenericAgentAdapter) -> None:
    """Two ways of naming the same side effect cannot both be given.

    The pair resolves to no single idempotency key, and guessing one would let
    the same call be ledgered twice under two identities.
    """
    decorate = adapter.wrap_tool(
        "github.create_issue", key="issue:7", key_fn=lambda *args: "issue:8"
    )

    with pytest.raises(ValueError, match="not both"):
        decorate(lambda title: title)


def test_start_run_backfills_the_run_started_event(adapter: GenericAgentAdapter) -> None:
    """A run created through the adapter begins its own log.

    Projection, replay and restore all require RUN_STARTED to be the first
    entry, and GenericAgentAdapter.start_run only creates the row; the backfill
    is what makes the adapter's own checkpoint_node work against that row.
    """
    from continuum.events import EventType

    run = adapter.start_run("migrate billing", run_id="run_backfill")

    assert [e.type for e in adapter.storage.read_events("run_backfill")] == [EventType.RUN_STARTED]
    assert adapter.storage.read_events("run_backfill")[0].payload["goal"] == "migrate billing"
    assert run.run_id == "run_backfill"


def test_start_run_refuses_a_log_that_does_not_begin_with_run_started(
    adapter: GenericAgentAdapter,
) -> None:
    """A run already logging work has an order the adapter must not reorder.

    Backfilling RUN_STARTED after other events would place the run's beginning
    behind them, so an existing non-empty log is an error, not a repair.
    """
    from continuum.events import EventType

    adapter.storage.create_run(Run(run_id="run_midstream", goal="g"))
    adapter.storage.append_event(
        "run_midstream", EventType.TASK_UPDATED, {"completed": 1, "total": 5}, source=Origin.HUMAN
    )

    with pytest.raises(ValueError, match="does not begin with RUN_STARTED"):
        adapter.start_run("g", run_id="run_midstream")


def test_checkpoint_node_passes_state_through_without_a_run_id(
    adapter: GenericAgentAdapter,
) -> None:
    """A state with no run id is a pipeline this adapter did not start.

    Pipelines share state shapes; the node has to be safe to drop into any of
    them, which means no run id is a no-op, not an error mid-chain.
    """
    state = {"goal": "process orders", "completed_count": 3}

    assert adapter.checkpoint_node(state) is state


def test_checkpoint_node_projects_a_run_that_has_events(
    adapter: GenericAgentAdapter,
) -> None:
    """A run with work on the log is checkpointed from the log, not from the dict.

    The dict is the caller's word on progress; the log is the authority.
    Building the state from the dict fields left source_sequence=0, which made
    restore disagree with the log (#46), so the node only falls back to the
    dict when the log has nothing to read.
    """
    adapter.start_run("migrate billing", run_id="run_cp")
    adapter.storage.append_event(
        "run_cp",
        EventType.TASK_UPDATED,
        {"goal": "migrate billing", "completed": 1, "total": 1},
        source=Origin.HUMAN,
    )

    state = {"continuum_run_id": "run_cp", "completed_count": 99}

    assert adapter.checkpoint_node(state) is state
    assert any(
        e.type is EventType.STATE_CHECKPOINTED for e in adapter.storage.read_events("run_cp")
    )


def test_wrap_tool_runs_the_side_effect_once_per_idempotency_key(
    adapter: GenericAgentAdapter,
) -> None:
    """A wrapped tool answers from the ledger on the second identical call.

    That is the adapter's whole reason to exist: a pipeline re-invoked after an
    interruption reaches the same step and the same arguments, and the side
    effect must not fire a second time.
    """
    adapter.start_run("send invoices", run_id="run_wrap")
    calls: list[str] = []

    @adapter.wrap_tool("invoice.send", key="invoice:1", scoped_to_run=False)
    def send(title: str, continuum_run_id: str = "") -> str:
        calls.append(title)
        return "sent"

    assert send("inv-1", continuum_run_id="run_wrap") == "sent"
    assert send("inv-1", continuum_run_id="run_wrap") == "sent"

    assert calls == ["inv-1"], "the second call must answer from the ledger, not the tool"
    assert send.__name__ == "send"


def test_extract_semantic_state_maps_the_default_state_shape(
    adapter: GenericAgentAdapter,
) -> None:
    state = {
        "continuum_run_id": "run_1",
        "goal": "Process records",
        "completed_count": 42,
        "total_count": 100,
    }

    semantic = adapter.extract_semantic_state(state)

    assert semantic.run_id == "run_1"
    assert semantic.goal.description == "Process records"
    assert semantic.progress.completed == 42
    assert semantic.progress.total == 100


def test_extract_semantic_state_fills_defaults_for_missing_fields(
    adapter: GenericAgentAdapter,
) -> None:
    """A state dict with only a run id is the minimum a pipeline must send."""
    semantic = adapter.extract_semantic_state({"continuum_run_id": "run_2"})

    assert semantic.run_id == "run_2"
    assert semantic.goal.description == "LangChain agent task"
    assert semantic.progress.completed == 0
    assert semantic.progress.total is None


def test_extract_semantic_state_honours_a_caller_supplied_extractor(
    store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An integration with its own state shape sees its own extractor."""
    import continuum.adapters.langchain as lc

    monkeypatch.setattr(lc, "langchain_available", True)

    def custom(state: dict[str, Any]) -> SemanticState:
        return SemanticState(run_id=state["rid"], goal=Goal(description="custom"))

    adapter = lc.LangChainAgentAdapter(store, state_to_semantic=custom)

    assert adapter.extract_semantic_state({"rid": "run_3"}).goal.description == "custom"
