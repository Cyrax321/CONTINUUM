"""Tests for post-compaction constraint re-injection and verification hook (issue #1413)."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.cli import main
from continuum.events import EventType
from continuum.models import ConstraintPinned, Origin, Run
from continuum.security.constraints import (
    load_constraints,
    reinject_constraint_pins,
)
from continuum.storage import SQLiteStorage


def _write_constraints(path: Path, constraints: list[dict[str, object]]) -> Path:
    target = path / ".continuum" / "constraints.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"constraints": constraints}), encoding="utf-8")
    return target


def test_post_compaction_constraint_reinjection_with_registry(tmp_path: Path) -> None:
    """Active constraints are verified and re-injected into live tail across compaction."""
    constraints_file = _write_constraints(
        tmp_path,
        [
            {
                "id": "no-direct-db-writes",
                "level": "hard",
                "predicate": "The agent must not write to the primary database directly.",
                "scope": ["db.write"],
            },
            {
                "id": "prefer-cache",
                "level": "soft",
                "predicate": "Prefer cache over recomputing.",
            },
        ],
    )
    reg = load_constraints(constraints_file, asserted_by=Origin.HUMAN)
    expected_digest = reg.digest
    pred_hash = hashlib.sha256(
        b"The agent must not write to the primary database directly."
    ).hexdigest()

    db_path = str(tmp_path / "test.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_compaction_pins"

    storage.create_run(Run(run_id=run_id, goal="test compaction pin re-injection"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="no-direct-db-writes", sha256=pred_hash).model_dump(),
        source=Origin.HUMAN,
    )

    manager = CheckpointManager(storage)
    manager.checkpoint(run_id)

    # Perform compaction with constraints_path
    report = storage.compact_run(run_id, constraints_path=constraints_file)
    assert report["archived"] >= 1

    live_events = storage.read_events(run_id)
    types = [e.type for e in live_events]

    assert EventType.EVENT_LOG_ANCHORED in types
    assert EventType.CONSTRAINT_PINNED in types
    assert EventType.CONSTRAINT_PINS_VERIFIED in types

    verified_ev = next(e for e in live_events if e.type is EventType.CONSTRAINT_PINS_VERIFIED)
    assert verified_ev.payload["verified"] is True
    assert verified_ev.payload["digest"] == expected_digest
    assert verified_ev.payload["count"] == 2

    # Projecting with full history preserves both active pins and live tail has them
    live_pinned = {
        e.payload["constraint_id"]: e.payload["sha256"]
        for e in live_events
        if e.type is EventType.CONSTRAINT_PINNED
    }
    assert "no-direct-db-writes" in live_pinned
    assert "prefer-cache" in live_pinned

    state = CheckpointManager(storage).project_current(run_id, full_history=True)
    assert "no-direct-db-writes" in state.pins
    assert "prefer-cache" in state.pins
    assert state.pins["no-direct-db-writes"].sha256 == pred_hash


def test_post_compaction_constraint_dropped_on_hash_mismatch(tmp_path: Path) -> None:
    """A tampered or mismatched constraint pin is dropped and re-pinned with valid digest."""
    constraints_file = _write_constraints(
        tmp_path,
        [
            {
                "id": "strict-security",
                "level": "hard",
                "predicate": "Legitimate operator security policy.",
            }
        ],
    )
    db_path = str(tmp_path / "mismatch.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_mismatch"

    storage.create_run(Run(run_id=run_id, goal="test mismatch"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    # Tampered sha256
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "strict-security", "sha256": "0" * 64},
        source=Origin.HUMAN,
    )

    storage.compact_run(run_id, constraints_path=constraints_file)

    live_events = storage.read_events(run_id)
    types = [e.type for e in live_events]

    assert EventType.CONSTRAINT_PIN_DROPPED in types
    dropped_ev = next(e for e in live_events if e.type is EventType.CONSTRAINT_PIN_DROPPED)
    assert dropped_ev.payload["constraint_id"] == "strict-security"
    assert dropped_ev.payload["reason"] == "digest_mismatch"

    # The legitimate constraint from operator registry was re-pinned
    legit_hash = hashlib.sha256(b"Legitimate operator security policy.").hexdigest()
    state = CheckpointManager(storage).project_current(run_id, full_history=True)
    assert state.pins["strict-security"].sha256 == legit_hash


def test_post_compaction_constraint_dropped_when_removed_from_registry(
    tmp_path: Path,
) -> None:
    """A constraint removed from the operator registry is dropped during compaction."""
    constraints_file = _write_constraints(
        tmp_path,
        [
            {
                "id": "retained-pin",
                "level": "hard",
                "predicate": "This pin remains active.",
            }
        ],
    )
    db_path = str(tmp_path / "removed.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_removed"

    storage.create_run(Run(run_id=run_id, goal="test removed"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "obsolete-pin", "sha256": "a" * 64},
        source=Origin.HUMAN,
    )

    storage.compact_run(run_id, constraints_path=constraints_file)

    live_events = storage.read_events(run_id)
    types = [e.type for e in live_events]

    assert EventType.CONSTRAINT_PIN_DROPPED in types
    dropped_ev = next(e for e in live_events if e.type is EventType.CONSTRAINT_PIN_DROPPED)
    assert dropped_ev.payload["constraint_id"] == "obsolete-pin"
    assert dropped_ev.payload["reason"] == "not_in_registry"

    state = CheckpointManager(storage).project_current(run_id, full_history=True)
    assert "obsolete-pin" not in state.pins
    assert "retained-pin" in state.pins


def test_post_compaction_no_registry_preserves_active_pins(tmp_path: Path) -> None:
    """When no registry file exists, active pins are re-anchored with verified=False."""
    db_path = str(tmp_path / "no_reg.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_no_reg"

    storage.create_run(Run(run_id=run_id, goal="test no reg"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "stand-alone-pin", "sha256": "f" * 64},
        source=Origin.HUMAN,
    )

    # Missing registry file
    missing_file = tmp_path / "absent_constraints.json"
    storage.compact_run(run_id, constraints_path=missing_file)

    live_events = storage.read_events(run_id)
    types = [e.type for e in live_events]

    assert EventType.CONSTRAINT_PINNED in types
    assert EventType.CONSTRAINT_PINS_VERIFIED in types

    verified_ev = next(e for e in live_events if e.type is EventType.CONSTRAINT_PINS_VERIFIED)
    assert verified_ev.payload["verified"] is False
    assert verified_ev.payload["count"] == 1

    state = CheckpointManager(storage).project_current(run_id, full_history=True)
    assert "stand-alone-pin" in state.pins


def test_post_compaction_clean_when_no_constraints_and_no_registry(tmp_path: Path) -> None:
    """A run with no constraints and no registry file undergoes compaction without extra events."""
    db_path = str(tmp_path / "clean.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_clean"

    storage.create_run(Run(run_id=run_id, goal="test clean"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(run_id, EventType.WORK_COMPLETED, {"count": 1})

    missing_file = tmp_path / "absent_constraints.json"
    storage.compact_run(run_id, constraints_path=missing_file)

    live_events = storage.read_events(run_id)
    types = {e.type for e in live_events}
    assert types == {EventType.STATE_CHECKPOINTED, EventType.EVENT_LOG_ANCHORED}


def test_reinject_constraint_pins_direct_helper(tmp_path: Path) -> None:
    """The reinject_constraint_pins helper works on arbitrary storage and run."""
    constraints_file = _write_constraints(
        tmp_path,
        [
            {
                "id": "direct-pin",
                "level": "hard",
                "predicate": "Direct hook test predicate.",
            }
        ],
    )
    db_path = str(tmp_path / "direct.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_direct"

    storage.create_run(Run(run_id=run_id, goal="direct test"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "direct"})

    appended = reinject_constraint_pins(storage, run_id, constraints_path=constraints_file)
    assert len(appended) == 2  # CONSTRAINT_PINNED and CONSTRAINT_PINS_VERIFIED
    assert appended[0].type is EventType.CONSTRAINT_PINNED
    assert appended[1].type is EventType.CONSTRAINT_PINS_VERIFIED


def test_cmd_precompact_reinjects_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precompact command re-injects constraint verification marker."""
    monkeypatch.chdir(tmp_path)
    _write_constraints(
        tmp_path,
        [
            {
                "id": "precompact-pin",
                "level": "hard",
                "predicate": "Must survive precompact hook.",
            }
        ],
    )
    db = str(tmp_path / "continuum.db")
    storage = SQLiteStorage(db)
    storage.create_run(Run(run_id="run_pre", goal="precompact test"))
    storage.append_event("run_pre", EventType.RUN_STARTED, {"goal": "precompact"})

    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "precompact", "--run-id", "run_pre"], out=out, err=err)
    assert code == 0, err.getvalue()

    live_events = storage.read_events("run_pre")
    types = [e.type for e in live_events]
    assert EventType.CONSTRAINT_PINNED in types
    assert EventType.CONSTRAINT_PINS_VERIFIED in types


