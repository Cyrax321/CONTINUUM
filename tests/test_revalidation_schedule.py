"""Tests for the periodic revalidation scheduler (Extension 2)."""

from __future__ import annotations

import pytest

from continuum.checkpoint.manager import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import RecoveryMode, Run
from continuum.security.revalidation import (
    RevalidationPolicy,
    RevalidationTrigger,
    maybe_revalidate,
)
from continuum.storage.sqlite import SQLiteStorage


@pytest.fixture
def store() -> SQLiteStorage:
    s = SQLiteStorage(":memory:")
    s.create_run(Run(run_id="run_1", goal="g"))
    s.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield s
    s.close()


def env(dataset: str = "v3"):
    return capture("run_1", StaticProvider(dataset=dataset))


def seed(s: SQLiteStorage, dataset: str = "v3") -> None:
    s.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": dataset}
    )
    s.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "e1", "summary": "s", "source": "dataset"},
    )
    s.append_event(
        "run_1", EventType.FINDING_ADDED, {"finding_id": "f1", "evidence": ["e1"], "claim": "c"}
    )
    s.append_event("run_1", EventType.WORK_COMPLETED, {"count": 1})
    # Record the environment at this checkpoint so a later assess with the same
    # environment is recognized as intact rather than merely unknown.
    CheckpointManager(s).checkpoint("run_1", environment=env(dataset))


def test_policy_defaults() -> None:
    p = RevalidationPolicy()
    assert p.step_interval == 25
    assert p.revalidate_on_app_switch is True


def test_fires_on_app_switch() -> None:
    result = maybe_revalidate(0, True, RevalidationPolicy())
    assert result is not None
    assert result.trigger is RevalidationTrigger.APP_SWITCH
    assert result.decision is None


def test_fires_on_step_interval() -> None:
    result = maybe_revalidate(25, False, RevalidationPolicy())
    assert result is not None
    assert result.trigger is RevalidationTrigger.STEP_INTERVAL


def test_no_fire_between_intervals() -> None:
    assert maybe_revalidate(24, False, RevalidationPolicy()) is None
    assert maybe_revalidate(0, False, RevalidationPolicy()) is None


def test_step_interval_catches_mid_run_drift(store: SQLiteStorage) -> None:
    seed(store)
    result = maybe_revalidate(
        25, False, RevalidationPolicy(), storage=store, run_id="run_1", env=env("v4")
    )
    assert result is not None
    assert result.trigger is RevalidationTrigger.STEP_INTERVAL
    # Drift is detected within this single cycle, not only at the next crash.
    assert result.decision is not None
    assert result.decision.mode is not RecoveryMode.RESUME


def test_app_switch_catches_mid_run_drift(store: SQLiteStorage) -> None:
    seed(store)
    result = maybe_revalidate(
        0, True, RevalidationPolicy(), storage=store, run_id="run_1", env=env("v4")
    )
    assert result is not None
    assert result.trigger is RevalidationTrigger.APP_SWITCH
    assert result.decision.mode is not RecoveryMode.RESUME


def test_no_false_positive_on_intact_environment(store: SQLiteStorage) -> None:
    seed(store)
    result = maybe_revalidate(
        25, False, RevalidationPolicy(), storage=store, run_id="run_1", env=env("v3")
    )
    assert result is not None
    assert result.decision.mode is RecoveryMode.RESUME
