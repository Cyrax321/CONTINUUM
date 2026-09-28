# Reconciler plugins: settling uncertain side effects with external evidence

When an agent crashes between performing a side effect and recording it, the ledger
marks that action uncertain. Only the outside world knows whether the effect
landed. CONTINUUM has two ways to ask it, and they compose:

- **Probes** (`continuum reconcile` with `.continuum/reconcilers.json`, issue #218)
  are operator-configured commands: one per action type, run in a subprocess,
  printing `occurred=true|false|unknown`.
- **Reconciler plugins** (issue #765) are in-process Python objects implementing the
  `ActionReconciler` seam: an OpenTelemetry span store, a payment provider's API,
  a filesystem outbox, a queue's dead-letter table.

The plugin seam existed since Phase 7 with no consumer; `docs/ARCHITECTURE_EVOLUTION.md`
listed it as "declared, not load-bearing". `continuum.plugins.reconcile` is the
consumer.

## The seam

A reconciler is any object with this shape (no base class, no import required):

```python
from continuum.models import Action
from continuum.plugins import Reconciliation


class OutboxReconciler:
    name = "outbox"

    def reconcile(self, action: Action) -> Reconciliation:
        ...
```

`Reconciliation` is three-valued, and the third value is not a hedge:

| `occurred`   | meaning                                                |
|--------------|--------------------------------------------------------|
| `True`       | evidence the effect exists was found                   |
| `False`      | evidence of absence was found                          |
| `None`       | looked, and could not obtain evidence                  |

Returning `None` is the honest answer when a source is unreachable or has nothing
to say. It is *never* read as evidence of absence.

## Registering reconcilers

Registration is always explicit. Nothing is auto-discovered: a resume or
reconcile operation must not implicitly execute code the operator did not ask for.

From the CLI, name each plugin by dotted path (repeatable):

```console
$ continuum reconcile run_1 \
    --reconciler myapp.reconcilers:OutboxReconciler \
    --reconciler myapp.reconcilers:SpanReconciler
```

From Python, pass the collection directly, or resolve it from a `Registry`:

```python
from continuum.plugins import Registry, settle_with_reconcilers

registry = Registry()
registry.register("outbox", OutboxReconciler())
registry.register("otel", SpanReconciler())

report = settle_with_reconcilers(storage, "run_1", registry)
```

A `Registry` is resolved by seam membership: every registered service that
structurally conforms to `ActionReconciler` participates, regardless of the name
it was registered under.

Probes and plugins compose: the probe pass runs first, and plugins only ever see
the actions the probes left pending. A probe's settlement is never revisited or
contradicted by a plugin.

## The result contract

Every action the plugins assess lands in exactly one of four categories:

| Outcome                 | When it happens                                     | What the ledger does            |
|-------------------------|-----------------------------------------------------|---------------------------------|
| `confirmed_occurred`    | Every source with evidence agrees it happened       | Settled `COMPLETED`             |
| `confirmed_not_occurred`| Every source with evidence agrees it did not        | Settled `FAILED` (retryable)    |
| `unavailable_evidence`  | No source applied, all declined, or **any errored** | Escalated to `REQUIRES_REVIEW`  |
| `conflicting_evidence`  | Sources with evidence disagreed                     | Escalated to `REQUIRES_REVIEW`  |

Two rules make this safe rather than merely convenient:

**An errored source blocks confirmation.** A reconciler that raised or returned
a malformed value could not say whether it agrees, so its silence is not
neutrality. Even if three other sources confirm, one broken source escalates the
action. Settling on the survivors would let a broken plugin quietly veto a real
conflict.

**Conflict is never adjudicated.** Two sources disagreeing is escalated, not
majority-voted. A machine has no way to rank two external systems it did not
build, and picking either side would let a broken reconciler veto a correct one.

## Implementing an OpenTelemetry reconciler (#268 compatibility)

An OpenTelemetry span reconciler inspects an external tracing backend to confirm
whether the side effect completed. Because `ActionReconciler` receives only the
`Action` record, the plugin encapsulates its own tracing client and queries:

```python
from typing import Any
from continuum.models import Action
from continuum.plugins import ActionReconciler, Reconciliation


class OtelSpanReconciler:
    name = "otel_spans"

    def __init__(self, trace_client: Any) -> None:
        self.client = trace_client

    def reconcile(self, action: Action) -> Reconciliation:
        # Search for spans matching the action's idempotency key or action_id
        try:
            spans = self.client.find_spans(
                attributes={"continuum.action_id": action.action_id}
            )
        except Exception as exc:
            # An error can be returned as None or allowed to raise to fail closed
            return Reconciliation(occurred=None, note=f"trace backend query failed: {exc}")

        if not spans:
            return Reconciliation(occurred=None, note="no matching span observed")

        span = spans[0]
        if span.status.is_ok:
            return Reconciliation(
                occurred=True,
                external_id=span.context.span_id,
                note=f"confirmed by span {span.name}",
            )
        elif span.status.is_error:
            return Reconciliation(
                occurred=False,
                note=f"span reported error: {span.status.description}",
            )
        return Reconciliation(occurred=None, note="span in progress or ambiguous")
```
