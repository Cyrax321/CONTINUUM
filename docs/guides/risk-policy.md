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
  never blocking the run. Scores default to `0.0` and clamp to `[0.0, 1.0]`,
  details are bounded to 32 keys and 512 chars per string.

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

Copy the example and edit the mapping. Unknown trigger names and downgrade
attempts below safe baseline defaults fail closed at load time:

```json
{
  "loop": "replan",
  "loop_persisting": "rollback",
  "error_cascade": "wait",
  "meltdown": "rollback",
  "governance_decay": "request_human"
}
```

A file that is not a JSON object, contains unknown trigger keys, specifies a
value outside the mode set plus `annotate`, or attempts to downgrade a severe
trigger below its baseline default, fails validation at load. To preview a custom
file without installing it, load it explicitly (`load_risk_policy(path)`) or point
the working directory at it; assessment reads `.continuum/risk-policy.json` and
falls back to the built-in default when absent.

## Rules worth knowing

- Severity decides, not arrival order: of all recorded triggers, the one
  mapping to the most severe mode wins, and ties accumulate event ids.
- `annotate` is deliberately silent: it records interest without proposing a
  mode, for signals you want visible in history but not yet acting.
- `token_runaway` escalates on confidence, not just on its mapped mode: at or
  above `token_runaway_threshold` (default `0.8`) the trigger proposes `abort`
  whatever the mapping says, because a runaway the monitor is that sure about
  is past waiting. Assessment reads the event's `score`, so the threshold you
  configure is the threshold a decision uses.
- A policy file that fails validation does not silence the run: assessment
  falls back to the built-in conservative defaults and counts a
  `risk_policy.load_fallback` marker, so a misconfigured file costs the
  customization but never the `meltdown` and `side_effect_duplicate`
  protections. The file itself still fails closed when you load it directly
  through `load_risk_policy`.
- Risk never certifies: a signal can only escalate toward caution, and
  `EXTERNAL_MONITOR` provenance keeps it from counting as verification.
- An observation is knowledge, not a change (issue #1421): every recorded
  signal is folded into `SemanticState.observed_risks`, but the fold does not
  mint a state version. A run that was merely *seen* doing something risky is
  the same run it was a moment ago; the mitigation is what moves it, and that
  bumps the version through whatever it changed.

## The payload schema

`RiskObservedPayload` in `src/continuum/models.py` is the typed shape of a
signal, and `ingest_risk` validates and normalises through it before anything
reaches the log, so what is on the wire is always what the schema describes:

| Field | Meaning |
| --- | --- |
| `trigger` | Risk class, normalised to lowercase. Not restricted to the nine the default policy knows: a monitor may name a class policy has not mapped yet, and the signal is recorded and ignored until it does. |
| `score` | Confidence in `[0.0, 1.0]`, clamped rather than rejected. |
| `episode_id`, `step_id` | Where the risk was seen, when the monitor reports them. |
| `detail` | Structured diagnostics, free-form but bounded (32 keys, 512 chars per string). A plain string is accepted and wrapped as `{"message": ...}`, because ingestion predating the schema truncated detail to text. |
| `ts` | Observation time on the monitor's own clock, as an epoch number or ISO 8601. An unparseable value is re-dated rather than dropping the signal. |

`source` is deliberately absent from that table: it is not part of the payload
a monitor sends. The writer stamps `EXTERNAL_MONITOR` unconditionally, so no
signal can be self-certified by the agent it describes.
