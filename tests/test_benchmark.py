from __future__ import annotations

from continuum.benchmark import METHODS, SCENARIOS, render, run_benchmark


def test_benchmark_runs_every_combination() -> None:
    results = run_benchmark(total=20)
    assert len(results) == len(SCENARIOS) * len(METHODS)
    by = {(r.scenario, r.method): r for r in results}

    # CONTINUUM resumes without duplicate work and keeps exactly one side effect.
    for scen in SCENARIOS:
        c = by[(scen, "continuum")]
        assert c.duplicate_work_ratio == 0.0
        assert c.side_effects_created == 1
        assert c.duplicate_side_effects == 0

    # Full transcript replay wastes work and duplicates the side effect.
    replay = by[("process_crash", "replay")]
    assert replay.duplicate_work_ratio > 0.0
    assert replay.side_effects_created == 2

    # CONTINUUM detects the dataset change; naive checkpointing does not.
    assert by[("dataset_change", "continuum")].detected_stale is True
    assert by[("dataset_change", "naive_checkpoint")].detected_stale is False

    # The recovery briefing is a small fraction of the full event log.
    assert by[("process_crash", "continuum")].compression_ratio is not None
    assert by[("process_crash", "continuum")].compression_ratio > 1.0

    # Crash timing changes how much full replay wastes, while continuum stays clean.
    assert by[("partial_completion", "continuum")].duplicate_work_ratio == 0.0
    assert by[("early_crash", "continuum")].duplicate_work_ratio == 0.0
    assert (
        by[("early_crash", "replay")].duplicate_work_ratio
        < by[("process_crash", "replay")].duplicate_work_ratio
    )
    assert (
        by[("partial_completion", "replay")].duplicate_work_ratio
        > by[("process_crash", "replay")].duplicate_work_ratio
    )


def test_benchmark_results_are_json_serialisable() -> None:
    import json

    results = run_benchmark(total=10)
    text = render(results)
    assert "continuum" in text
    payload = json.dumps([r.as_dict() for r in results])
    assert json.loads(payload) == [r.as_dict() for r in results]


def test_crash_timing_scenarios_recover_cleanly() -> None:
    results = run_benchmark(total=20)
    by = {(r.scenario, r.method): r for r in results}
    for scen in ("partial_completion", "early_crash"):
        cont = by[(scen, "continuum")]
        assert cont.duplicate_work_ratio == 0.0
        assert cont.side_effects_created == 1
        assert cont.detected_stale is False
