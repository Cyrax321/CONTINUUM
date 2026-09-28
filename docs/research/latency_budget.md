# Recovery Latency Budget

Recovery must be fast enough to not dominate the agent loop but thorough enough to be correct. This note derives a budget from measured support points.

## Measured points

- **Source graph build** (`benchmarks/graph_build_overhead.py`): 50 files in 2.2 ms, 100 files in 3.8 ms, 200 files in 9.2 ms. Linear in file count, well below a scheduler quantum.

- **Assessment of 100 dependencies** (`src/continuum/benchmark/phase6/scenarios.py: large_state_recovery_latency`): `engine.assess` completes well under a second and the test asserts under 1s. The validator and planner walk the state once.

- **Full suite** (`uv run pytest`): 1020 tests in about 32 seconds on CI, including hypothesis fuzzing. Per test median is well under 100 ms.

## Budget formula

Propose:

```
budget_ms = 50 + 0.2 * files + 5 * decision_count
```

- `50 ms` base covers transaction open and ledger verify.
- `0.2 ms` per file covers import scanning.
- `5 ms` per decision covers provenance and validation propagation.

For a typical run with 100 files and 10 decisions the budget is `50 + 20 + 50 = 120 ms`. For a large run with 200 files and 100 dependencies the budget is `50 + 40 + 500 = 590 ms`, still under the 1 second assertion in the large state scenario.

## Guidance

- Keep `engine.assess` read only so it can be called often without cost of a write.
- Cache the source graph when it is passed as `source_graph` to `assess_scoped`. The graph is immutable for a given root, so the 0.2 ms per file cost is paid once.
- Budget is a soft SLO for the harness, not a hard timeout. The opt in `run_with_limits` in `src/continuum/recovery/limits.py:1` can enforce a hard cap when a caller needs it.

Reproduce support points with `uv run python benchmarks/graph_build_overhead.py` and `uv run pytest tests/test_phase6.py::test_large_state_recovery_latency -v`.

## Reproducible latency matrix (issue #766)

The support points above are individual measurements. The latency matrix turns
them into a grid CI can watch: it runs `RecoveryEngine.assess_scoped` over the
full cross product of the counts above (50/100/200 files x 10/100 decisions),
derives this note's budget for each point, and classifies the measured median
twice.

**Reproduce locally.** `python -m benchmarks.latency_matrix` runs the grid and
writes `benchmarks/out/latency_matrix_report.{json,md}`. `--list` prints the
points and the tolerance policy without measuring. Each point builds a
deterministic fixture (no network, no clock, no randomness), so a changed
median points at code rather than at the fixture.

**Two classifications, deliberately separate.** A *soft-budget miss* says the
median exceeded the derived budget for its point. It is informational and never
fails CI: the budget is a harness guideline, and a slow but valid assessment is
still a valid answer. A *regression* says the median breached both tolerance
thresholds against the checked-in baseline in
`benchmarks/latency_matrix/baseline.json`, and that is what fails the nightly
job.

**Tolerance policy.** A point is a regression only when its median is both more
than 3x its baseline value and more than 25 ms slower. The ratio is relative and
travels with the runner; the absolute floor stops a sub-millisecond baseline
from tripling into a "regression" that is a few milliseconds of scheduler noise
on a loaded worker. Requiring both is what keeps ordinary machine-to-machine
variability from failing CI. Widen either for a debugging run with
`CONTINUUM_LATENCY_REGRESSION_FACTOR` or `CONTINUUM_LATENCY_ABSOLUTE_FLOOR_MS`.

**Updating the baseline.** The baseline is a committed JSON artifact, not a
value computed at runtime, and a normal run never rewrites it. Expectations
change only through `python -m benchmarks.latency_matrix --update-baseline`,
which regenerates the file so the change lands as a reviewable diff. Fix the
code rather than the baseline when the regression is real; refresh the baseline
and say why in the commit message when the runner or the fixture legitimately
moved.

**Why a soft SLO and not a hard timeout.** Enforcing a cap inside `assess` would
turn a slow but valid assessment into an unsafe incomplete decision, so the
matrix observes and reports instead. A caller that needs a hard cap already has
one in the opt-in `run_with_limits` in `src/continuum/recovery/limits.py`; the
matrix never imposes one.

No external claims are made. The curve is from the repo's own fixtures on the CI runner at this writing.
