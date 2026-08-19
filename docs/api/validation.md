# Validation

The validator checks a run's projected state against the live environment and
against the model it was built for. Staleness propagates from a dependency to the
evidence resting on it, to the finding, to the final decision.

```python
from continuum.state.validator import StateValidator, validate_state

outcome = validate_state(state, current_environment=env, expected_model="gpt-4o")
print(outcome.report.status)   # VALID | STALE | CONFLICTED | UNKNOWN
```

## StateValidator

`continuum.state.validator.StateValidator(*, strict_unknown=True, confirmed=False)`

### `validate(state, *, current_environment=None, checkpoint_environment=None, checkpoint_version=0, expected_model=None, confirmed=False) -> ValidationOutcome`

Validate `state` against the current environment and the model. `strict_unknown`
makes an unconfirmed, unknown outcome block a safe resume; `confirmed` records
that a human has vouched for the state.

## validate_state

`continuum.state.validator.validate_state(state, *, current_environment=None, checkpoint_environment=None, expected_model=None) -> ValidationOutcome`

Module-level convenience that builds a default `StateValidator` and validates.

## ValidationOutcome

`continuum.state.validator.ValidationOutcome(state, report, environment_diff)`

### `report`

The `StateValidationResult`: per-component statuses, the overall `status`, and any
findings. A component whose status is not `VALID` is what makes the run unsafe to
resume. External dependencies that have drifted report `CONFLICTED`.

### `environment_diff`

The `EnvironmentDiff` between the checkpoint's declared dependencies and the
current environment: which resources changed, and how.

### `render() -> str`

A human-readable validation report.
