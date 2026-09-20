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

`continuum.actions.ledger.ActionLedger(storage, run_id, *, lease=None, holder_id=None, ttl=None)`

Deduplication is a fold of the log followed by an append, not an atomic
compare-and-set, so two processes claiming one key at the same instant can both
read "no prior slot". `lease` closes that window: pass a
`continuum.concurrency.LeaseCoordinator` and every mutating method acquires the
run's lease before it reads and releases it after it writes, so concurrent
claimants collapse to a single winner and the losers raise `ClaimLockError`
instead of opening a parallel slot.

`holder_id` is required alongside `lease` and must be a stable agent identity. A
shared default would make two processes look like the same holder and silently
defeat the serialization. `ttl` overrides the coordinator's default lease
lifetime.

The lease is reentrant for its own holder, so a caller that already acquired the
run lease (the pattern in [multi-agent isolation](../multi_agent_isolation.md))
can use the ledger inside it; the ledger leaves releasing to whoever acquired.

Omitting `lease` keeps the single-process behaviour unchanged: nothing is
acquired, and exactly-once is only as strong as the caller's own serialization.

```python
from continuum.actions.ledger import ActionLedger, ClaimLockError
from continuum.concurrency import SQLiteLeaseCoordinator

ledger = ActionLedger(
    storage, run_id, lease=SQLiteLeaseCoordinator("leases.db"), holder_id="agent-a"
)
try:
    outcome = ledger.claim("email.send", {"to": "alice"})
except ClaimLockError:
    return  # another agent owns this run right now; back off and retry
```

Two limits are worth knowing. The lease is scoped to one `run_id`, so an
unscoped claim (`scoped_to_run=False`) racing the same key from another run is
not covered by this run's lease. And the claim is still not atomic in storage, so
mixing leased and unleased ledgers on one run yields the weaker guarantee.

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

## ClaimLockError

`continuum.actions.ledger.ClaimLockError`

Raised by a lease-aware ledger when the run's lease is held by another holder, in
place of writing anyway. A subclass of `LedgerError`. Seeing it means another
agent owns the run: back off and retry rather than forcing the write, because
whatever that agent is claiming may be the very action this caller wanted.

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

## Authority consumption

One-time authorities (credentials, approval tokens) are tracked so a replay
never resurrects a spent grant. `record_authority_consumed` in
`src/continuum/actions/authority.py` appends a hash-chained
`AUTHORITY_CONSUMED` event that never deduplicates, and the gate refuses reuse
of a consumed authority until a reconciler probe settles it with
`AUTHORITY_RECONCILED` (`src/continuum/reconcilers.py`). The ledger's retry
path checks the same events so a live retry under a reused key cannot slip
past (`src/continuum/actions/ledger.py`).
