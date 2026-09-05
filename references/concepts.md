## Core Concepts

### Semantic Checkpoints

Not a conversation dump. Not a full state snapshot. A compact, inspectable, versioned representation of what the agent actually needs to continue:

```json
{
  "run_id": "run_4821",
  "goal": {
    "description": "Analyze 10,000 documents for evidence supporting hypothesis X",
    "version": 3
  },
  "progress": { "completed": 3421, "pending": 6579, "failed": 3 },
  "decisions": [
    {
      "decision": "Only include peer-reviewed studies",
      "reason": "User requirement",
      "evidence": ["user_instruction_001"],
      "status": "valid"
    }
  ],
  "findings": [
    {
      "id": "finding_17",
      "claim": "Strong correlation observed in dataset subset A",
      "evidence": ["paper_128"],
      "confidence": 0.91
    }
  ],
  "pending_work": [
    "Search 2019-2022 literature",
    "Resolve contradictory evidence"
  ],
  "external_dependencies": [
    { "resource": "dataset", "version": "v3" }
  ]
}
```

### State Validation

CONTINUUM never blindly trusts an old checkpoint. Before recovery, every component is independently verified:

```
CHECKPOINT
     |
     v
STATE VALIDATOR
     |
     +-- environment unchanged?
     +-- dependencies unchanged?
     +-- permissions unchanged?
     +-- external actions still valid?
     +-- evidence still available?
     +-- goals still valid?
     +-- previous decisions still valid?
             |
             v
       VALID / STALE / CONFLICT
```

Every semantic state component carries a status:

| Component    | Status       |
|:------------|:------------|
| Goal         | `VALID`      |
| Progress     | `VALID`      |
| Decision #12 | `STALE`      |
| Evidence #81 | `VALID`      |
| Dataset      | `CONFLICTED` |
| Approval     | `EXPIRED`    |

### Idempotent Action Ledger

External side effects are tracked. If the agent crashes after creating GitHub issue #481, CONTINUUM prevents re-creation on recovery:

```
Agent:     Create GitHub issue.
CONTINUUM: Action already completed. External ID: #481. Returning previous result.
```

Action states: `PLANNED` > `STARTED` > `COMPLETED` / `FAILED` / `UNKNOWN` / `COMPENSATED` / `REQUIRES_REVIEW`

If the outcome of a side effect is uncertain, CONTINUUM raises `UNKNOWN_SIDE_EFFECT` instead of silently retrying.

### Recovery Modes

| Mode               | Trigger                           |
|:-------------------|:----------------------------------|
| `RESUME`           | Checkpoint fully valid            |
| `REPAIR_AND_RESUME`| Checkpoint partially stale        |
| `ROLLBACK`         | Critical state corrupted          |
| `WAIT`             | Dependency temporarily unavailable|
| `REQUEST_HUMAN`    | Side effect outcome uncertain     |
| `ABORT`            | Unrecoverable conflict            |

### Commitment Graph and Restore-Point Admissibility

A checkpoint can be locally valid yet semantically invalid to resume from
when downstream committed work depended on state produced after it. This is
the DART admissibility problem (arXiv:2605.23311). CONTINUUM records for
each completed action which inputs it consumed: checkpoint version,
event positions, component ids and prior action ids. On resume, the validator
walks forward from the checkpoint: if any completed action consumed an input
after the checkpoint, the checkpoint is inadmissible for plain RESUME. The
engine maps this to REPAIR_AND_RESUME when the commitment is re-derivable
(event or component) and to REQUEST_HUMAN when it involves the prior action
graph. The sealed contract lists each blocking commitment with its chain
position, so the refusal is machine-readable and auditable. Empty or absent
consumed_inputs remains admissible for backward compatibility.

### Recovery Contract

Before allowing resume, CONTINUUM generates a deterministic, machine-readable contract:

```json
{
  "run_id": "run_4821",
  "recovery_status": "SAFE_TO_RESUME",
  "verified": ["goal", "completed_documents", "evidence"],
  "invalidated": ["dataset_v3"],
  "required_actions": ["revalidate experiment results"],
  "next_allowed_action": "revalidate_dataset"
}
```

---

