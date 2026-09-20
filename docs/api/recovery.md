# Recovery

The recovery engine decides how, and whether, a run may resume. It combines the
validator's environment check, the action ledger's uncertain outcomes, and the
checkpoint state into a single `RecoveryDecision`.

```python
from continuum.recovery.engine import RecoveryEngine

engine = RecoveryEngine(storage)
decision = engine.assess(run_id, current_environment=env)
print(decision.mode)        # RESUME | REPAIR_AND_RESUME | ROLLBACK | WAIT | REQUEST_HUMAN | REPLAN | ABORT
if decision.permits("resume"):
    ...
```

## RecoveryEngine

`continuum.recovery.engine.RecoveryEngine(storage, *, validator=None, strict_unknown=True, validation_rules=None, registry=None)`

### `assess(run_id, *, current_environment=None, expected_model=None, replay=True, validation_rules=None) -> RecoveryDecision`

Decide how `run_id` may resume, without changing anything. `current_environment`
is the live environment to compare against the checkpoint's declared dependencies;
`expected_model` pins the model the run was built for. The engine takes the
maximum on a severity ordering, so the most cautious signal wins regardless of
evaluation order.

`validation_rules` are domain staleness rules ([issue #761](../guides/validation-rules.md)):
they run after built-in validation, over the state it already revised, and their
findings merge by maximum caution, so a rule can escalate a component's status
but can never relax one. Rules are read-only and receive only the state and the
environment. An engine with no rules configured behaves exactly as before, and
a rule that finds nothing leaves the report and the sealed contract unchanged.
Per-assessment rules add to the engine's configured set rather than replacing it.
Nothing is auto-discovered: a rule is executed only because it was handed to the
engine or registered in a `Registry`.

The MCP `continuum_resume` tool and the `continuum resume` CLI accept an
*optional* `run_id`: when omitted they resolve the **active run** via
`Storage.get_active_run()`, the most recently touched run not in a terminal state
(`COMPLETED`/`CRASHED`/`ABORTED`/`FAILED`). That is what lets a fresh session
resume an interrupted run without having remembered its id.

## RecoveryDecision

`continuum.recovery.engine.RecoveryDecision(run_id, mode, contract, plan, validation, restored, uncertain_actions=(), rationale=())`

### `mode`

One of the seven `RecoveryMode` values: `RESUME`, `REPAIR_AND_RESUME`, `ROLLBACK`, `WAIT`, `REQUEST_HUMAN`, `REPLAN`, or `ABORT` (there is no `REPLAY` mode).

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

## Liveness and risk signals

Beyond validation, the engine weighs two live signals. A liveness advisory
from `continuum.recovery.health` reports whether the run has gone quiet past
its cadence contract (`LIVENESS_SILENCE_DETECTED` events in the log, evaluated
by `continuum watch`), and
`triggering_risks` carries any external risks mapped through
`.continuum/risk-policy.json` (`src/continuum/recovery/risk.py`). Both are
advisory: silence and risk inform the verdict without replacing validation,
gate, or ledger evidence. See `src/continuum/recovery/engine.py` for how the
signals combine, and the operator guides `docs/guides/liveness-watch.md` and
`docs/guides/risk-policy.md` for the workflows.
