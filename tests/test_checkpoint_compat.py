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
from continuum.models import PROJECTION_BOOKKEEPING, Run, SemanticState
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


# --------------------------------------------------------------------------- #
# Approval.parked (issue #1410 review queue)
#
# The same class of bug as #383, one layer down: parked was added to Approval,
# and both StateCheckpoint.content (integrity_hash) and state_fingerprint hash
# the serialised approvals. Sealed unconditionally, every checkpoint and
# version row written before the review queue existed regains the key as a
# default on load, the recomputed digest grows it back, and the stored hash
# stops matching, which is indistinguishable from tampering.
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_with_approval(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "compat-approval.db")
    storage = SQLiteStorage(path)
    try:
        storage.create_run(Run(run_id="run_1", goal="Deploy the service"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "Deploy the service"})
        # An approval request exactly as pre-#1410 events carried it: no
        # parked key.
        storage.append_event(
            "run_1",
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "approval_1", "subject": "approve deployment"},
        )
        CheckpointManager(storage).checkpoint("run_1")
    finally:
        storage.close()
    yield path


def test_a_checkpoint_written_before_review_queue_still_verifies(db_with_approval: str) -> None:
    """The pre-#1410 approval body hashes identically under the new content().

    Simulating the old writer takes the same two steps as the #383 test: strip
    the field the old model did not have, and re-seal the hash over the reduced
    payload, because that is the digest old code committed. With parked
    serialised unconditionally the reloaded approval regains the key as a
    default and verify() fails: the cross-version mismatch reported in review.
    """
    with sqlite3.connect(db_with_approval) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT rowid, body FROM checkpoints").fetchall()
        body = json.loads(row["body"])
        approvals = body["state"].get("approvals", [])
        assert approvals, "fixture must seed an approval before checkpointing"
        for approval in approvals:
            approval.pop("parked", None)
        body["integrity_hash"] = stable_hash(
            {k: v for k, v in body.items() if k != "integrity_hash"}
        )
        conn.execute(
            "UPDATE checkpoints SET body = ? WHERE rowid = ?",
            (json.dumps(body), row["rowid"]),
        )

    storage = SQLiteStorage(db_with_approval)
    try:
        checkpoint = storage.latest_checkpoint("run_1")
        assert checkpoint is not None
        assert checkpoint.verify(), (
            "a checkpoint sealed before the review queue must not report as tampered"
        )
        assert checkpoint.state.approvals[0].subject == "approve deployment"
    finally:
        storage.close()


def test_a_version_row_written_before_review_queue_still_loads(db_with_approval: str) -> None:
    """Old version rows still verify their fingerprint once parked is skipped.

    put_version stores state_fingerprint(state) beside the canonical body. A
    row written before #1410 holds a fingerprint over approvals the old model
    serialised without parked, so recomputing with the field present raises
    CorruptedRecord on load. The legacy fingerprint below is computed exactly
    as pre-#1410 code did: the same dump state_fingerprint hashes, minus the
    field the old model did not carry.
    """
    with sqlite3.connect(db_with_approval) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT rowid, state FROM versions ORDER BY version").fetchall()
        body = json.loads(row["state"])
        approvals = body.get("approvals", [])
        assert approvals, "fixture must seed an approval before checkpointing"
        for approval in approvals:
            approval.pop("parked", None)
        legacy_dump = SemanticState.model_validate(body).model_dump(
            mode="json",
            exclude={
                "version",
                "created_at",
                "updated_at",
                "source_sequence",
                *PROJECTION_BOOKKEEPING,
            },
        )
        for approval in legacy_dump["approvals"]:
            approval.pop("parked", None)
        conn.execute(
            "UPDATE versions SET state = ?, fingerprint = ? WHERE rowid = ?",
            (json.dumps(body), stable_hash(legacy_dump), row["rowid"]),
        )

    storage = SQLiteStorage(db_with_approval)
    try:
        state = storage.get_version("run_1", 0)
        assert state.approvals[0].subject == "approve deployment"
        assert state.approvals[0].parked is False
    finally:
        storage.close()


def test_new_checkpoints_do_not_persist_unparked_approvals(tmp_path: Path) -> None:
    """Readers built before #1410 validate Approval with extra="forbid".

    A body carrying parked: false would make every database written after the
    review queue unreadable by older builds, so the canonical form omits the
    field while it is at its default, exactly as bookkeeping is omitted.
    """
    path = str(tmp_path / "forward-approval.db")
    storage = SQLiteStorage(path)
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            "run_1",
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "approval_1", "subject": "approve deployment"},
        )
        CheckpointManager(storage).checkpoint("run_1")
    finally:
        storage.close()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT body FROM checkpoints").fetchall()
    approvals = json.loads(row["body"])["state"]["approvals"]
    assert approvals, "fixture must seed an approval before checkpointing"
    for approval in approvals:
        assert "parked" not in approval, "unparked approvals must not persist the field"


def test_parked_approvals_survive_a_store_load_cycle(tmp_path: Path) -> None:
    """A genuinely parked approval keeps its flag through the canonical body.

    Omitting the default is only safe if the set case still round trips,
    otherwise parked queue items would silently lose their deferred status on
    every checkpoint restore.
    """
    path = str(tmp_path / "parked-roundtrip.db")
    storage = SQLiteStorage(path)
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            "run_1",
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "approval_1", "subject": "parked review", "parked": True},
        )
        CheckpointManager(storage).checkpoint("run_1")
        checkpoint = storage.latest_checkpoint("run_1")
        assert checkpoint is not None
        assert checkpoint.verify()
        assert checkpoint.state.approvals[0].parked is True
    finally:
        storage.close()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        (row,) = conn.execute("SELECT body FROM checkpoints").fetchall()
    approvals = json.loads(row["body"])["state"]["approvals"]
    assert approvals[0]["parked"] is True
