import os, sys
from continuum import (ActionLedger, CheckpointManager, EventType, Run, SQLiteStorage,
                       SemanticPolicy, StaticProvider, capture_environment)

db, effects, dataset = sys.argv[1], sys.argv[2], sys.argv[3]
store = SQLiteStorage(db)
manager = CheckpointManager(store, policy=SemanticPolicy(progress_stride=200))
env = capture_environment("run_4821", StaticProvider(dataset=dataset))

try:
    store.get_run("run_4821")
    resumed = True
except Exception:
    resumed = False
    store.create_run(Run(run_id="run_4821", goal="Analyze 1,000 documents"))
    store.append_event("run_4821", EventType.RUN_STARTED,
                       {"goal": "Analyze 1,000 documents", "total": 1_000})
    store.append_event("run_4821", EventType.DEPENDENCY_DECLARED,
                       {"resource": "dataset", "version": dataset})
    store.append_event("run_4821", EventType.EVIDENCE_ADDED,
                       {"evidence_id": "paper_128", "summary": "peer-reviewed study",
                        "source": "dataset"})
    store.append_event("run_4821", EventType.FINDING_ADDED,
                       {"finding_id": "finding_17", "claim": "X holds",
                        "evidence": ["paper_128"], "confidence": 0.91})

restored = manager.restore("run_4821") if resumed else None
done = restored.state.progress.completed if restored else 0
if resumed:
    print(f"    resumed at {done} documents (nothing reprocessed)")

for i in range(done, 1_000):
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})
    manager.maybe_checkpoint("run_4821", environment=env)

    if not resumed and i == 399:
        ledger = ActionLedger(store, "run_4821")
        if ledger.claim("github.create_issue", {"title": "Anomaly in batch 7"}).fresh:
            with open(effects, "a") as fh:          # the real external side effect
                fh.write("issue #481: Anomaly in batch 7\n")
            print("    created GitHub issue #481", flush=True)
            print("    PROCESS TERMINATED", flush=True)
            sys.stdout.flush()   # os._exit skips stdio teardown
            os._exit(9)          # no cleanup, no atexit, no close

manager.checkpoint("run_4821", environment=env)
print(f"    finished at {manager.restore('run_4821').state.progress.completed} documents")
