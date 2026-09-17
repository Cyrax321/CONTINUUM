"""A corrupted checkpoint is reported as corrupted, not as missing (#1059).

Both checkpoint resolvers used to wrap ``storage.get_checkpoint`` in a bare
``except Exception: pass``, so a record that failed its integrity check was
silently retried as a version number and finally reported as a lookup miss.
The tamper-evidence the storage layer raises is the one signal an operator
most needs, and it was the signal both resolvers converted into noise.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.checkpoint.rewind import RewindError, resolve_checkpoint
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import EnvResource, Run
from continuum.recovery.restore import _anchor_for
from continuum.storage import SQLiteStorage
from continuum.storage.base import CorruptedRecord


def _corrupt_body(db: Path, checkpoint_id: str) -> None:
    """Rewrite a checkpoint body so its sealed hash no longer matches."""
    conn = sqlite3.connect(db)
    try:
        body = conn.execute(
            "SELECT body FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()[0]
        obj = json.loads(body)
        obj["state"]["goal"]["description"] = "TAMPERED GOAL"
        conn.execute(
            "UPDATE checkpoints SET body = ? WHERE checkpoint_id = ?",
            (json.dumps(obj), checkpoint_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_run(tmp_path: Path) -> tuple[SQLiteStorage, str]:
    db = tmp_path / "corrupt.db"
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"})
    storage.append_event(
        "r", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v1"}
    )
    env = capture(
        "r", StaticProvider(resources={"dataset": EnvResource(name="dataset", version="v1")})
    )
    checkpoint_id = CheckpointManager(storage).checkpoint("r", environment=env).checkpoint_id
    storage.close()
    return SQLiteStorage(db), checkpoint_id


def test_storage_flags_the_tampered_checkpoint(tmp_path: Path) -> None:
    storage, checkpoint_id = _seed_run(tmp_path)
    _corrupt_body(tmp_path / "corrupt.db", checkpoint_id)

    with pytest.raises(CorruptedRecord, match="integrity hash does not match"):
        storage.get_checkpoint(checkpoint_id)


def test_rewind_resolver_reports_corruption_instead_of_a_missing_checkpoint(
    tmp_path: Path,
) -> None:
    storage, checkpoint_id = _seed_run(tmp_path)
    _corrupt_body(tmp_path / "corrupt.db", checkpoint_id)

    with pytest.raises(RewindError) as excinfo:
        resolve_checkpoint(storage, "r", checkpoint_id)

    message = str(excinfo.value)
    assert checkpoint_id in message
    assert "corrupted" in message
    # The old message pointed at a typo or a missing version; it must not anymore.
    assert not message.startswith("no checkpoint")


def test_restore_resolver_reports_corruption_instead_of_a_missing_checkpoint(
    tmp_path: Path,
) -> None:
    storage, checkpoint_id = _seed_run(tmp_path)
    _corrupt_body(tmp_path / "corrupt.db", checkpoint_id)

    with pytest.raises(ValueError) as excinfo:
        _anchor_for(storage, "r", checkpoint_id)

    message = str(excinfo.value)
    assert checkpoint_id in message
    assert "corrupted" in message
    assert not message.startswith("no checkpoint")


def test_a_genuinely_missing_checkpoint_is_still_reported_as_missing(tmp_path: Path) -> None:
    storage, _ = _seed_run(tmp_path)

    with pytest.raises(RewindError, match="no checkpoint 'checkpoint_does_not_exist'"):
        resolve_checkpoint(storage, "r", "checkpoint_does_not_exist")
    with pytest.raises(ValueError, match="no checkpoint 'checkpoint_does_not_exist'"):
        _anchor_for(storage, "r", "checkpoint_does_not_exist")


def test_version_and_sequence_lookup_still_fall_through(tmp_path: Path) -> None:
    """An id-shaped miss must still resolve by version or source sequence."""
    storage, checkpoint_id = _seed_run(tmp_path)
    target = storage.get_checkpoint(checkpoint_id)
    expected = target.state.source_sequence

    # A bare integer is not a checkpoint id, so both resolvers must fall through
    # to the version and source-sequence strategies rather than reporting a miss.
    assert resolve_checkpoint(storage, "r", str(target.version)).checkpoint_id == checkpoint_id
    assert resolve_checkpoint(storage, "r", str(expected)).checkpoint_id == checkpoint_id
    assert _anchor_for(storage, "r", str(expected)) == expected
