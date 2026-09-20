# Webhook-out: the bell next to the HITL door

A run that blocks on `request_human` is safe, but stuck: it parks until a
human looks. Polling `continuum resume` or watching the dashboard both
require someone to already be paying attention. The webhook stack pushes the
verdict out instead, so an unattended agent cannot sit blocked for hours
because nobody polled.

## The registry

`.continuum/webhooks.json` declares the receivers, following the same
data-beside-code convention as the gate and gateway registries:

```json
{
  "dashboard_base_url": "http://localhost:8765",
  "endpoints": [
    {
      "url": "https://hooks.example.com/continuum",
      "secret": "operator-held-hmac-key",
      "events": ["request_human"],
      "re_notify_seconds": 3600,
      "retries": 2,
      "timeout": 5.0
    }
  ]
}
```

* `url` is the receiver; `secret` enables HMAC-SHA256 signing (below).
* `events` filters what the endpoint hears about: `request_human` (the
  default) and optionally `requires_review`. An unknown name is refused at
  load time, because a typo'd filter that silently never fires is the
  quietest failure there is.
* `re_notify_seconds` is the dedup window (below). `retries` and `timeout`
  bound delivery.
* `dashboard_base_url` turns each notification into a deep link
  (`<base>/runs/<run_id>`) so the recipient can act from the message.

A missing file means no webhooks: everything stays silent, nothing changes.
A file that exists but is malformed is refused loudly, naming the resolved
path to open.

## When the bell rings

`continuum resume` that ends in `request_human` - including a parent whose
family is blocked by an unsafe child - delivers one notification per
endpoint. The payload mirrors the resume JSON contract (`mode`, `safe`,
`family_rationale`, `children`, the full contract, repairs, progress) plus
`event` and, when configured, `dashboard_url`.

Dedup is the load-bearing part: one notification per *transition* into a
blocked state, not per assessment. The identity of a verdict is
`(run_id, mode, contract hash)`; a cron that re-runs `resume` every minute
produces the same verdict and therefore no new notification. When the run
settles and later blocks again, the contract differs, so the bell rings
again. The window is `re_notify_seconds` per endpoint.

The dedup state lives in the run's event log (`NOTIFICATION_SENT` /
`NOTIFICATION_FAILED` rows), so it survives restarts, and a failed delivery
is still a dead-letter row an operator can find by polling the log.

Delivery is fire-and-forget and never gates anything: a receiver that is
down changes nothing about the recovery verdict, the exit code, or the
output of `resume` beyond one warning line.

## Signatures

With a `secret` configured, every POST carries
`X-Continuum-Signature: sha256=<hmac>` over the exact request body. The
receiver verifies with constant-time comparison:

```python
import hashlib, hmac

def verify(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Without a `secret` the wire format is a plain JSON POST. The signature
matters for the same reason `CONTINUUM_MCP_CONFIRM_TOKEN` does: the receiver
must be able to trust that the notification came from CONTINUUM and was not
forged by the very agent being reported on.

The secret comes from either of two places. A per-endpoint `secret` string in
the webhook registry wins when it is present; otherwise `CONTINUUM_WEBHOOK_SECRET`
is the default signing secret for every endpoint that does not declare one:

```bash
export CONTINUUM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
```

Set neither and the wire format is the plain JSON POST above -- the signature
header is simply absent, which is what an endpoint opting out of verification
sees.

## Verify the wiring

`continuum notify-test` posts a test payload to every configured endpoint -
bypassing dedup by design, since a wiring probe is not a state transition -
and exits non-zero when any endpoint refuses:

```bash
continuum notify-test
```

Use it after configuring the registry, and after any change on the receiving
side, so the first real `request_human` is never also the first delivery
attempt.

The liveness watch (`continuum watch --on-breach webhook`) predates this
stack and sends its own one-shot payload; see
[liveness watch](liveness-watch.md).
