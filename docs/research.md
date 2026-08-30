# Research and published results

This page is an index, not a claim. Every number it mentions is printed by a suite that already exists on `main`. Every design it links is a document that already landed on `main`. No new benchmark is invented here and no result is estimated in prose.

For the orientation table that names where CONTINUUM sits, see `README.md` section [Where CONTINUUM sits](../README.md#where-continuum-sits). This page is the backing list that makes that table auditable.

## Published suite outputs (what already prints numbers)

These are the places where a real run already emits measured output. Read the output there, not a paraphrase here.

- **Recovery walkthrough** - `docs/recovery_walkthrough.md`, generated from `examples/recovery_walkthrough.py` (`uv run python examples/recovery_walkthrough.py`). Traces one failure from adapter error to sealed contract: uncertain side effect, dataset moved `v3 -> v4`, `REQUEST_HUMAN`, reconciled by probe, then `REPAIR_AND_RESUME`. Every output block is captured from an actual run.

- **Crash-recovery agent** - `examples/crash_recovery_agent.py` (also `demo-run/worker.py` via `try-it.sh` and `demo-run/generate_crash_visual.py`). Hard-kills a worker with `os._exit(9)` at document 399 mid-batch, writes a real side effect to `demo-run/github-issues.log`, then resumes from the same `demo-run/agent.db`. The regenerable visual is in `docs/assets/crash-recovery.svg` (plain transcript in `docs/assets/crash-recovery.txt`). See the seven-step verdict at the end of the script.

- **CONTINUUM-Bench (minimal harness)** - `references/bench.md` documents the shipped harness in `src/continuum/benchmark/` (command `continuum benchmark`). It runs five scenarios (`process_crash`, `dataset_change`, `unknown_side_effect`, `partial_completion`, `early_crash`) and a dedicated argument-drift scenario against three strategies (`continuum`, `replay`, `naive_checkpoint`). The harness prints `duplicate_work_ratio`, `duplicate_side_effects`, `detected_stale`, `context_tokens`, `compression_ratio`.

- **Recovery-correctness suite (Phase 6)** - `src/continuum/benchmark/phase6/` encodes crash points from the durable-execution survey as executable assertions. It is the 12-scenario suite referenced in `references/bench.md` and `STATUS.md`. Run via `pytest` (see `tests/test_benchmark.py` family).

- **Fault-injection chaos suite** - `benchmarks/fault_injection/` with runner `benchmarks/fault_injection/runner.py` and emitter `benchmarks/fault_injection/emitter.py`. Use `benchmarks/run.py` or the `continuum benchmark` integration to emit the shared envelope `{benchmark, generated_at, summary, results}`. The fault suite publishes `detection_rate`, `unsafe_resume_rate`, `false_positive_rate` (see its `README` and the merged docs for #397).

- **Horizon-scale judge (shared envelope)** - `benchmarks/horizon/` (or the Phase 6 extension described in `src/continuum/benchmark/phase6/harness.py` comments for #398). It reuses the same `BenchmarkReport` / `ScenarioResult` envelope with `benchmark: horizon` and its own metrics. See the coordination comment on issue #399 and the harness docs. No numbers are quoted here.

- **Security extension results** - `docs/RESULTS.md` records the two additive extensions. It marks toy-task and scheduling checks as `PASSED` with ledger traces and leaves mini-benchmarks as `PENDING` by design. `docs/PROBLEM.md` states what each extension does not solve.

- **End-to-end autonomy kit** - `references/e2e.md` and `e2e-autonomy-test/` (issue #6). Scripts a real invoice batch, a hard-kill mid-run, and a fresh resume session, then scores outbox, ledger, and event chain out of band. The README there records the 7/7 mechanics run.

- **MCP compliance** - `references/mcp.md` and `docs/TESTING_MCP.md` plus `scripts/mcp_smoke.py` (real subprocess over stdio, real JSON-RPC). The inspector sequence `docs/TESTING_MCP.md` drives the live server via `@modelcontextprotocol/inspector --cli`.

- **Live-model adapter proofs** - `references/adapters.md` documents the three framework adapters driven against a live OpenRouter model (`examples/*_real_llm_crash.py`), including the hard-crash `os._exit` proof per adapter and the dedup fix it surfaced.

No other numeric claim is made here. If you need a number, run the suite that prints it.

## Design docs already on main (what we are building toward)

These are the design and problem statements that existed before this launch-assets branch. They are linked, not duplicated.

- **North star and migration** - `docs/ARCHITECTURE_EVOLUTION.md` (service model on top of durable execution) and `docs/CONTINUUM_MASTER_PLAN.md` (verified vs believed, phased plan).

- **Upgrade spec** - `docs/UPGRADE_SPEC.md` (six layers for the months-scale durability plane: milestone-anchored plan #312, structured attempt memory #313, instant detection / scoped confirm / token floor, atomic dual-state rewind #292, sleep-time consolidation, prefix trust #401).

- **Live web synthesis** - `docs/research/WEB_SYNTHESIS.md` (2026-08-24 arXiv sweep consolidating the 9 prior research notes, maps gap to shipped module).

- **Gap and analysis notes** - `docs/research/long_horizon_gaps.md`, `docs/research/task_context.md`, `docs/research/policy_learning.md`, `docs/research/instant_detection.md`, `docs/research/confirm_tax.md`, `docs/research/token_floor.md`, `docs/research/human_gate_minimization.md`, `docs/research/cross_agent_portability.md`, `docs/research/auto_resume_architecture.md`, `docs/research/latency_budget.md`.

- **Problem and threat framing** - `docs/PROBLEM.md`, `docs/threat_model.md`, `docs/GLOSSARY.md`.

- **Architecture reference** - `references/architecture.md` (data model, event log, projection, storage, checkpointing, recovery engine), `references/concepts.md`, `references/api.md`, `references/cli.md` (command list, no table duplicated here per #363 fence), `references/install.md`, `references/related-work.md` and `references/citation-audit-2026-08-24.md`.

- **Status and changelog** - `STATUS.md` (verified vs believed, module by module) and `CHANGELOG.md` (Keep a Changelog, `0.1.0` cut).

## How to verify locally

All of these are additive and the suite is the source of truth:

```bash
pytest -q                         # full suite, skips vary by env (see STATUS.md)
ruff check src/ tests/ examples/ && ruff format --check src/ tests/ examples/
mypy src/continuum                # strict, as CI enforces

python examples/recovery_walkthrough.py   # walkthrough, output matches docs/recovery_walkthrough.md
python examples/crash_recovery_agent.py   # hard kill with os._exit(9), refusal then allow
python demo-run/generate_crash_visual.py  # regenerates docs/assets/crash-recovery.svg + .txt

continuum benchmark --total 30 2>&1 | head  # minimal harness sample (see references/bench.md)
```

`demo-run/generate_crash_visual.py` is the regenerable visual: it runs `demo-run/worker.py` until `os._exit(9)` at doc 399, shows the refusal path (`REQUEST_HUMAN`, `safe:false`, non-zero exit), reconciles the uncertain `github.create_issue`, then resumes and finishes. Both paths are shown, not only the happy path. Rerun it to prove the visual regenerates.

## Fences respected

This page does not produce a CLI command table (that is #363), does not add harness hook recipes (that is #396 in `docs/recipes/`), does not invent or reprint benchmark numbers (#397 / #398 own measurement), and does not change the test counts in `README.md` (those stay whatever #316 lands).

## Where this is referenced

- `README.md` Where CONTINUUM sits links here for audit.
- `docs/release-checklist.md` points here for the launch assets it references.
- `docs/release-notes.md` (skeleton, same branch) quotes the suites, not the other way around.
