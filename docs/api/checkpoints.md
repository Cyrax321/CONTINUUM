# Checkpoints

Checkpoints persist a run's semantic state so a later session can resume from a
known-good point instead of replaying the whole event log.

```python
from continuum.checkpoint.manager import CheckpointManager

mgr = CheckpointManager(storage)
cp = mgr.checkpoint(run_id, state=state, reason="milestone", environment=env)
restored = mgr.restore(run_id)
```

## CheckpointManager

`continuum.checkpoint.manager.CheckpointManager(storage, *, policy=None)`

### `checkpoint(run_id, *, state=None, trigger="manual", reason="", environment=None) -> StateCheckpoint`

Create, seal, and persist a checkpoint unconditionally. `environment` pins the
resources the state depends on; those resources are declared as run dependencies
so a later drift is reported by the validator.

### `maybe_checkpoint(run_id, *, state=None, explicit=False, context_tokens=None, environment=None, now=None) -> StateCheckpoint | None`

Checkpoint only if the policy agrees. Returns `None` when the policy declines, so
call it on every step without paying for a checkpoint each time.

### `evaluate(run_id, *, state=None, explicit=False, context_tokens=None, now=None) -> CheckpointDecision`

Ask the policy whether a checkpoint is warranted right now, without writing
anything.

### `restore(run_id, *, replay=True) -> RestoredRun`

Load the newest checkpoint and catch it up to the log. `replay=True` replays
events recorded after the checkpoint so the returned state reflects all work.

### `project_current(run_id) -> SemanticState`

Fold the run's full event history into state, ignoring checkpoints.

### `history(run_id) -> Sequence[StateCheckpoint]`

Every checkpoint recorded for the run, in order.

## RestoredRun

`continuum.checkpoint.manager.RestoredRun(run_id, state, checkpoint, pending_events, replayed)`

The value returned by `restore`. `state` is the resumed state, `checkpoint` the
checkpoint it was based on, and `pending_events` the number of events replayed
after it.

## Policy

The default `CheckpointPolicy` honors triggers (manual, interval, event,
semantic, context-pressure, hybrid). Supply a custom `policy` to `CheckpointManager`
to tune when checkpoints are taken.
