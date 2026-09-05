"""Benchmark harness for memory governance (issue #304, #304d).

Measures the cost of the control plane, not the memory system itself:
- key derivation (normalize + render) for tenant-scoped identities
- forensic join across runs (scan + hash)
- gateway tenant deny (render + tenant extract)

These are cheap, deterministic operations that must stay cheap. The
harness records elapsed time and byte counts so the \"what does durability
cost?\" question has real numbers, not estimates.
"""

from __future__ import annotations

import time
from pathlib import Path

from continuum.actions.ledger import ActionLedger, forensic_join_across_runs
from continuum.events import EventType
from continuum.gate import derive_memory_key
from continuum.gateway import Route, match_route
from continuum.models import Run
from continuum.security.hashing import stable_hash
from continuum.storage import SQLiteStorage


def bench_key_derivation(iterations: int = 10000) -> dict[str, float]:
    start = time.perf_counter()
    for i in range(iterations):
        derive_memory_key(
            {"store_id": "pgvector_main", "tenant": "acme", "record_key": f"rec-{i % 100}"}
        )
    elapsed = time.perf_counter() - start
    return {
        "iterations": iterations,
        "elapsed_ms": elapsed * 1000,
        "per_op_us": elapsed * 1e6 / iterations,
    }


def bench_forensic_join(
    tmp_path: Path, runs: int = 10, records_per_run: int = 20
) -> dict[str, float]:
    path = str(tmp_path / "bench_mem.db")
    with SQLiteStorage(path) as store:
        for r in range(runs):
            rid = f"run_{r}"
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
            ledger = ActionLedger(store, rid)
            for rec in range(records_per_run):
                digest = stable_hash({"r": r, "rec": rec})
                rendered = f"mem:pgvector_main:acme:rec-{r}-{rec}"
                # Unique per run to avoid cross-run STARTED collision
                outcome = ledger.claim(
                    "mem_write", {}, key=rendered, scoped_to_run=False, origin_digest=digest
                )
                # Complete so later forensic sees completed, not STARTED
                ledger.complete(outcome.key)
        start = time.perf_counter()
        hits = forensic_join_across_runs(store, "rec-")
        elapsed = time.perf_counter() - start
        return {
            "runs": runs,
            "records_per_run": records_per_run,
            "hits": len(hits),
            "elapsed_ms": elapsed * 1000,
        }


def bench_gateway_tenant_deny(iterations: int = 5000) -> dict[str, float]:
    route = Route(
        host="pgvector.internal",
        methods=("POST",),
        prefix="/upsert",
        action_type="mem_write",
        key_template="mem:{store_id}:{tenant}:{record_key}",
    )
    # Pre-create a claimed action for acme
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "gw_bench.db")
        with SQLiteStorage(path) as store:
            store.create_run(Run(run_id="run_1", goal="g"))
            store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
            ledger = ActionLedger(store, "run_1")
            ledger.claim("mem_write", {}, key="mem:pgvector_main:acme:rec-42", scoped_to_run=False)
            from continuum.actions.ledger import fold_action_events

            actions = fold_action_events(store.read_events("run_1"))
            start = time.perf_counter()
            for _ in range(iterations):
                # Mix of allowed and denied
                match_route(
                    [route],
                    host="pgvector.internal",
                    method="POST",
                    body={"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"},
                    actions_by_key=actions,
                    run_id="run_1",
                    bound_tenant="acme",
                    storage=store,
                )
                match_route(
                    [route],
                    host="pgvector.internal",
                    method="POST",
                    body={"store_id": "pgvector_main", "tenant": "globex", "record_key": "rec-42"},
                    actions_by_key=actions,
                    run_id="run_1",
                    bound_tenant="acme",
                    storage=store,
                )
            elapsed = time.perf_counter() - start
            return {
                "iterations": iterations * 2,
                "elapsed_ms": elapsed * 1000,
                "per_op_us": elapsed * 1e6 / (iterations * 2),
            }


if __name__ == "__main__":
    import tempfile

    print("memory governance bench")
    print(bench_key_derivation(10000))
    with tempfile.TemporaryDirectory() as td:
        print(bench_forensic_join(Path(td), runs=10, records_per_run=20))
    print(bench_gateway_tenant_deny(2000))
