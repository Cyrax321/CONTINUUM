# Bring your own dashboard: consume the resume contract and evidence export

CONTINUUM is a verification layer, not a UI. This recipe shows how an external control plane or dashboard consumes the resume contract and the content-addressed evidence export as a substrate.

## What you get

- Resume contract (`continuum --json resume` or MCP `continuum_resume`) with `mode`, `safe`, `contract`, `human_steps`, `informed_retry`.
- Evidence export (`continuum export-evidence <run>`) with hash-chained primitives, verifiable via `stable_hash`.

## Resume contract (poll this)

```bash
continuum --json resume <run_id> | python -m json.tool
```

Key fields for a dashboard:

- `run_id`, `mode` (`RESUME`/`REQUEST_HUMAN`/…), `safe` (bool, only `RESUME` with `safe:true` exits 0)
- `contract.recovery_status`, `contract.verified`, `contract.invalidated`, `contract.required_actions`, `contract.next_allowed_action`
- `contract.human_steps`, `contract.post_checkpoint_observations`, `informed_retry`

Minimal polling loop:

```python
import json, subprocess, time
def fetch_contract(run_id, db="continuum.db"):
    out = subprocess.check_output(["continuum", "--db", db, "--json", "resume", run_id], text=True)
    return json.loads(out)
while True:
    c = fetch_contract("run_42")
    print(c["mode"], c["safe"], c["contract"]["next_allowed_action"])
    time.sleep(5)
```

Over MCP the same shape is returned by `continuum_resume` (read-only). Over the sidecar it is `dispatch("resume", {"run_id": ...})`.

## Evidence export (audit this)

```bash
continuum export-evidence <run_id> | head -n 5
```

Each line is a JSON primitive with `content_hash`, `prev_hash`, `origin`, `sequence`, `signature_inputs`. Re-verify:

```python
from continuum.security.hashing import stable_hash
def verify_exported(primitives, expected_count, expected_head):
    prev = None
    for i, p in enumerate(primitives, start=1):
        assert p["sequence"] == i
        assert p["prev_hash"] == prev
        assert p["content_hash"] == stable_hash(p["signature_inputs"])
        prev = p["content_hash"]
    assert len(primitives) == expected_count
    assert primitives[-1]["content_hash"] == expected_head
```

A middle truncation breaks `prev_hash`, a tail truncation breaks `length`/`final_hash`. The four kinds are `transition`, `observation`, `relation`, `checkpoint` (see `src/continuum/interchange/evidence.py`).

The export is pure read, zero new dependencies, and covers archived events after `continuum compact`.

## Bring-your-own-dashboard wiring

- List runs: `continuum --json runs` or `continuum --json tree <parent>` for multi-agent.
- Per-run: `continuum --json resume <run>` for the contract, `continuum export-evidence <run>` for audit, `continuum --json actions <run>` for ledger, `continuum --json inspect <run>` for state.
- Human-in-the-loop: When `mode==REQUEST_HUMAN`, render `human_steps` as buttons. Each maps 1:1 to CLI verbs (`continuum confirm`, `continuum reconcile`, `continuum complete`) and to dashboard HITL endpoints in `src/continuum/dashboard/hitl.py`.

## Hard-kill test

```bash
python - << 'PY'
import os
from continuum import SQLiteStorage, Run, ActionLedger
from continuum.events import EventType
store = SQLiteStorage("continuum.db")
store.create_run(Run(run_id="plane_test", goal="control plane test"))
store.append_event("plane_test", EventType.RUN_STARTED, {"goal": "control plane test"})
ledger = ActionLedger(store, "plane_test")
outcome = ledger.claim("payment.charge", {"id": "pay_123"})
print(f"claimed {outcome.key}")
if outcome.fresh:
    open("out.txt","w").write("charged")
    os._exit(9)
PY
echo "exit $?"  # 9
continuum --json resume plane_test | python -m json.tool | grep -E "mode|safe|next_allowed_action"
continuum export-evidence plane_test | wc -l
continuum export-evidence plane_test | python -c "
import json, sys
from continuum.security.hashing import stable_hash
prims = [json.loads(l) for l in sys.stdin]
prev=None
for p in prims:
    assert p['prev_hash']==prev
    assert p['content_hash']==stable_hash(p['signature_inputs'])
    prev=p['content_hash']
print(f'verified {len(prims)} primitives')
"
```

The control plane sees the same `REQUEST_HUMAN` contract and the same verifiable chain as `continuum verify`.

## Notes

The resume contract and evidence export are both pure read, so polling never mutates the log. Your UI owns orchestration; CONTINUUM owns `safe` and the sealed contract.
