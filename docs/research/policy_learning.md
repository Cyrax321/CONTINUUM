# Recovery Policy Learning from Ledger History

## Question

Can past recovery decisions improve the hand coded policy in `src/continuum/recovery/planner.py:1` and `src/continuum/recovery/engine.py:1`? The ledger holds every decision, its contract, and its reconciled outcome, so it is tempting to learn from it.

## Findings

- **Signal exists.** `RecoveryLedger` records per attempt kind and the final `RecoveryContract.reason`. Aggregating `attempts` and `requires_human` rates by `action_type` shows which adapter operations most often need human gates. This is useful as a report, not as an auto policy.

- **Risk of auto change.** The max severity ordering in `src/continuum/recovery/engine.py:62` is intentionally conservative: the most cautious signal wins. Learning a less cautious threshold from data that is itself biased by prior conservative decisions would silently lower the bar. A ledger trained on past human gates cannot prove that a gate was unnecessary, only that it happened.

- **Safe use is advisory.** The ledger history can inform periodic human review of the policy weights, not autonomous updates. Example: if `POST /payments` shows a 90 percent human gate rate due to `probe` timeouts, the fix is to improve the probe, not to lower the gate threshold.

## Recommendation

Keep the manual policy as the safe default. Use ledger history for a weekly report that lists per action type: attempts, human required count, compact survival, and drift after auto repairs. Do not wire the report back into `plan_repairs` without a human approved change and a dedicated adversarial test.

Reproduce the underlying data with `tests/test_ledger_replay.py:1` and `tests/test_contract_forgery.py:1` which exercise the ledger and contract paths that such a report would read.

No production policy change is made. This is a report only.

## The report

The recommendation is implemented as `continuum policy-review` (issue #743), a
read-only aggregation over live and archived recovery history. It groups
repair attempts by `RepairKind` and side effects by action type, and reports
attempts, human-required counts and rates, repeat repairs (the same target
needing the same repair again - drift after an automatic repair),
reconciliation outcomes (effect found / absence confirmed), compaction
survival (how many of each group's events are readable only through the
archive prefix), and human-gate counts:

```bash
# every run in the database...
continuum policy-review
# ...or one run, with machine-readable output for diffing week over week
continuum policy-review run_1 --json
```

Reproducible example - seed a run with a repeated repair, a human-gated step,
and mixed reconciliation outcomes:

```python
from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Run
from continuum.recovery.policy_review import build_policy_review
from continuum.storage import SQLiteStorage

with SQLiteStorage("demo.db") as store:
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RECOVERY_STARTED, {"mode": "repair_and_resume", "plan": [
        {"kind": "revalidate_dependency", "target": "dataset", "requires_human": False},
    ]})
    store.append_event("run_1", EventType.RECOVERY_STARTED, {"mode": "repair_and_resume", "plan": [
        {"kind": "revalidate_dependency", "target": "dataset", "requires_human": False},
    ]})  # repeated: the first repair did not hold
    ledger = ActionLedger(store, "run_1")
    action = ledger.claim("github.create_issue", {"title": "x"}).action
    ledger.reconcile(action.action_id, occurred=True)
    print(build_policy_review(store, "run_1"))
```

Reading the report is the whole feature. A high human-required rate on an
action type means the probes or the workflow for that type deserve
investigation - for example, `POST /payments` gating 90 percent of the time on
probe timeouts is a probe problem, not a reason to lower the gate threshold.
Historical gates cannot prove a gate was unnecessary, only that it happened;
nothing in the report feeds `plan_repairs`, `RecoveryEngine.assess`, or any
threshold, and outcomes the log does not record are reported as `unknown`
rather than inferred.
