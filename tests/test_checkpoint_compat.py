"""Checkpoint and version compatibility across the #383 field addition.

Adding projection bookkeeping (status, unprojectable_*) to SemanticState
changed what StateCheckpoint.content serialises, which is the input to
integrity_hash, so every checkpoint written before #383 failed verification
under the new code: the message says the content does not match its hash,
which is indistinguishable from tampering. These tests exist because no
same-process round-trip can catch that class of bug: the hash always agrees
with itself when one version of the code writes and reads. The only shape
that catches it is pinning the serialised form of one side.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.checkpoint.manager import CheckpointManager
from continuum.events import EventType
from continuum.models import Run
from continuum.security.hashing import stable_hash
from continuum.storage.sqlite import SQLiteStorage

#: Pinned by name, deliberately not imported: these tests freeze the exact
#: serialised shape that crosses the #383 boundary, so they must not move if
#: the constant ever does.
BOOKKEEPING_FIELDS = frozenset(
    {
        "status",
        "unprojectable_at_sequence",
        "unprojectable_event_type",
        "unprojectable_reason",
    }
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "compat.db")
    storage = SQLiteStorage(path)
    try:
        storage.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        storage.append_event(
            "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
        )
        CheckpointManager(storage).checkpoint("run_1")
    finally:
        storage.close()
    yield path


def _strip_bookkeeping(db: str, table: str, json_column: str) -> None:
    """Rewrite stored bodies to the exact shape pre-#383 code wrote."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT rowid, {json_column} AS body FROM {table}").fetchall()
        for row in rows:
            body = json.loads(row["body"])
            state = body.get("state")
            if state is None:
                continue
            for field in BOOKKEEPING_FIELDS:
                state.pop(field, None)
            conn.execute(
                f"UPDATE {table} SET {json_column} = ? WHERE rowid = ?",
                (json.dumps(body), row["rowid"]),
            )


def test_a_checkpoint_written_before_383_still_verifies(db: str) -> None:
    """The pre-#383 body hashes identically under the new content().

    Simulating the old writer faithfully takes two steps: strip the fields the
    old model did not have, and re-seal the hash over that reduced payload,
    because that is exactly the digest origin/main committed. Getting either
    half wrong reports healthy old checkpoints as tampered, spending the one
    alarm this hash exists to mean. Under the first cut of #383 (fields added,
    not excluded from content()) the reloaded state regains the four keys as
    defaults, the recomputed payload grows them back, and verify() fails: the
    exact cross-version mismatch reported in review.
    """
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT rowid, body FROM checkpoints").fetchall()
        body = json.loads(row["body"])
        for field in BOOKKEEPING_FIELDS:
            body["state"].pop(field, None)
        # Re-seal exactly as the old writer did: digest over everything but
        # the hash, written back into the body beside the content.
        body["integrity_hash"] = stable_hash(
            {k: v for k, v in body.items() if k != "integrity_hash"}
        )
        conn.execute(
            "UPDATE checkpoints SET body = ? WHERE rowid = ?",
            (json.dumps(body), row["rowid"]),
        )

    storage = SQLiteStorage(db)
    try:
        checkpoint = storage.latest_checkpoint("run_1")
        assert checkpoint is not None
        assert checkpoint.verify(), (
            "a checkpoint written by shipped code must not report as tampered"
        )
        restored = CheckpointManager(storage).restore("run_1")
        assert restored.state.goal.description == "Analyze 100 documents"
    finally:
        storage.close()


def test_a_version_row_written_before_383_still_loads(db: str) -> None:
    """Old version rows load with defaults filling the new fields."""
    _strip_bookkeeping(db, "versions", "state")

    storage = SQLiteStorage(db)
    try:
        state = storage.get_version("run_1", 0)
        assert state.status.value == "valid"
        assert state.unprojectable_at_sequence is None
        assert state.goal.description == "Analyze 100 documents"
    finally:
        storage.close()


def test_new_checkpoints_do_not_persist_projection_bookkeeping(tmp_path: Path) -> None:
    """Readers built before #383 validate SemanticState with extra="forbid".

    A body carrying fields they have never heard of would make every database
    written after the upgrade unreadable by older builds, so the canonical
    form omits them entirely.
    """
    path = str(tmp_path / "forward.db")
    storage = SQLiteStorage(path)
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        CheckpointManager(storage).checkpoint("run_1")
    finally:
        storage.close()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT body FROM checkpoints").fetchall()
    state_body = json.loads(row["body"])["state"]
    for field in BOOKKEEPING_FIELDS:
        assert field not in state_body, f"{field} must stay out of the persisted body"


def test_new_version_rows_do_not_persist_projection_bookkeeping(tmp_path: Path) -> None:
    path = str(tmp_path / "forward-versions.db")
    storage = SQLiteStorage(path)
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 10})
        CheckpointManager(storage).checkpoint("run_1")
    finally:
        storage.close()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT state FROM versions").fetchall()
    state_body = json.loads(row["state"])
    for field in BOOKKEEPING_FIELDS:
        assert field not in state_body, f"{field} must stay out of the persisted body"
