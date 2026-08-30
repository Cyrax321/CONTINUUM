# Bring your own dashboard

CONTINUUM as the verification layer under any control plane or UI. This guide shows how to consume the resume contract and evidence export so you can render run state, gate launches, and prove history without trusting the UI itself.

## What this gives you

- a sealed recovery contract: the only legal next action, plus why, plus what was verified
- a content-addressed evidence stream: every event as a neutral primitive with hash-chain inputs so the receiver can detect truncation or tampering by recomputing the chain exactly as `verify()` does
- chain attestation: an Ed25519 signed head for external verifiers

Your dashboard is presentation. CONTINUUM stays the substrate. That separation is deliberate: a UI bug cannot turn an unsafe resume into a safe one because safety is decided in the storage layer, not in the renderer.

## Substrates consumed

### 1. Resume contract (`continuum resume --json`)

Shell:

```bash
continuum resume my-task --json | python -m json.tool
```

Python:

```python
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage

store = SQLiteStorage("continuum.db")
decision = RecoveryEngine(store).assess("my-task", current_environment=None)
print(decision.mode)      # RecoveryMode.RESUME, REQUEST_HUMAN, ...
print(decision.safe)      # bool, also exit code 0 means safe
print(decision.contract.model_dump(mode="json"))
print(decision.human_steps)  # executable next steps
```

JSON shape (abridged):

```json
{
  "run_id": "my-task",
  "mode": "resume",
  "safe": true,
  "next_allowed_action": null,
  "contract": {
    "recovery_status": "safe_to_resume",
    "verified": ["goal", "progress"],
    "invalidated": [],
    "reason": "all state verified against the environment",
    "integrity_hash": "5cdda5...",
    "post_checkpoint_observations": [],
    "required_actions": [],
    "next_allowed_action": null
  },
  "human_steps": [],
  "family_rationale": [],
  "children": [],
  "pinning_drift": [],
  "informed_retry": null,
  "progress": {"completed": 3, "pending": 7, "failed": 0}
}
```

When blocked, the same structure carries the cause and the fix:

```json
{
  "mode": "request_human",
  "safe": false,
  "next_allowed_action": "reconcile_action:action_efbdf7a796e5e36b792ca57f2eb3868e",
  "contract": {
    "recovery_status": "requires_human",
    "reason": "1 external side effect(s) have unknown outcomes; at least one repair needs a person",
    "required_actions": ["reconcile_action:action_efbdf7a796e5e36b792ca57f2eb3868e"]
  },
  "human_steps": [
    "check whether 'slack.notify' actually reached the outside world, then call continuum_reconcile_action(run_id=my-task, action_key=..., occurred=true|false)"
  ]
}
```

Exit code mirrors `safe`: only a verified-safe run exits 0. Gate launches with `continuum resume my-task && ./start-agent.sh`.

