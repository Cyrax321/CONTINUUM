# Risk policy: teaching recovery which signals matter

Some failures are visible in state (stale evidence, uncertain actions) and some
arrive as signals from outside: a monitor reporting a latency anomaly, a judge
flagging a duplicated side effect, a rollout guard crying meltdown. Risk policy
maps those signal names to recovery modes so the engine weighs them alongside
everything else, with the most cautious proposal winning regardless of order.

## The moving parts

- `RISK_OBSERVED` events carry `trigger`, `score`, `detail` (plus optional
  `episode_id` / `step_id`). They are hash-chained and stamped
  `EXTERNAL_MONITOR`: a witness, never an authority, so a risk signal alone
  cannot certify a run.
- `risk-policy.json` (default `.continuum/risk-policy.json`, copy
  `examples/risk-policy.json` to start) maps trigger names to modes:
  `replan`, `wait`, `repair_and_resume`, `rollback`, `abort`, `request_human`,
  or `annotate` for watch-without-action. Unknown triggers and `annotate`
  propose nothing.
- At assess time the engine evaluates every recorded trigger against the
  policy, takes the most severe resulting mode, and names the winning event
  ids in the contract's `triggering_risks` field.
- Ingestion is fail-open: a malformed payload returns `False` and is dropped,
  never blocking the run. Scores default to `0.0`, details truncate at 512
  chars.

## Try it

```bash
export DB=/tmp/risk-demo/g.db
continuum --db $DB start risk-demo --goal "risk walkthrough"
```

Ingest one signal (in production a monitor posts these; here we post directly):

```bash
python - <<'EOF'
from continuum.storage import SQLiteStorage
from continuum.recovery.risk import ingest_risk_json_line
with SQLiteStorage("/tmp/risk-demo/g.db") as store:
    print(ingest_risk_json_line(store, "risk-demo", '{"trigger": "meltdown", "score": 0.9}'))
    print(ingest_risk_json_line(store, "risk-demo", "not json"))
EOF
```

```text
True
False
```

Assess the run:

```bash
continuum --db $DB --json resume risk-demo | python -c \
  "import json,sys; d = json.load(sys.stdin); print(d['mode'], d['safe'], len(d['contract']['triggering_risks']))"
```

```text
rollback False 1
```

The `meltdown` trigger maps to `rollback` in the default policy, so the run
proposes rollback and is unsafe to resume hands-free. Garbage ingestion
returned `False` and changed nothing.

## Writing policy

Copy the example and edit the mapping; unknown trigger names are ignored until
a signal arrives bearing them, so extra keys are harmless but dead:

```json
{
  "loop": "replan",
  "error_cascade": "wait",
  "meltdown": "rollback",
  "governance_decay": "request_human"
}
```

A file that is not a JSON object, or a value outside the mode set plus
`annotate`, fails validation at load. To preview a custom file without
installing it, load it explicitly (`load_risk_policy(path)`) or point the
working directory at it; assessment reads `.continuum/risk-policy.json` and
falls back to the built-in default when absent.

## Rules worth knowing

- Severity decides, not arrival order: of all recorded triggers, the one
  mapping to the most severe mode wins, and ties accumulate event ids.
- `annotate` is deliberately silent: it records interest without proposing a
  mode, for signals you want visible in history but not yet acting.
- Risk never certifies: a signal can only escalate toward caution, and
  `EXTERNAL_MONITOR` provenance keeps it from counting as verification.
