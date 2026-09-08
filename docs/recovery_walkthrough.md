# End-to-end recovery walkthrough

This traces one failure from adapter error to sealed contract using nothing but
the real library. Every output block below was captured from an actual run of
examples/recovery_walkthrough.py against the current code; nothing is invented.
Action ids differ per run, so do not be surprised if yours look different.

The scenario: an agent summarizes quarterly reports from a pinned dataset. It
posts a notification to Slack, then dies before it can confirm the request
landed. While it was down, the dataset moved from v3 to v4.

## 1. The agent acts, then dies mid side effect

The ActionLedger records intent before the call and marks the outcome uncertain
when the process dies. An exception escaping an external call does not prove
nothing happened, so CONTINUUM refuses to guess.

```
== 1. the agent acts, then dies mid side effect ==
claimed action key: e2534dd58c13675530e60d5b5506845b22a30eb3cf85efe73d8a0a27b64c893a
recorded outcome: uncertain (the request may or may not have landed)
```

## 2. A fresh session assesses

A new process captures the environment, sees the dataset moved, and asks the
recovery engine what is allowed. The engine reduces three signals (validation,
ledger, checkpoints) and the most cautious applicable one wins: the unknown
side effect outranks the repairable staleness, so the verdict is
REQUEST_HUMAN, not a blind resume and not a full reset.

Note the localization in the validation report: only the subtree that rests on
`dataset` is invalidated. The goal and progress stay valid.

```
== 2. a fresh session assesses; the dataset moved v3 -> v4 ==
CONTINUUM RECOVERY

Run: walkthrough
Checkpoint: v0

State validation:
  [!!] external dependency dataset - v3 -> v4
  [!!] evidence q3_sales - source 'dataset' changed
  [!!] finding f_rev - rests on changed evidence: q3_sales
  [!!] decision d_send - rests on changed support: f_rev
  [ok] goal - v1
  [ok] progress - 0 completed

Action ledger:
  [!!] slack.notify: outcome unknown (id action_0243135)

Recovery decision: REQUEST_HUMAN
  because 1 external side effect(s) have unknown outcomes
  because at least one repair needs a person

Repairs required:
  1. [human] reconcile_action action_0243135b37757b236f1dfd7bf386ec83 - slack.notify was interrupted; the side effect may or may not have occurred
  2. [auto]  revalidate_dependency dataset - v3 -> v4
  3. [auto]  rederive_evidence q3_sales - source 'dataset' changed
  4. [auto]  rederive_finding f_rev - rests on changed evidence: q3_sales
  5. [auto]  review_decision d_send - rests on changed support: f_rev

Next permitted action: reconcile_action:action_0243135b37757b236f1dfd7bf386ec83
```

## 3. The sealed contract explains itself

The contract is immutable and hash-chained. Its Phase 1 fields carry the
decision's justification: `reason` is the threaded rationale and `evidence`
names what drove it. Whatever resumes this run has one permitted next action,
and it is the reconciliation, not the retry. When outside signals drive the
verdict, the contract names them in `triggering_risks`; that flow is walked
through in `docs/guides/risk-policy.md`.

```
== 3. the sealed contract explains itself (Phase 1 fields) ==
recovery_status:     requires_human
invalidated:         ['decision:d_send (stale)', 'evidence:q3_sales (stale)', 'external_dependency:dataset (conflicted)', 'finding:f_rev (stale)']
next_allowed_action: reconcile_action:action_0243135b37757b236f1dfd7bf386ec83
reason:              1 external side effect(s) have unknown outcomes; at least one repair needs a person
```

## 4. Evidence provenance

Each state item carries its provenance. The canonical view projects the three
axes (who, how trusted, what state) without collapsing them: the evidence was
deterministically captured (origin `deterministic`, canonically `observed`) but
is now `stale`.

```
== 4. evidence provenance, canonical view ==
evidence q3_sales: origin=deterministic status=stale canonical=stale
```

## 5. Reconcile the uncertain side effect

A ProbeReconciler asks the external system whether the effect exists. Slack
shows no message, so the action is resolved as failed rather than completed:
recording a definite failure is now safe because a probe, not optimism,
established it.

```
== 5. reconcile the uncertain side effect with a probe ==
resolved_completed: 0  resolved_failed: 1  unresolved: 0
```

## 6. Re-assess and resume

With the ledger quiet, the remaining work is exactly the stale subtree, every
step automatic, and the contract names the single next permitted action.

```
== 6. re-assess after reconciliation and repair ==
CONTINUUM RECOVERY

Run: walkthrough
Checkpoint: v0

State validation:
  [!!] external dependency dataset - v3 -> v4
  [!!] evidence q3_sales - source 'dataset' changed
  [!!] finding f_rev - rests on changed evidence: q3_sales
  [!!] decision d_send - rests on changed support: f_rev
  [ok] goal - v1
  [ok] progress - 0 completed

Action ledger:  [ok] no uncertain side effects

Recovery decision: REPAIR_AND_RESUME
  because 4 component(s) need repair before continuing

Repairs required:
  1. [auto]  revalidate_dependency dataset - v3 -> v4
  2. [auto]  rederive_evidence q3_sales - source 'dataset' changed
  3. [auto]  rederive_finding f_rev - rests on changed evidence: q3_sales
  4. [auto]  review_decision d_send - rests on changed support: f_rev

Next permitted action: revalidate_dependency:dataset
```

## What just happened

- No duplicate Slack message: the interrupted effect was reconciled by probe
  before any retry was permitted.
- No blind resume: four stale components were identified precisely, and the
  clean parts of the state were left alone.
- No lost rationale: the contract carries its own reason, evidence, and a
  single next allowed action, sealed by hash.

## Reproduce

```shell
uv run python examples/recovery_walkthrough.py
```
