"""Restore and rewind must resolve a numeric target to the same anchor.

``checkpoint.rewind.resolve_checkpoint`` documents the precedence id > version
> source_sequence and implements it with two passes. ``restore._anchor_for``
used to fold version and source_sequence into one ``or`` loop, so when a
numeric target was one checkpoint's ``source_sequence`` and an earlier-listed
checkpoint's version, the two rollback paths disagreed and ``restore`` could
discard a larger span of history than ``rewind`` for the identical input.
"""

from __future__ import annotations

import pytest

from continuum.checkpoint.rewind import resolve_checkpoint
from continuum.events import EventType
from continuum.models import Goal, Run, SemanticState, StateCheckpoint
from continuum.recovery.restore import _anchor_for
from continuum.storage import SQLiteStorage

RUN_ID = "r1"


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=RUN_ID, goal="g"))
    storage.append_event(RUN_ID, EventType.RUN_STARTED, {"goal": "g"})
    return storage


def _state(sequence: int) -> SemanticState:
    return SemanticState(run_id=RUN_ID, goal=Goal(description="g"), source_sequence=sequence)


def _put(storage: SQLiteStorage, *, version: int, sequence: int) -> None:
    storage.put_checkpoint(StateCheckpoint(run_id=RUN_ID, version=version, state=_state(sequence)))


def test_numeric_target_prefers_version_like_rewind() -> None:
    """A number that is one checkpoint's source_sequence and another's version
    resolves to the version match, and restore agrees with rewind."""
    storage = _storage()
    # Written source_sequence-first so the version match is the later row: a
    # single `or` loop would return the earlier source_sequence match instead.
    _put(storage, version=5, sequence=10)
    _put(storage, version=10, sequence=20)

    rewind_cp = resolve_checkpoint(storage, RUN_ID, "10")
    assert rewind_cp.version == 10
    assert rewind_cp.state.source_sequence == 20

    assert _anchor_for(storage, RUN_ID, 10) == 20
    assert _anchor_for(storage, RUN_ID, "10") == 20


def test_source_sequence_only_target_still_resolves() -> None:
    """When no checkpoint has that version, the source_sequence rung wins."""
    storage = _storage()
    _put(storage, version=1, sequence=9)

    assert _anchor_for(storage, RUN_ID, 9) == 9
    assert _anchor_for(storage, RUN_ID, "9") == 9


def test_version_target_without_collision_resolves_to_its_sequence() -> None:
    storage = _storage()
    _put(storage, version=7, sequence=4)

    assert _anchor_for(storage, RUN_ID, 7) == 4
    assert _anchor_for(storage, RUN_ID, "7") == 4


def test_unknown_int_target_reports_version_and_source_sequence() -> None:
    """An int matching neither field exhausts both passes and is reported."""
    storage = _storage()
    _put(storage, version=1, sequence=2)

    with pytest.raises(ValueError, match="version/source_sequence 999"):
        _anchor_for(storage, RUN_ID, 999)


def test_unknown_numeric_string_target_is_reported() -> None:
    """A numeric string matching neither field falls through to the miss."""
    storage = _storage()
    _put(storage, version=1, sequence=2)

    with pytest.raises(ValueError, match="no checkpoint '999'"):
        _anchor_for(storage, RUN_ID, "999")
