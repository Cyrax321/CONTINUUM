# Recovery

The recovery engine decides how, and whether, a run may resume. It combines the
validator's environment check, the action ledger's uncertain outcomes, and the
checkpoint state into a single `RecoveryDecision`.

```python
from continuum.recovery.engine import RecoveryEngine

engine = RecoveryEngine(storage)
decision = engine.assess(run_id, current_environment=env)
print(decision.mode)        # RESUME | REPLAY | REQUEST_HUMAN | ABORT
if decision.permits("resume"):
    ...
```

## RecoveryEngine

`continuum.recovery.engine.RecoveryEngine(storage, *, validator=None, strict_unknown=True)`

### `assess(run_id, *, current_environment=None, expected_model=None, replay=True) -> RecoveryDecision`

Decide how `run_id` may resume, without changing anything. `current_environment`
is the live environment to compare against the checkpoint's declared dependencies;
`expected_model` pins the model the run was built for. The engine takes the
maximum on a severity ordering, so the most cautious signal wins regardless of
evaluation order.

## RecoveryDecision

`continuum.recovery.engine.RecoveryDecision(run_id, mode, contract, plan, validation, restored, uncertain_actions=(), rationale=())`

### `mode`

One of `RESUME` (safe to continue from the checkpoint), `REPLAY` (re-run from a
recorded plan), `REQUEST_HUMAN` (a human must adjudicate an uncertain side
effect), or `ABORT` (the run cannot be trusted to continue).

### `permits(action) -> bool`

Whether `action` is the single step the contract currently allows. For
`REQUEST_HUMAN` this is typically `confirm`; for `RESUME` it is `resume`.

### `render() -> str`

A human-readable explanation of the decision and its rationale, suitable for
printing or handing to an operator.

### `validation`, `restored`, `uncertain_actions`, `plan`, `contract`, `rationale`

The underlying `ValidationOutcome`, the `RestoredRun`, any actions whose outcome
is unknown, the repair `RepairPlan`, the `RecoveryContract`, and the textual
reasons for the decision.
