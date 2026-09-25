# Escalation policy: budgeting human attention

A run that lasts weeks generates a lot of decisions the ledger cannot settle on
its own. Each one becomes `REQUIRES_REVIEW` and interrupts a human at once, and
a human interrupted that often stops reading before approving (arXiv:2606.08919,
arXiv:2606.22721). An approval nobody read is a gate with no gate in it.

Escalation policy spends the interruption where it matters: score each action,
buffer the cheap ones behind a window and an hourly cap, and escalate
immediately only the ones whose blast radius clears a threshold you set.

## Status

This guide covers the schema, the loader and the scorer (issue #1409). The
consumers are not wired yet: the deferred review queue (#1410) will batch and
the fatigue telemetry (#1411) will measure. Nothing in the engine reads the
policy today, so installing one changes no behaviour yet. The pieces are
shipped and tested on their own so the wiring lands against a fixed, known
scorer rather than a heuristic that moves with it.

## The moving parts

`.continuum/escalation.json` (copy `examples/escalation-policy.json` to start)
holds four knobs:

```json
{
  "hourly_prompt_cap": 10,
  "batch_window_seconds": 3600,
  "blast_radius_threshold": 0.8,
  "risk_weights": {"default": 0.2, "mem_write": 0.4, "mem_delete": 0.9}
}
```

- `hourly_prompt_cap` is the most interruptions per hour; the queue stops
  spending the budget rather than exceeding it.
- `batch_window_seconds` is how long a low-risk item waits for company before
  it interrupts, so one batch prompt carries several items.
- `blast_radius_threshold` is the score at or above which an item skips the
  window and interrupts immediately. `0.0` escalates everything; `1.0`
  escalates only a weight of exactly 1.0.
- `risk_weights` maps an action type or a resource class to a score in
  `[0.0, 1.0]`. `default` is what an unrecognised action falls back to.

## Scoring

`evaluate_action_risk(action_type, arguments, policy)` is pure and
deterministic: the same inputs always yield the same `ActionRisk(score,
immediate)`. The score is the *highest* applicable weight, not the average:

- the action's own type,
- a `resource_class` named in its arguments (`pgvector`, `mem0`), for weighting
  by the thing touched rather than the verb that touched it,
- the policy's `default`.

Taking the maximum is deliberate. When two classifications both apply and
disagree, the more dangerous one governs, so a low generic weight can never
dull a specific high one.

`immediate` is `score >= blast_radius_threshold`, and the boundary is inclusive:
an action that is exactly at the threshold interrupts at once.

## Writing policy

Copy the example and edit the weights. Unknown keys in `risk_weights` are
harmless but dead until an action carries them. A policy that names no
`default` scores every unrecognised action `0.0`, which is fail-safe: nothing
unfamiliar is escalated on its own, and the burden is on the operator to say
what is dangerous.

To preview a custom file without installing it, load it explicitly:

```python
from continuum.recovery.escalation import evaluate_action_risk, load_escalation_policy

policy = load_escalation_policy("examples/escalation-policy.json")
print(evaluate_action_risk("mem_delete", {"resource_class": "pgvector"}, policy))
```

```text
ActionRisk(score=0.9, immediate=True)
```

The loader resolves the path it is given, so point it anywhere; the engine will
read `.continuum/escalation.json` and fall back to the built-in default when it
is absent.

## Rules worth knowing

- A missing policy is the fail-safe default, not an error and not silence. The
  default cap and window are ordinary, deliberately unpermissive numbers: an
  operator who finds them noisy is meant to widen them on purpose, not discover
  that quiet was the shipped posture.
- A present but invalid policy raises `EscalationPolicyError` and loads
  nothing. Malformed JSON, a non-object root, a cap below 1, a negative window,
  a threshold outside `[0.0, 1.0]`, a weight outside that interval, an empty
  weight key: each names the offending file and value, because "must be a
  number" is the same sentence for a missing field, a string and a boolean and
  never points at the entry to fix.
- JSON booleans are not scores. `isinstance(True, int)` holds in Python, so a
  naive check reads `true` as `1.0` and escalates everything; booleans are
  refused rather than coerced, and a refused weight falls back to `default`
  rather than to zero.
- Unknown top-level keys are ignored, so a later schema can add a knob without
  rejecting policies written against this one.
- Scoring never raises on a hand-built policy. A malformed section degrades to
  the fail-safe floor, because the scorer is called from gate paths where an
  exception would be worse than a conservative score.
