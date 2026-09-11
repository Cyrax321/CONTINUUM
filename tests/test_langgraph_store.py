"""Native LangGraph checkpointer over CONTINUUM storage (issue #236).

Round-trips through LangGraph's own saver protocol and serde, then proves
the integration story: a StateGraph built against this checkpointer resumes
across process-like boundaries (separate compile instances), and every put
lands a provenance-tagged STATE_CHECKPOINTED event in the hash chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph.checkpoint.base")

from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages

from continuum.adapters.langgraph_store import (
    make_continuum_checkpointer,
)
from continuum.events import EventType
from continuum.models import Origin
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "lg.db")


def saver(db: str):
    return make_continuum_checkpointer(SQLiteStorage(db))


def mk_checkpoint(cid: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": 1,
        "id": cid,
        "ts": datetime.now().isoformat(),
        "channel_values": values,
        "channel_versions": dict.fromkeys(values, 1),
        "versions_seen": {},
        "updated_channels": list(values),
    }


def cfg(thread: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"configurable": {"thread_id": thread}}
    if checkpoint_id:
        c["configurable"]["checkpoint_id"] = checkpoint_id
    return c


def test_round_trip_preserves_values_and_types(db: str) -> None:
    s = saver(db)
    cp = mk_checkpoint(
        str(uuid.uuid4()),
        {
            "messages": ["hello"],
            "when": datetime(2026, 8, 23),
            "count": 7,
        },
    )
    out = s.put(cfg("t1"), cp, {"source": "loop"}, {})
    assert out["configurable"]["checkpoint_id"] == cp["id"]

    tup = s.get_tuple(cfg("t1"))
    assert tup is not None
    assert tup.checkpoint["id"] == cp["id"]
    assert tup.checkpoint["channel_values"]["count"] == 7
    assert tup.checkpoint["channel_values"]["messages"] == ["hello"]
    assert tup.metadata["source"] == "loop"


def test_list_orders_newest_first_and_honours_limit_before_filter(db: str) -> None:
    s = saver(db)
    ids = [str(uuid.uuid4()) for _ in range(4)]
    for i, cid in enumerate(ids):
        s.put(
            cfg("t1"), mk_checkpoint(cid, {"step": i}), {"source": "loop", "tag": f"t{i % 2}"}, {}
        )
    got = [t.checkpoint["id"] for t in s.list(cfg("t1"))]
    assert got == ids[::-1]

    limited = [t.checkpoint["id"] for t in s.list(cfg("t1"), limit=2)]
    assert limited == ids[::-1][:2]

    before = [t.checkpoint["id"] for t in s.list(cfg("t1"), before=cfg("t1", ids[2]))]
    assert before == [ids[1], ids[0]]

    filtered = [t.checkpoint["id"] for t in s.list(cfg("t1"), filter={"tag": "t0"})]
    assert filtered == [ids[2], ids[0]]


def test_parent_chain_and_point_in_time_get(db: str) -> None:
    s = saver(db)
    first = s.put(cfg("t9"), mk_checkpoint(str(uuid.uuid4()), {"n": 1}), {}, {})
    second = s.put(
        {
            "configurable": {
                "thread_id": "t9",
                "checkpoint_id": first["configurable"]["checkpoint_id"],
            }
        },
        mk_checkpoint(str(uuid.uuid4()), {"n": 2}),
        {},
        {},
    )
    tup = s.get_tuple(cfg("t9"))
    assert tup is not None and tup.config == second
    assert tup.parent_config is not None
    assert (
        tup.parent_config["configurable"]["checkpoint_id"]
        == (first["configurable"]["checkpoint_id"])
    )
    old = s.get_tuple(cfg("t9", first["configurable"]["checkpoint_id"]))
    assert old is not None and old.checkpoint["channel_values"]["n"] == 1


def test_put_writes_surface_as_pending(db: str) -> None:
    s = saver(db)
    cp = s.put(cfg("t2"), mk_checkpoint(str(uuid.uuid4()), {}), {}, {})
    s.put_writes(cp, [("node_a", "partial value")], task_id="task_1")
    tup = s.get_tuple(cfg("t2"))
    assert tup is not None and tup.pending_writes is not None
    assert ("task_1", "node_a", "partial value") in tup.pending_writes


def test_delete_thread_is_total(db: str) -> None:
    s = saver(db)
    s.put(cfg("t3"), mk_checkpoint(str(uuid.uuid4()), {}), {}, {})
    s.delete_thread("t3")
    assert s.get_tuple(cfg("t3")) is None
    assert list(s.list(cfg("t3"))) == []


def test_each_put_lands_provenance_tagged_state_checkpointed_events(db: str) -> None:
    s = saver(db)
    for i in range(3):
        s.put(cfg("team-thread"), mk_checkpoint(str(uuid.uuid4()), {"i": i}), {}, {})
    with SQLiteStorage(db) as store:
        events = [
            e for e in store.read_events("lg-team-thread") if e.type is EventType.STATE_CHECKPOINTED
        ]
        started = [
            e for e in store.read_events("lg-team-thread") if e.type is EventType.RUN_STARTED
        ]
    assert len(events) == 3
    assert all(e.source is Origin.EXTERNAL_AGENT for e in events)
    assert started and started[0].source is Origin.EXTERNAL_AGENT


# --- integration: a real StateGraph resumes across instances ------------------ #


class State(TypedDict):
    messages: Annotated[list[str], add_messages]


def _build(db: str):
    def node(state: State) -> dict[str, Any]:
        return {"messages": ["node ran"]}

    g = StateGraph(State)
    g.add_node("step", node)
    g.set_entry_point("step")
    return g.compile(checkpointer=saver(db))


def test_state_graph_resumes_across_instances(db: str) -> None:
    thread = {"configurable": {"thread_id": "smoke"}}
    app1 = _build(db)
    app1.invoke({"messages": ["first"]}, thread)

    # Simulate a crash + fresh process: brand-new graph instance over the
    # same storage must see prior state.
    app2 = _build(db)
    result = app2.invoke({"messages": ["second"]}, thread)
    msgs = result["messages"]
    assert any(getattr(m, "content", m) == "first" or m == "first" for m in msgs)
