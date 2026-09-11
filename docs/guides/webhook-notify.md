# Webhook notifications: paging a human on blocked verdicts

A run that blocks on `request_human` is only useful if a human learns about
it. The webhook stack delivers one signed page per distinct blocked verdict
to operator-configured endpoints: `.continuum/webhooks.json` declares them,
`continuum resume` fans out on blocked verdicts with log-backed dedup so cron
re-assessments stay silent, and `continuum notify-test` verifies wiring
without needing a real blockage.

## The registry

```json
{
  "webhooks": [
    {"url": "https://hooks.example.com/continuum", "secret": "opaque-shared-secret"},
    {"url": "http://10.0.0.9:8080/hook", "events": ["liveness_breach"], "timeout": 2, "max_retries": 3}
  ]
}
```

A missing file (or a missing `webhooks` key) means no endpoints and nothing
happens. A malformed file raises naming the file and entry index. `url` must
be http(s): anything else is refused so a typo cannot turn the notifier into
a local-file reader. `events` defaults to `request_human` and accepts
`requires_review` and `liveness_breach`. `timeout` must be a positive number
(booleans refused) and `max_retries` an int in 0..5, so typos cannot retry
forever.

## Delivery contract

```bash
export DB=/tmp/notify-demo/g.db
mkdir -p /tmp/notify-demo/.continuum
continuum --db $DB start notify-demo --goal "webhook walkthrough"
cat > /tmp/notify-demo/.continuum/webhooks.json <<'EOF'
{"webhooks": [{"url": "http://127.0.0.1:1/dead"}]}
EOF
continuum --db $DB notify-test; echo "exit=$?"
```

```text
  FAILED  http://127.0.0.1:1/dead
exit=1
```

Each subscribed endpoint gets up to 1 + `max_retries` attempts. The command
exits 0 only when every subscribed endpoint answers 2xx. Machine output:

```bash
continuum --db $DB --json notify-test
```

```json
{
  "event": "request_human",
  "results": {
    "http://127.0.0.1:1/dead": false
  }
}
```

Posts carry JSON with an `X-Continuum-Signature` HMAC-SHA256 header whenever
a secret is configured (per-endpoint `secret`, else `CONTINUUM_WEBHOOK_SECRET`);
without one the format is plain JSON. Delivery never raises: failures report
`false` and callers decide how loudly to note them.

## Blocked verdicts page once

On a `request_human` verdict, `resume` loads the registry, skips silently
when nothing subscribes, and otherwise fans out. Dedup key is the verdict
fingerprint over stable contract fields (wall-clock telemetry excluded, so
repeat assessments hash identically): the first identical verdict notifies,
repeats stay silent, and a genuinely new verdict re-notifies. Successes
record one `NOTIFY_SENT`, failures append `NOTIFY_FAILED` dead letters naming
url, event, and error. Verdict, text, and exit code are byte-identical with
and without a registry.

## Rules worth knowing

- Unknown event names in `--event` or the registry fail loudly at load, not
  silently never.
- Duplicate urls aggregate conservatively: a url reports delivered only when
  every entry sharing it delivered.
- Single page per verdict is deliberate: a recovered endpoint catches the
  next distinct verdict rather than re-paging every cron tick.
