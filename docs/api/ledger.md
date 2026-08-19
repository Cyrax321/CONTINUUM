# Action ledger

The action ledger records side effects so a restarted or resumed agent never
repeats an effect it already performed. It is the durability layer behind
`adapter.intercept_action`.

```python
from continuum.actions.ledger import ActionLedger

ledger = ActionLedger(storage, run_id)
outcome = ledger.claim("email.send", {"to": "alice"})
if outcome.fresh:
    send_email("alice")
    ledger.complete(outcome.key, result={"sent": True})
```

## ActionLedger

`continuum.actions.ledger.ActionLedger(storage, run_id)`

### `claim(action_type, arguments=None, *, volatile=(), scoped_to_run=True, key=None, on_unknown=None) -> ActionOutcome`

Register intent to perform an action, or report that it already happened.
Returns an `ActionOutcome` whose `fresh` flag is `False` when the ledger
recognizes the action as already completed. `volatile` names arguments excluded
from identity. `key` is a stable idempotency key (for example
`invoice:INV-001`). `on_unknown` is invoked when an uncertain action is claimed
and lets the caller decide inline.

### `complete(key, *, external_id=None, result=None) -> Action`

Record that the effect succeeded. `external_id` is the resource the effect
produced (an invoice id, a file path); it is excluded from identity matching so a
richer re-claim is not mistaken for a new action.

### `fail(key, error, *, certain=True) -> Action`

Record that the effect did not happen. `certain=False` records a timeout, which
is treated as unknown rather than absent.

### `reconcile(key, *, occurred, external_id=None, result=None, note="") -> Action`

Resolve an uncertain action using evidence from the outside world. `occurred`
says whether the effect in fact happened; the ledger then marks the action
completed or cleared accordingly and, if `occurred` is false, removes the
recorded effect.

### `compensate(key, *, note="", by=None) -> Action`

Record that a completed effect was deliberately undone (for example a refund).

### `flag_for_review(key, reason) -> Action`

Escalate an action a human must judge.

### `get(key) -> Action | None`

Return the recorded action for a key, if any.

### `pending() -> Sequence[Action]`

Actions whose real-world outcome is not known (interrupted or timed out). These
are what recovery refuses to guess about.

### `all() -> Sequence[Action]`

Every recorded action for the run.

## ActionOutcome

`continuum.actions.ledger.ActionOutcome(key, action, fresh)`

The value returned by `claim`. `fresh` is `True` when the effect should still be
performed; `False` when the ledger already has a recorded outcome and
`action` carries it. `key` is the idempotency key to pass to `complete`, `fail`,
or `reconcile`.
