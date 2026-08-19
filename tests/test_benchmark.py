"""CONTINUUM-Bench: the results must be real, never invented.

These tests run the actual library (storage, checkpointing, validation,
recovery, action ledger) and assert the measured numbers show what the docs
claim: CONTINUUM avoids duplicate work and duplicate side effects where the
naive baselines do not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from continuum import benchmark
from continuum.benchmark import (
    METHODS,
    SCENARIOS,
    IdempotencyResult,
    MethodResult,
    run_benchmark,
    run_idempotency_benchmark,
)
from continuum.storage import SQLiteStorage


def test_recovery_benchmark_covers_every_scenario_and_method() -> None:
    results = run_benchmark(total=20)
    assert len(results) == len(SCENARIOS) * len(METHODS)
    assert {r.method for r in results} == set(METHODS)
    assert {r.scenario for r in results} == set(SCENARIOS)


def test_continuum_recovers_without_duplicate_work() -> None:
    results = {r.scenario: {} for r in run_benchmark(total=40)}  # type: ignore[var-annotated]
    for r in run_benchmark(total=40):
        results[r.scenario][r.method] = r
    for scenario in SCENARIOS:
        cont = results[scenario]["continuum"]
        assert isinstance(cont, MethodResult)
        assert cont.duplicate_work_ratio == 0.0
        assert cont.duplicate_side_effects == 0
        # CONTINUUM must notice a changed environment; naive baselines are blind.
        if SCENARIOS[scenario].env_change:
            assert cont.detected_stale is True


def test_idempotency_benchmark_proves_issue_6() -> None:
    total = 50
    results = {r.method: r for r in run_idempotency_benchmark(total=total)}
    assert set(results) == {
        "continuum_key",
        "continuum_drift",
        "naive_retry",
        "replay",
    }

    # CONTINUUM dedups across argument shape changes (absolute vs relative path).
    for method in ("continuum_key", "continuum_drift"):
        r = results[method]
        assert isinstance(r, IdempotencyResult)
        assert r.attempts == total
        assert r.distinct_side_effects == total
        assert r.duplicate_side_effects == 0

    # Baselines repeat the side effect on every retry.
    for method in ("naive_retry", "replay"):
        r = results[method]
        assert r.attempts == 2 * total
        assert r.duplicate_side_effects == total


@pytest.mark.parametrize(
    "run",
    [
        pytest.param(lambda: run_benchmark(total=4), id="run_benchmark"),
        pytest.param(lambda: run_idempotency_benchmark(total=2), id="run_idempotency"),
    ],
)
def test_benchmark_releases_every_storage_handle(
    run: Callable[[], object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #81: the benchmark must close every database it opens.

    Both harnesses put their databases inside a ``TemporaryDirectory``. A handle
    left open makes that cleanup raise ``PermissionError`` on Windows, which
    took down the whole ``continuum benchmark`` command. This asserts the
    handles are released rather than asserting on the platform error, so it also
    fails on POSIX — where unlinking an open file quietly succeeds — if the
    surrounding ``with`` blocks are ever dropped.
    """
    opened: list[_TrackedStorage] = []

    class _TrackedStorage(SQLiteStorage):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.closed = False
            opened.append(self)

        def close(self) -> None:
            super().close()
            self.closed = True

    monkeypatch.setattr(benchmark, "SQLiteStorage", _TrackedStorage)
    run()

    assert opened, "benchmark opened no database, so this test would prove nothing"
    assert [s for s in opened if not s.closed] == []
