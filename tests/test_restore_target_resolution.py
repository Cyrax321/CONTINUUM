"""The target-resolution and input-validation branches of approve_restore (#1292).

``test_restore_merge_gate.py`` drives restore through one entry point only:
``approve_restore(..., anchor_sequence=0)``. Every branch that resolves a
*target* -- and every input check that guards that resolution -- was uncovered,
so a change to any of them would ship green. This suite covers them directly.

Falsifiable: each branch below was reached by hand first and its error message
recorded in #1292; the asserts match that recorded behaviour rather than a
guess, so a message change fails the test instead of silently redefining what
an operator sees.
"""

from __future__ import annotations

import pytest

from continuum.events import EventType
from continuum.models import Goal, Run, SemanticState, StateCheckpoint
from continuum.recovery.restore import _anchor_for, approve_restore
from continuum.storage import SQLiteStorage

RUN_ID = "r1"


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=RUN_ID, goal="g"))
    storage.append_event(RUN_ID, EventType.RUN_STARTED, {"goal": "g"})
    return storage


def _state(sequence: int, **overrides: object) -> SemanticState:
    base: dict[str, object] = {
        "run_id": RUN_ID,
        "goal": Goal(description="g"),
        "source_sequence": sequence,
    }
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


def _put_checkpoint(storage: SQLiteStorage, *, version: int, sequence: int) -> str:
    """Write a checkpoint at ``version`` whose state consumed ``sequence``."""

    checkpoint = storage.put_checkpoint(
        StateCheckpoint(run_id=RUN_ID, version=version, state=_state(sequence))
    )
    return checkpoint.checkpoint_id


def _restored_anchor(storage: SQLiteStorage) -> int:
    """The anchor the last RUN_RESTORED event recorded."""
    events = storage.read_events(RUN_ID)
    restored = next(e for e in events if e.type is EventType.RUN_RESTORED)
    return int(restored.payload["anchor_sequence"])


# --- input validation: the two guards that run before any resolution -------- #


def test_passing_both_target_and_anchor_is_refused() -> None:
    """Two anchors is ambiguous, so it is an error rather than a preference."""
    storage = _storage()
    with pytest.raises(ValueError, match="exactly one of target or anchor_sequence"):
        approve_restore(
            storage,
            RUN_ID,
            reason="rollback",
            target="cp_1",
            anchor_sequence=0,
        )


@pytest.mark.parametrize("reason", ["", "   ", "\t"])
def test_a_blank_reason_is_refused(reason: str) -> None:
    """The reason is the audit trail; a whitespace-only one carries no audit."""
    storage = _storage()
    with pytest.raises(ValueError, match="a restore needs a stated reason"):
        approve_restore(storage, RUN_ID, reason=reason, anchor_sequence=0)


# --- target=None: resolve to the latest checkpoint ------------------------- #


def test_no_target_and_no_checkpoints_anchors_at_zero() -> None:
    """A run with nothing checkpointed restores from the start, not an error."""
    storage = _storage()
    approve_restore(storage, RUN_ID, reason="rollback")
    assert _restored_anchor(storage) == 0


def test_no_target_resolves_to_the_latest_checkpoint() -> None:
    """No target anchors on ``latest_checkpoint`` and returns its *sequence*.

    ``latest_checkpoint`` is the highest version, and the anchor is that
    checkpoint's ``source_sequence`` -- the log position to replay from -- not
    the version itself. Written out of order so the highest version is not the
    highest sequence: a resolver that returned the version would report 3.
    """
    storage = _storage()
    _put_checkpoint(storage, version=1, sequence=5)
    _put_checkpoint(storage, version=3, sequence=1)
    approve_restore(storage, RUN_ID, reason="rollback")
    assert _restored_anchor(storage) == 1


# --- target as an id or a number: the fall-through ladder ------------------- #


def test_target_as_checkpoint_id_resolves() -> None:
    storage = _storage()
    checkpoint_id = _put_checkpoint(storage, version=1, sequence=4)
    approve_restore(storage, RUN_ID, reason="rollback", target=checkpoint_id)
    assert _restored_anchor(storage) == 4


def test_target_as_version_resolves() -> None:
    storage = _storage()
    _put_checkpoint(storage, version=7, sequence=4)
    approve_restore(storage, RUN_ID, reason="rollback", target=7)
    assert _restored_anchor(storage) == 4


def test_target_as_source_sequence_resolves() -> None:
    storage = _storage()
    _put_checkpoint(storage, version=1, sequence=9)
    approve_restore(storage, RUN_ID, reason="rollback", target=9)
    assert _restored_anchor(storage) == 9


def test_target_as_a_numeric_string_falls_through_to_version() -> None:
    """A bare number is not a checkpoint id, so it must reach the number rungs."""
    storage = _storage()
    _put_checkpoint(storage, version=7, sequence=4)
    approve_restore(storage, RUN_ID, reason="rollback", target="7")
    assert _restored_anchor(storage) == 4


# --- the exported precondition helper ------------------------------------- #


def test_restore_to_anchor_checks_preconditions_for_a_restore() -> None:
    """``restore_to_anchor`` is the public precondition-only entry point.

    It is exported in ``__all__`` but called from neither the CLI nor the MCP
    server, so nothing else exercised it.
    """
    from continuum.recovery.restore import restore_to_anchor

    storage = _storage()
    derivation, carry, summary = restore_to_anchor(storage, RUN_ID, 0, reason="rollback")
    # An empty prefix crosses nothing outstanding; safety is the caller's call.
    assert derivation.unsettled_authorizations == frozenset()
    assert derivation.depended_results == frozenset()
    assert derivation.uncertain_slots == frozenset()
    # ``summary`` is the JSON-native projection of ``derivation``: the same
    # sets, serialised as lists for the event payload.
    assert summary == derivation.model_dump(mode="json")
    assert carry == set()


# --- the three miss shapes a target can take ------------------------------- #


def test_an_unknown_string_target_reports_the_target_and_the_run() -> None:
    storage = _storage()
    with pytest.raises(ValueError, match="no checkpoint 'nope' for run 'r1'"):
        approve_restore(storage, RUN_ID, reason="rollback", target="nope")


@pytest.mark.parametrize("target", ["", "   ", "\t"])
def test_a_blank_target_is_refused_before_any_lookup(target: str) -> None:
    """Blank would otherwise become 0 and silently restore from the start."""
    storage = _storage()
    with pytest.raises(ValueError, match="restore target must be non-empty"):
        approve_restore(storage, RUN_ID, reason="rollback", target=target)


def test_an_unknown_numeric_target_reports_the_number_it_tried() -> None:
    storage = _storage()
    _put_checkpoint(storage, version=1, sequence=1)
    with pytest.raises(
        ValueError, match="no checkpoint with version/source_sequence 999 for run 'r1'"
    ):
        approve_restore(storage, RUN_ID, reason="rollback", target=999)


def test_a_checkpoint_belonging_to_another_run_is_not_a_valid_target() -> None:
    """The id rung matches on run id, so a foreign checkpoint is a miss.

    Written into the same store on purpose: in a separate database the lookup
    would miss outright and never reach the ``run_id`` comparison this asserts.
    """
    storage = _storage()
    storage.create_run(Run(run_id="other", goal="g"))

    foreign = storage.put_checkpoint(
        StateCheckpoint(run_id="other", version=1, state=_state(1, run_id="other"))
    )
    with pytest.raises(ValueError, match="no checkpoint"):
        _anchor_for(storage, RUN_ID, foreign.checkpoint_id)