Family rollup (multi-agent, issue #243): when a parent has children, the same `resume` JSON includes `family_rationale` and `children`, and a `requires_human` blocked parent whose only block is an unsafe child surfaces that child in the contract text.

### 2. Evidence export (`continuum export-evidence`)

Shell:

```bash
continuum export-evidence my-task > evidence.jsonl
head -n1 evidence.jsonl | python -m json.tool
```

Python:

```python
from continuum.interchange.evidence import export_evidence, verify_export

primitives = export_evidence(store, "my-task")
for p in primitives:
    print(p.kind, p.sequence, p.content_hash[:12])

# receiver-side tamper check (same logic as verify())
ok = verify_export(primitives)
```

Each line in the export is one of four neutral primitives:

- `transition` — every event appended to the log
- `observation` — validations and diffs
- `relation` — dependency edges
- `checkpoint` — sealed checkpoints

Every primitive is content-addressed:

```json
{
  "kind": "transition",
  "run_id": "my-task",
  "sequence": 4,
  "event_id": "evt_...",
  "event_type": "WORK_COMPLETED",
  "content_hash": "2396017db9a7...",
  "prev_hash": "abc...",
  "origin": "human",
  "timestamp": "2026-08-28T04:39:36Z",
  "payload": {"doc": 2},
  "signature_inputs": {}
}
```

A receiver recomputes the chain from `prev_hash` and `content_hash` exactly as `store.verify_events(run_id)` does. Truncation or reordering is detected without trusting the sender.

### 3. Chain attestation (optional)

For an external verifier that holds only the attestation file:

```bash
continuum attest-keygen --out signer.pem --pub signer.pem.pub
continuum attest my-task --key signer.pem --out my-task.attest.json
continuum attest-verify my-task --attest my-task.attest.json --json | python -m json.tool
```

Verdicts are `SIGNED`, `ALTERED`, or `UNTRUSTED` and appear in both human text and JSON.

## Minimal dashboard

A 60-line Python example you can drop in and adapt. It polls the live store, renders the contract, and streams evidence.

```python
# dashboard_minimal.py — polling example, adapt to your framework
import json
from pathlib import Path
from flask import Flask, jsonify, render_template_string
from continuum.storage import SQLiteStorage
from continuum.recovery import RecoveryEngine
from continuum.interchange.evidence import export_evidence

DB = "continuum.db"
app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<title>CONTINUUM dashboard</title>
<h1>Run {{run_id}} — {{mode}} (safe={{safe}})</h1>
<p>Goal: {{goal}}</p>
<p>Progress: {{completed}} / {{total or '?'}} completed</p>
<h2>Contract</h2>
<pre>{{contract_text}}</pre>
<h2>Next steps</h2>
<ul>{% for s in human_steps %}<li>{{s}}</li>{% endfor %}</ul>
<h2>Evidence (last 5)</h2>
<pre>{{evidence_text}}</pre>
"""

@app.get("/")
def index():
    return '<a href="/run/my-task">open my-task</a>'

@app.get("/run/<run_id>")
def run_page(run_id: str):
    store = SQLiteStorage(DB)
    try:
        store.get_run(run_id)
    except Exception as exc:
        return jsonify(error=str(exc)), 404
    decision = RecoveryEngine(store).assess(run_id)
    evidence = export_evidence(store, run_id)[-5:]
    return render_template_string(TEMPLATE,
        run_id=run_id, mode=decision.mode.value, safe=decision.safe,
        goal=decision.state.goal.description,
        completed=decision.state.progress.completed,
        total=decision.state.progress.total,
        contract_text=json.dumps(decision.contract.model_dump(mode="json"), indent=2),
        human_steps=decision.human_steps,
        evidence_text=json.dumps([e.model_dump(mode="json") for e in evidence], indent=2))

@app.get("/api/run/<run_id>/resume")
def api_resume(run_id: str):
    store = SQLiteStorage(DB)
    decision = RecoveryEngine(store).assess(run_id)
    payload = {
        "run_id": decision.run_id, "mode": decision.mode.value, "safe": decision.safe,
        "next_allowed_action": decision.next_allowed_action,
        "contract": decision.contract.model_dump(mode="json"),
        "human_steps": decision.human_steps,
        "progress": {"completed": decision.state.progress.completed, "pending": decision.state.progress.pending}
    }
    return jsonify(payload)

@app.get("/api/run/<run_id>/evidence")
def api_evidence(run_id: str):
    store = SQLiteStorage(DB)
    return jsonify([p.model_dump(mode="json") for p in export_evidence(store, run_id)])

if __name__ == "__main__":
    app.run(port=8766)
```

Run it:

```bash
pip install flask
python dashboard_minimal.py
# open http://localhost:8766/run/my-task
```

The API endpoints mirror the two shell primitives, so any frontend (React, Svelte, plain fetch) can consume them without learning CONTINUUM internals. The `contract.integrity_hash` can be rechecked with `continuum.recovery.contract.verify_contract` before rendering, and the evidence chain can be re-verified with `verify_export`.

## Hard-kill recovery in the dashboard

Prove the dashboard reflects reality before you trust it. Same mechanism as the harness recipes:

```bash
rm -f /tmp/byod-demo.db /tmp/byod-demo.db-wal /tmp/byod-demo.db-shm
continuum --db /tmp/byod-demo.db start dashboard-demo --goal "Dashboard BYO test"

python - << 'PY'
from continuum.models import Origin
from continuum.events import EventType
from continuum.storage import SQLiteStorage
from continuum.actions.ledger import ActionLedger
db="/tmp/byod-demo.db"
store=SQLiteStorage(db)
for i in range(3):
    store.append_event("dashboard-demo", EventType.WORK_COMPLETED, {"doc": i}, source=Origin.HUMAN)
ledger=ActionLedger(store, "dashboard-demo")
out=ledger.claim("slack.notify", {"channel": "#alerts"})
print("claimed", out.key)
PY

# SIGKILL simulation done, now ask the dashboard substrate what it should show
continuum --db /tmp/byod-demo.db resume dashboard-demo --json | python -m json.tool | head -n 60
continuum --db /tmp/byod-demo.db export-evidence dashboard-demo | wc -l
continuum --db /tmp/byod-demo.db verify dashboard-demo
```

Expected resume contract (real output, ids vary):

```json
{
  "mode": "request_human",
  "safe": false,
  "contract": {
    "recovery_status": "requires_human",
    "reason": "1 external side effect(s) have unknown outcomes; at least one repair needs a person",
    "next_allowed_action": "reconcile_action:action_efbdf7a796e5e36b792ca57f2eb3868e"
  },
  "human_steps": [
    "check whether 'slack.notify' actually reached the outside world, then call continuum_reconcile_action(...)"
  ]
}
```

Verify reports `Event chain verified` (the log is intact) and `export-evidence` line count equals `store.last_sequence`. The dashboard rendering this contract shows a blocked run with an actionable next step. After reconciling, the same endpoints flip to `resume` / `safe: true`.

Clean-run case stays `resume` and `safe: true`, and evidence primitives include the checkpoints and observations your dashboard can render as a timeline.

Real `os._exit(9)` with a subprocess before the claim settles gives the same `request_human` contract, verified in the harness guides.

## Ten-minute integration attempt (honest report)

Measured from a fresh checkout:

1. `uv pip install -e ".[dev]"` (about 40s)
2. `continuum start my-task --goal "trial"` (1s)
3. `python dashboard_minimal.py` (or `continuum resume my-task --json` from any UI) (1s)
4. Simulate kill, re-poll `GET /api/run/my-task/resume` and see `request_human` (under 1s)
5. Reconcile, re-poll, see `resume` (under 1s)

Total under two minutes once dependencies are cached. No gap found for the contract substrate. Evidence export gap notes: the `checkpoint` primitive appears only after `continuum checkpoint` or adapter `capture_state`; a run with no checkpoints exports only transitions and observations, which is correct, not a missing primitive. The receiver should not assume a checkpoint exists until one is taken.

## Security notes

- Hash chain is the truth. Compare `content_hash`/`prev_hash` in evidence export against `continuum verify my-task`. Do not trust `events` that fail `verify`.
- Resume contract sealing is deterministic. The same state and environment always produce a byte-identical contract, so comparison in tests is meaningful.
- Externally reported progress (`Origin.EXTERNAL_AGENT`, e.g., via MCP) resolves to `request_human` until a human `continuum confirm` attests it. That is not a stuck run, it is the anti-self-certification guarantee. Confirm via `continuum confirm my-task` or `continuum_confirm` via MCP (gated behind `CONTINUUM_MCP_CONFIRM_TOKEN`).
- Stale environment is localized: only the subtree depending on a changed resource is invalidated. Check `contract.invalidated` to see exactly what moved, and `pinning_drift` for assert-level pin mismatches.

## See also

- `docs/guides/embed-claude-code.md` and `docs/guides/embed-codex.md` for the harness that feeds this dashboard
- `src/continuum/interchange/evidence.py` for primitive definitions and `verify_export`
- `docs/api/cli.md` for `resume`, `verify`, `export-evidence`, `attest` semantics
