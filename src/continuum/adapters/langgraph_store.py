"""Native LangGraph checkpointer over CONTINUUM storage (issue #236).

LangGraph is the largest production agent framework and ships its own
persistence interface: implement BaseCheckpointSaver and any LangGraph app
gets CONTINUUM's durability without changing its persistence story. This is
the adoption unlock the universality roadmap (#213) calls seam 1's missing
piece: thread_id maps deterministically to a run_id, every put appends a
provenance-tagged STATE_CHECKPOINTED event to the hash chain, and the run
inherits everything else CONTINUUM does (gate, reconcilers, contracts,
briefing, human_steps).

Serialization uses LangGraph's own JsonPlusSerializer, so channel values can
be anything their ecosystem serialises (pydantic models, datetimes, dataclasses).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["langgraph_checkpoint_available", "make_continuum_checkpointer"]


def langgraph_checkpoint_available() -> bool:
    import importlib.util

    return (
        importlib.util.find_spec("langgraph") is not None
        and importlib.util.find_spec("langgraph.checkpoint.base") is not None
    )


def make_continuum_checkpointer(storage: Any) -> Any:
    """Build a BaseCheckpointSaver backed by ``storage``.

    Raises ImportError with an install hint when LangGraph is absent. The
    returned saver is sync-only; async entry points delegate through
    ``asyncio.to_thread``-style defaults provided by callers when needed.
    """
    if not langgraph_checkpoint_available():
        raise ImportError(
            "langgraph is required for the CONTINUUM checkpointer. "
            "Install it (the core library stays dependency-free)."
        )

    from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()

    class ContinuumCheckpointStore(BaseCheckpointSaver[Any]):
        """Provenance-tagged LangGraph persistence over CONTINUUM storage."""

        def __init__(self, store: Any) -> None:
            super().__init__(serde=serde)
            self._store = store

        # -- helpers ----------------------------------------------------- #

        @staticmethod
        def _thread(config: Any) -> str:
            thread = ((config or {}).get("configurable") or {}).get("thread_id")
            if not thread:
                raise ValueError("CONTINUUM checkpointer requires configurable.thread_id")
            return str(thread)

        def _ensure_run(self, conn: Any, thread_id: str) -> None:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", ("lg-" + thread_id,)
            ).fetchone()
            if row is None:
                from continuum.models import Origin

                conn.execute(
                    "INSERT INTO runs(run_id, goal, status, created_at, updated_at)"
                    " VALUES (?, ?, 'started', datetime('now'), datetime('now'))",
                    ("lg-" + thread_id, f"LangGraph thread {thread_id}"),
                )
                head = conn.execute(
                    "SELECT sequence, hash FROM events WHERE run_id = ?"
                    " ORDER BY sequence DESC LIMIT 1",
                    ("lg-" + thread_id,),
                ).fetchone()
                from continuum.events import Event, EventType
                from continuum.security.hashing import make_id

                event = Event(
                    event_id=make_id("event"),
                    run_id="lg-" + thread_id,
                    sequence=(head["sequence"] if head else 0) + 1,
                    type=EventType.RUN_STARTED,
                    payload={"goal": f"LangGraph thread {thread_id}", "total": 0},
                    source=Origin.EXTERNAL_AGENT,
                    prev_hash=head["hash"] if head else None,
                ).sealed()
                conn.execute(
                    "INSERT INTO events(run_id, sequence, event_id, type, timestamp,"
                    " payload, causer_event_id, source, prev_hash, hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                    (
                        event.run_id,
                        event.sequence,
                        event.event_id,
                        event.type.value,
                        event.timestamp.isoformat(),
                        json.dumps(dict(event.payload), sort_keys=True),
                        event.source.value,
                        event.prev_hash,
                        event.hash,
                    ),
                )

        def _log_event(self, conn: Any, thread_id: str, checkpoint_id: str) -> None:
            self._ensure_run(conn, thread_id)
            rid = "lg-" + thread_id
            head = conn.execute(
                "SELECT sequence, hash FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (rid,),
            ).fetchone()
            from continuum.events import Event, EventType
            from continuum.models import Origin
            from continuum.security.hashing import make_id

            event = Event(
                event_id=make_id("event"),
                run_id=rid,
                sequence=(head["sequence"] if head else 0) + 1,
                type=EventType.STATE_CHECKPOINTED,
                payload={"checkpoint_id": checkpoint_id, "via": "langgraph"},
                source=Origin.EXTERNAL_AGENT,
                prev_hash=head["hash"] if head else None,
            ).sealed()
            conn.execute(
                "INSERT INTO events(run_id, sequence, event_id, type, timestamp,"
                " payload, causer_event_id, source, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    event.run_id,
                    event.sequence,
                    event.event_id,
                    event.type.value,
                    event.timestamp.isoformat(),
                    json.dumps(dict(event.payload), sort_keys=True),
                    event.source.value,
                    event.prev_hash,
                    event.hash,
                ),
            )

        # -- saver protocol ----------------------------------------------- #

        def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
            thread_id = self._thread(config)
            btype, blob = self.serde.dumps_typed(checkpoint)
            meta_type, meta_blob = self.serde.dumps_typed(
                metadata if isinstance(metadata, dict) else {}
            )
            with self._store._write() as conn:
                self._ensure_run(conn, thread_id)
                conn.execute(
                    "INSERT OR REPLACE INTO lg_checkpoints(thread_id, checkpoint_id,"
                    " parent_id, type, checkpoint, meta_type, metadata)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        checkpoint["id"],
                        ((config.get("configurable") or {}).get("checkpoint_id")),
                        btype,
                        blob,
                        meta_type,
                        meta_blob,
                    ),
                )
                self._log_event(conn, thread_id, checkpoint["id"])
            return {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": checkpoint["id"],
                }
            }

        def put_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
            thread_id = self._thread(config)
            checkpoint_id = (config.get("configurable") or {}).get("checkpoint_id")
            with self._store._write() as conn:
                for idx, (channel, value) in enumerate(writes):
                    wtype, wblob = self.serde.dumps_typed(value)
                    conn.execute(
                        "INSERT OR REPLACE INTO lg_writes(thread_id, checkpoint_id,"
                        " task_id, idx, channel, type, blob) VALUES (?,?,?,?,?,?,?)",
                        (thread_id, checkpoint_id, task_id, idx, channel, wtype, wblob),
                    )

        def get_tuple(self, config: Any) -> CheckpointTuple | None:
            thread_id = self._thread(config)
            requested = (config.get("configurable") or {}).get("checkpoint_id")
            with self._store._read() as conn:
                if requested:
                    row = conn.execute(
                        "SELECT * FROM lg_checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
                        (thread_id, requested),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM lg_checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                        (thread_id,),
                    ).fetchone()
            if row is None:
                return None
            checkpoint = self.serde.loads_typed((row["type"], row["checkpoint"]))
            metadata = self.serde.loads_typed((row["meta_type"], row["metadata"]))
            writes: list[tuple[str, str, Any]] = []
            with self._store._read() as conn:
                for w in conn.execute(
                    "SELECT channel, type, blob, task_id FROM lg_writes"
                    " WHERE thread_id = ? AND checkpoint_id = ? ORDER BY idx",
                    (thread_id, row["checkpoint_id"]),
                ):
                    writes.append(
                        (w["task_id"], w["channel"], self.serde.loads_typed((w["type"], w["blob"])))
                    )
            cfg = {"configurable": {"thread_id": thread_id, "checkpoint_id": row["checkpoint_id"]}}
            parent = (
                {"configurable": {"thread_id": thread_id, "checkpoint_id": row["parent_id"]}}
                if row["parent_id"]
                else None
            )
            factory: Any = CheckpointTuple
            out: CheckpointTuple | None = factory(
                cfg, checkpoint, metadata or {}, parent, list(writes) or None
            )
            return out

        def list(
            self, config: Any, *, filter: Any = None, before: Any = None, limit: int | None = None
        ) -> Any:
            thread_id = self._thread(config) if config else None
            if thread_id is None:
                return iter(())
            query = "SELECT * FROM lg_checkpoints WHERE thread_id = ?"
            params: list[Any] = [thread_id]
            if before is not None:
                before_id = (before.get("configurable") or {}).get("checkpoint_id")
                if before_id:
                    query += " AND id < (SELECT COALESCE(MAX(id), 0) FROM lg_checkpoints"
                    query += " WHERE thread_id = ? AND checkpoint_id = ?)"
                    params += [thread_id, before_id]
            query += " ORDER BY id DESC"
            if limit is not None:
                query += f" LIMIT {int(limit)}"
            with self._store._read() as conn:
                rows = conn.execute(query, params).fetchall()
            out: list[CheckpointTuple] = []
            for row in rows:
                metadata = self.serde.loads_typed((row["meta_type"], row["metadata"]))
                if filter and any(metadata.get(k) != v for k, v in filter.items()):
                    continue
                checkpoint = self.serde.loads_typed((row["type"], row["checkpoint"]))
                cfg = {
                    "configurable": {"thread_id": thread_id, "checkpoint_id": row["checkpoint_id"]}
                }
                parent = (
                    {"configurable": {"thread_id": thread_id, "checkpoint_id": row["parent_id"]}}
                    if row["parent_id"]
                    else None
                )
                factory: Any = CheckpointTuple
                out.append(factory(cfg, checkpoint, metadata or {}, parent))
            yield from out

        def delete_thread(self, thread_id: str) -> None:
            with self._store._write() as conn:
                conn.execute("DELETE FROM lg_writes WHERE thread_id = ?", (thread_id,))
                conn.execute("DELETE FROM lg_checkpoints WHERE thread_id = ?", (thread_id,))

        async def adelete_thread(self, thread_id: str) -> None:
            self.delete_thread(thread_id)

        async def aget_tuple(self, config: Any) -> CheckpointTuple | None:
            return self.get_tuple(config)

        async def alist(self, config: Any, **kwargs: Any) -> Any:
            for item in self.list(config, **kwargs):
                yield item

        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            return self.put(*args, **kwargs)

        async def aput_writes(self, *args: Any, **kwargs: Any) -> None:
            self.put_writes(*args, **kwargs)

    return ContinuumCheckpointStore(storage)
