# Liveness watch: hearing silence as a signal

A run that stops emitting events may be resting, or it may be wedged. The
liveness stack turns that distinction into a contract: `.continuum/liveness.json`
declares how long each phase may stay quiet, and `continuum watch` reports
breach or quiet without ever gating or mutating beyond its two audit events.

## The cadence contract

```json
{
  "max_silence_seconds": 3600,
  "phase_scopes": {"open_claim": 600, "otherwise": 3600}
}
```

`max_silence_seconds` is the default threshold (one hour). `phase_scopes`
overrides it per phase: while a tool claim is open the tighter `open_claim`
scope applies, otherwise the `otherwise` scope. A partial file stays valid:
any missing scope falls back to `max_silence_seconds`. With no file at all the
built-in default above applies, so a fresh clone behaves sanely with nothing
configured.

## Watch a run

```bash
export DB=/tmp/live-demo/g.db
continuum --db $DB start live-demo --goal "liveness walkthrough"
```

Wait past the threshold, then check. `--max-silence` overrides the contract
for one invocation and always wins, even with no claim open:

```bash
continuum --db $DB watch live-demo --max-silence 1s
```

```text
Liveness: BREACHED, silence 3.3s exceeds threshold 1s (phase otherwise). Advisory only.
```

Exit code 20: breach is advisory, never a gate. A breach appends one
`LIVENESS_SILENCE_DETECTED` event (hash-chained, carrying silence, threshold,
and phase) unless the last liveness event already says detected, so repeated
checks do not spam the log. Check again inside the window and the run reports
recovery the same way:

```bash
continuum --db $DB watch live-demo --max-silence 1h
```

```text
Liveness ok for live-demo: 0.201496
```

Exit code 0. Because the previous check left a `DETECTED` marker, this check
appends `LIVENESS_RECOVERED` and the pair brackets the silent interval in the
log for audit.

## Where else the advisory appears

`continuum resume` appends the same liveness reading to its verdict,
read-only:

```bash
continuum --db $DB resume live-demo
```

```text
Next permitted action: continue

Liveness: ok, silence 22.8s within threshold 3600s (phase otherwise).
```

`continuum health` stays scoped to the prefix-trust score; liveness lives
with the commands that act on quiet.

## Breach webhook

For unattended runs, `--on-breach webhook` with `--webhook-url` POSTs the
advisory as JSON and still exits 20:

```bash
continuum --db $DB watch live-demo --max-silence 1s \
  --on-breach webhook --webhook-url https://hooks.example.com/continuum
```

Delivery is fail-open like every notification path: a dead receiver prints a
warning and never changes the verdict. The POST carries the advisory as plain
JSON with no authentication header, so use a secret URL path.

## Rules worth knowing

- Watch never gates: breach exits 20 and appends audit events, but resume
  decisions stay with the recovery engine.
- Silence is measured from the last event timestamp with an injected clock,
  so the check is deterministic and testable, not wall-clock flaky.
- A run with no events at all is never breached: there is no silence to
  measure yet.
