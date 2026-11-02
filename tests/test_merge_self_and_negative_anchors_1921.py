"""Tests for issue #1921: refuse self-merges and negative anchor sequences in approve_merge."""

from __future__ import annotations

import pytest

from continuum.models import Run
from continuum.recovery.merge import approve_merge
from continuum.recovery.restore import approve_restore
from continuum.storage import SQLiteStorage


def test_approve_merge_refuses_self_merge() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))

    with pytest.raises(ValueError, match="cannot merge a run into itself"):
        approve_merge(storage, "run_1", source_run_id="run_1", reason="self merge")


def test_approve_merge_refuses_negative_anchor_sequence() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))

    with pytest.raises(ValueError, match="anchor_sequence must be non-negative"):
        approve_merge(storage, "run_1", anchor_sequence=-1, reason="negative anchor")


def test_approve_merge_refuses_negative_source_anchor_sequence() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="target"))
    storage.create_run(Run(run_id="run_2", goal="source"))

    with pytest.raises(ValueError, match="source_anchor_sequence must be non-negative"):
        approve_merge(
            storage,
            "run_1",
            source_run_id="run_2",
            source_anchor_sequence=-5,
            reason="negative source anchor",
        )


def test_approve_restore_refuses_negative_anchor_sequence() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test"))

    with pytest.raises(ValueError, match="anchor_sequence must be non-negative"):
        approve_restore(storage, "run_1", anchor_sequence=-1, reason="negative anchor")
