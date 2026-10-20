"""The shared compaction bound resolver (issues #1653, #705, #1078).

``resolve_compaction_bound`` is the single place that decides which part of a
run's log is safe to archive. Both storage engines call it, so a check that
lands here is a check both engines get. The guard that matters is the anchor
comparison: an explicit ``through_sequence`` at or above the anchor marker would
delete the marker, and the next append would mint a fresh genesis and fork the
live chain away from the archive (#705, #1078).

These tests run against a real ``SQLiteStorage`` in ``tmp_path``. The function
reaches into ``storage._anchor_environment``, so a mock would skip the part
under test.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.environment.snapshot import StaticProvider, capture
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage
from continuum.storage.compaction import resolve_compaction_bound


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStorage]:
    with SQLiteStorage(os.fspath(tmp_path / "c.db")) as opened:
        yield opened


def seed_run(store: SQLiteStorage, run_id: str = "run_1") -> None:
    """A run with a goal, a started event, and one checkpoint behind it."""
    store.create_run(Run(run_id=run_id, goal="long task"))
    store.append_event(run_id, EventType.RUN_STARTED, {"goal": "long task", "total": 4})
    CheckpointManager(store).checkpoint(run_id)


def test_returns_the_checkpoint_and_a_bound_below_the_anchor(store: SQLiteStorage) -> None:
    seed_run(store)

    version, through = resolve_compaction_bound(store, "run_1", None)

    assert version == store.latest_version("run_1")
    # The marker is appended at head + 1 in the caller's transaction, so the
    # bound has to stay strictly below that or the live log loses its anchor.
    assert through < store.last_sequence("run_1") + 1
    assert through == version.source_sequence


def test_anchors_a_run_that_has_no_checkpoint_yet(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="fresh", goal="long task"))
    store.append_event("fresh", EventType.RUN_STARTED, {"goal": "long task", "total": 4})
    assert store.latest_version("fresh") is None

    version, through = resolve_compaction_bound(store, "fresh", None)

    assert version is not None
    assert through >= 1


def test_reanchors_when_the_checkpoint_is_behind_the_head(store: SQLiteStorage) -> None:
    seed_run(store)

    # A new event after the checkpoint makes the recorded version stale, so the
    # resolver has to take a fresh anchor before it can name a bound.
    store.append_event("run_1", EventType.TASK_UPDATED, {"note": "after the checkpoint"})
    recorded = store.latest_version("run_1")
    assert recorded is not None
    assert recorded.source_sequence < store.last_sequence("run_1")

    version, through = resolve_compaction_bound(store, "run_1", None)

    assert version.source_sequence > recorded.source_sequence
    assert through == version.source_sequence


def test_explicit_bound_below_the_anchor_is_honoured(store: SQLiteStorage) -> None:
    seed_run(store)
    head = store.last_sequence("run_1")

    version, through = resolve_compaction_bound(store, "run_1", head - 1)

    assert through == head - 1
    assert through < store.last_sequence("run_1") + 1
    assert version is not None


def test_bound_at_or_above_the_anchor_is_refused(store: SQLiteStorage) -> None:
    """The regression guard. Deleting the ``>=`` on the comparison must fail this."""
    seed_run(store)

    with pytest.raises(ValueError, match="would archive the anchor marker"):
        resolve_compaction_bound(store, "run_1", 10_000)


def test_the_refused_bound_names_the_anchor_sequence_it_would_have_deleted(
    store: SQLiteStorage,
) -> None:
    seed_run(store)

    with pytest.raises(ValueError) as excinfo:
        resolve_compaction_bound(store, "run_1", 10_000)

    message = str(excinfo.value)
    named = re.search(r"at sequence (\d+)", message)
    assert named is not None, message
    # The marker lands at the head of the log as the caller's transaction runs,
    # so the sequence the refusal names is the head it left behind plus one.
    assert int(named.group(1)) == store.last_sequence("run_1") + 1
    assert "the live log must retain its anchor" in message


def test_a_refused_bound_leaves_the_log_appendable(store: SQLiteStorage) -> None:
    """The refusal must not have consumed the marker it was protecting."""
    seed_run(store)

    with pytest.raises(ValueError, match="would archive the anchor marker"):
        resolve_compaction_bound(store, "run_1", 10_000)

    head = store.last_sequence("run_1")
    store.append_event("run_1", EventType.TASK_UPDATED, {"note": "still appendable"})
    assert store.last_sequence("run_1") == head + 1


def test_a_run_with_no_started_event_cannot_be_anchored(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="headless", goal="long task"))

    with pytest.raises(ValueError, match="could not be anchored"):
        resolve_compaction_bound(store, "headless", None)


def test_an_unknown_run_cannot_be_anchored(store: SQLiteStorage) -> None:
    with pytest.raises(ValueError, match="could not be anchored"):
        resolve_compaction_bound(store, "never_seen", None)


def test_no_projectable_state_after_anchoring_is_refused(
    store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_run(store)
    # The checkpoint lands, but the engine then reports no readable version.
    # Without this the resolver would hand back ``None`` as the version and the
    # caller would fail later, inside the transaction, with a less useful error.
    monkeypatch.setattr(store, "latest_version", lambda run_id: None)

    with pytest.raises(ValueError, match="no projectable state"):
        resolve_compaction_bound(store, "run_1", None)


def test_a_bound_below_one_is_refused(store: SQLiteStorage) -> None:
    seed_run(store)

    with pytest.raises(ValueError, match="nothing to compact: anchor would be empty"):
        resolve_compaction_bound(store, "run_1", 0)


def test_a_caller_supplied_environment_forces_a_fresh_anchor(
    store: SQLiteStorage,
) -> None:
    seed_run(store)
    before = store.last_sequence("run_1")
    environment = capture("run_1", StaticProvider(dataset="v4"))

    version, through = resolve_compaction_bound(store, "run_1", None, environment)

    assert store.last_sequence("run_1") > before
    assert version is not None
    assert through >= 1
    recorded = store.latest_checkpoint("run_1")
    assert recorded is not None
    assert recorded.environment == environment
