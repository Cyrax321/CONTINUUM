## CONTINUUM-Bench

A recovery benchmark for long-running agents under controlled failures. The
minimal harness now ships: `continuum benchmark` runs real scenarios against the
actual library (storage, checkpointing, validation, action ledger, recovery)
with an in-process simulated agent. Nothing is mocked and no result is invented.

### Status

Implemented (minimal harness):

- Module: `src/continuum/benchmark/__init__.py`
- Command: `continuum benchmark [--total N] [--json]` (default total 200)
- Tests: `tests/test_benchmark.py` asserts that continuum resumes with 0
  duplicate work, detects the dataset change, and that full replay wastes work

Remaining Phase 12 goals (see STATUS.md):

- Expand from the 5 shipped scenarios to the full scenario suite below.
- Publish baselines from real agent runs, not just the simulated harness.
- Add a dashboard view of results.

### Implemented scenarios

| Scenario             | What breaks                                       |
|:---------------------|:--------------------------------------------------|
| process_crash        | The agent dies mid-run; measures duplicate work   |
| dataset_change       | The environment version changes while the agent is down |
| unknown_side_effect  | An external side effect is interrupted mid-flight |
| partial_completion   | Task mostly finished before the crash (late crash) |
| early_crash          | Agent crashes almost immediately; full replay wastes the most work |

### Implemented methods (baselines)

- `continuum`        - semantic checkpoint + environment revalidation + ledger
- `replay`           - full transcript replay from scratch (the waste case)
- `naive_checkpoint` - resume from the saved progress count, no validation

### Metrics

| Metric                       | Definition                                     |
|:-----------------------------|:-----------------------------------------------|
| duplicate_work_ratio         | Previously completed work repeated after recovery |
| duplicate_side_effects       | External actions accidentally repeated          |
| detected_stale               | Whether the method noticed the environment changed |
| context_tokens               | Size of the briefing the agent needs to resume |
| compression_ratio            | full log tokens / resume briefing tokens       |
| elapsed_seconds              | Time to run the (scenario, method) measurement |

The harness currently emits the six metrics above. The original Phase 12 issue
(#9) asked for a broader measurement contract; coverage is partial. Remaining
measurements such as recovery correctness, recovery latency, validation
accuracy, and cost are tracked under the remaining Phase 12 goals and are not
yet emitted by the harness.

### Target scenario suite (remaining not yet implemented)

The original spec called for ten controlled-failure scenarios. Five are now
shipped (see Implemented scenarios above: `process_crash`, `dataset_change`,
`unknown_side_effect`, `partial_completion`, `early_crash`). The remaining
seven are:

| Scenario             | Description                           |
|:---------------------|:--------------------------------------|
| Context compaction   | Context window overflow               |
| Tool failure         | External tool becomes unavailable     |
| API timeout          | Session expires mid-task              |
| File modification    | Working files modified externally     |
| Permission change    | Access revoked during execution       |
| Model switch         | LLM provider changes mid-task        |
| Stale decision       | Previously valid decision invalidated |

### Sample results

From `continuum benchmark --total 30` (illustrative, not the published baseline):

    scenario           method              dup_work  dup_side  stale  ctx_tok  compress
    process_crash      continuum              0.000         0  False       57     14.16
    process_crash      replay                 0.500         1  False      778       1.0
    process_crash      naive_checkpoint       0.000         0  False        4      None
    dataset_change     continuum              0.000         0   True       57     14.16
    dataset_change     replay                 0.500         1  False      778       1.0
    dataset_change     naive_checkpoint       0.000         0  False        4      None
    unknown_side_effect continuum              0.000         0  False       57     13.86
    unknown_side_effect replay                 0.233         1  False      516       1.0
    unknown_side_effect naive_checkpoint       0.000         0  False        3      None
    partial_completion continuum              0.000         0  False       57     14.16
    partial_completion replay                 0.800         1  False      778       1.0
    partial_completion naive_checkpoint       0.000         0  False        4      None
    early_crash       continuum              0.000         0  False       57     14.16
    early_crash       replay                 0.200         1  False      778       1.0
    early_crash       naive_checkpoint       0.000         0  False        4      None

Reading: continuum resumes with no duplicate work and exactly one side effect,
and is the only method that detects the dataset change. Full replay reprocesses
everything (wasteful but ends correct). Naive checkpoint is efficient but blind.
Full-replay waste also scales with crash timing: `early_crash` replays the least
and `partial_completion` the most, while continuum stays at zero duplicate work
in every scenario.

<!-- BENCH:START -->
### Latest nightly results (real runs, no invented numbers)

Generated: 2026-09-12T16:26:03.939958  Horizon scenarios: 5  Passed: 3  Failed: 2

Accuracy: 0.6  Unnecessary escalation: 0.2  Repair precision: 0.8  Duplicate side effects: 0  Duplicate work: 0.0  Compression: 0.139

| Scenario | Cycles | Years | Correct | Actual | Accuracy |
| --- | --- | --- | --- | --- | --- |
| horizon_steady_progress_year | 141 | 2.3 | resume | resume | 1.0 |
| horizon_quarterly_drift_year | 141 | 2.3 | repair | request_human | 0.0 |
| horizon_budget_exhaustion_year | 141 | 2.3 | request_human | request_human | 1.0 |
| horizon_compaction_stress_year | 177 | 2.87 | resume | resume | 1.0 |
| horizon_abort_condition_year | 141 | 2.3 | abort | request_human | 0.0 |

Fault-injection: 7 scenarios, detection 0, unsafe 0
<!-- BENCH:END -->