def test_malformed_constraints_file_does_not_abort_compaction(tmp_path: Path) -> None:
    """A corrupted constraints.json does not abort compaction after archive commits."""
    bad_file = tmp_path / ".continuum" / "constraints.json"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("{not valid json", encoding="utf-8")

    db_path = str(tmp_path / "test_malformed.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_malformed"

    storage.create_run(Run(run_id=run_id, goal="test malformed registry"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="p1", sha256="a" * 64).model_dump(),
        source=Origin.HUMAN,
    )

    manager = CheckpointManager(storage)
    manager.checkpoint(run_id)

    # Compaction must succeed rather than crash
    report = storage.compact_run(run_id, constraints_path=bad_file)
    assert report["archived"] >= 1

    live = storage.read_events(run_id)
    types = [e.type for e in live]
    assert EventType.CONSTRAINT_PINS_VERIFIED in types
    verified_ev = next(e for e in live if e.type is EventType.CONSTRAINT_PINS_VERIFIED)
    assert verified_ev.payload["verified"] is False


def test_retracted_constraint_is_not_reinstated_across_compaction(tmp_path: Path) -> None:
    """An explicitly retracted constraint is not resurrected by post-compaction hook."""
    constraints_file = _write_constraints(
        tmp_path,
        [
            {
                "id": "retracted-rule",
                "level": "hard",
                "predicate": "A rule that was later retracted.",
            }
        ],
    )
    pred_hash = hashlib.sha256(b"A rule that was later retracted.").hexdigest()

    db_path = str(tmp_path / "test_retract.db")
    storage = SQLiteStorage(db_path)
    run_id = "run_retract"

    storage.create_run(Run(run_id=run_id, goal="test retraction persistence"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="retracted-rule", sha256=pred_hash).model_dump(),
        source=Origin.HUMAN,
    )
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_RETRACTED,
        {"constraint_id": "retracted-rule"},
        source=Origin.HUMAN,
    )

    manager = CheckpointManager(storage)
    manager.checkpoint(run_id)

    report = storage.compact_run(run_id, constraints_path=constraints_file)
    assert report["archived"] >= 1

    live = storage.read_events(run_id)
    pinned_events = [e for e in live if e.type is EventType.CONSTRAINT_PINNED]
    # Retracted pin must not be re-pinned in the live tail
    assert not any(e.payload.get("constraint_id") == "retracted-rule" for e in pinned_events)

    state = CheckpointManager(storage).project_current(run_id, full_history=True)
    assert "retracted-rule" not in state.pins
