"""Boundary conditions in persistence: bad payloads, forked chains, bad rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from continuum.events import Event, EventLog, EventType
from continuum.models import Goal, Progress, Run, SemanticState, StateCheckpoint
from continuum.storage import (
    ConcurrentWriteError,
    CorruptedRecord,
    SQLiteStorage,
    open_storage,
)


def make_state(run_id: str = "run_1", **overrides: object) -> SemanticState:
    base: dict[str, object] = {"run_id": run_id, "goal": Goal(description="g")}
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


# --- payloads must survive a round-trip ------------------------------------ #


def test_a_datetime_payload_is_rejected_at_construction() -> None:
    """Caught at the boundary: it would hash differently once reloaded."""
    with pytest.raises(ValidationError, match="not JSON-native"):
        Event(
            run_id="run_1",
            sequence=1,
            type=EventType.TOOL_CALLED,
            payload={"when": datetime(2026, 1, 1, tzinfo=UTC)},
        )


def test_nested_non_native_values_are_reported_with_a_path() -> None:
    with pytest.raises(ValidationError, match=r"payload\.outer\[1\]\.deep"):
        Event(
            run_id="run_1",
            sequence=1,
            type=EventType.TOOL_CALLED,
            payload={"outer": [{}, {"deep": {1, 2}}]},
        )


def test_non_string_payload_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="keys must be strings"):
        Event(run_id="run_1", sequence=1, type=EventType.TOOL_CALLED, payload={7: "x"})


def test_json_native_payloads_are_accepted_and_normalised() -> None:
    event = Event(
        run_id="run_1",
        sequence=1,
        type=EventType.TOOL_CALLED,
        payload={"s": "x", "n": 1, "f": 1.5, "b": True, "z": None, "xs": (1, 2)},
    ).sealed()
    reloaded = Event.model_validate_json(event.model_dump_json())
    assert reloaded.digest() == event.hash
    assert reloaded.payload["xs"] == [1, 2]


def test_an_explicitly_empty_payload_is_allowed() -> None:
    event = Event(run_id="run_1", sequence=1, type=EventType.TOOL_CALLED, payload=None)
    assert event.payload == {}


# --- forked and malformed chains ------------------------------------------- #


def test_a_forked_sealed_event_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

        log = EventLog()
        log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
        forked = log.append("run_1", EventType.WORK_COMPLETED, {})

        with pytest.raises(CorruptedRecord, match="broken chain"):
            store.append_sealed(forked)


def test_a_duplicate_event_id_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        first = store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

        clone = Event(
            event_id=first.event_id,
            run_id="run_1",
            sequence=2,
            type=EventType.WORK_COMPLETED,
            prev_hash=first.hash,
        ).sealed()
        with pytest.raises(ConcurrentWriteError, match="taken by another writer"):
            store.append_sealed(clone)


def test_an_unparseable_version_row_refuses_to_load(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.put_version(make_state())

    raw = sqlite3.connect(db)
    raw.execute("UPDATE versions SET state = '{\"nonsense\": true}' WHERE run_id = 'run_1'")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store, pytest.raises(CorruptedRecord, match="failed to load"):
        store.get_version("run_1", 0)


def test_an_unparseable_checkpoint_refuses_to_load(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.put_checkpoint(
            StateCheckpoint(checkpoint_id="cp_1", run_id="run_1", state=make_state())
        )

    raw = sqlite3.connect(db)
    raw.execute("UPDATE checkpoints SET body = '{\"bad\": 1}' WHERE checkpoint_id = 'cp_1'")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store, pytest.raises(CorruptedRecord, match="failed to load"):
        store.get_checkpoint("cp_1")


def test_latest_version_refuses_a_corrupted_head(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.put_version(make_state())

    raw = sqlite3.connect(db)
    forged = make_state(progress=Progress(completed=42)).model_dump_json()
    raw.execute("UPDATE versions SET state = ? WHERE run_id = 'run_1'", (forged,))
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store, pytest.raises(CorruptedRecord):
        store.latest_version("run_1")


def test_a_corrupted_run_row_is_caught_when_listing(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))

    raw = sqlite3.connect(db)
    raw.execute("UPDATE runs SET metadata = 'not json' WHERE run_id = 'run_1'")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store, pytest.raises(CorruptedRecord):
        store.list_runs()


# --- URLs ------------------------------------------------------------------- #


def test_not_found_errors_read_cleanly_despite_inheriting_keyerror() -> None:
    """KeyError.__str__ repr-wraps its message; these must not."""
    from continuum.storage import CheckpointNotFound, RunNotFound

    assert str(RunNotFound("ghost")) == "no such run: 'ghost'"
    assert str(CheckpointNotFound("run 'r' has no version 9")) == "run 'r' has no version 9"
    assert str(CheckpointNotFound()) == "CheckpointNotFound"
    # still catchable as a KeyError by callers that expect one
    assert isinstance(RunNotFound("ghost"), KeyError)


def test_an_absolute_path_is_not_mangled_into_a_relative_one() -> None:
    from continuum.storage.sqlite import _resolve_path

    assert _resolve_path("sqlite:////var/db/agent.db") == "//var/db/agent.db"
    assert _resolve_path("sqlite:///var/db/agent.db") == "/var/db/agent.db"
    assert _resolve_path("sqlite://relative.db") == "relative.db"
    assert _resolve_path("/plain/abs.db") == "/plain/abs.db"


def test_a_drive_letter_survives_the_three_slash_form() -> None:
    """``sqlite:///C:/db`` is the absolute form of ``C:/db`` on Windows (#842).

    The leftover third slash produced ``/C:/db``, which sqlite3 rejects on
    Windows, so the URL grammar's slash is dropped and the drive letter kept.
    POSIX absolute paths must keep theirs, which the sibling assertions pin.
    """
    from continuum.storage.sqlite import _resolve_path

    assert _resolve_path("sqlite:///C:/data/agent.db") == "C:/data/agent.db"
    assert _resolve_path("sqlite:///d:/agent.db") == "d:/agent.db"
    # A plain drive-letter path was already fine and stays untouched.
    assert _resolve_path("C:/data/agent.db") == "C:/data/agent.db"
    # POSIX forms regress to nothing: the slash before a non-drive stem stays.
    assert _resolve_path("sqlite:///var/db/agent.db") == "/var/db/agent.db"
    assert _resolve_path("sqlite:////var/db/agent.db") == "//var/db/agent.db"


def test_a_drive_letter_url_opens_on_windows(tmp_path: Path) -> None:
    """End to end: the three-slash drive form must open a real database.

    Runs on every platform so the POSIX green path is pinned too; the
    interesting failure (sqlite3 rejecting ``/C:/...``) only existed on
    Windows.
    """
    db = tmp_path / "agent.db"
    with open_storage(f"sqlite:///{db.as_posix()}") as store:
        store.create_run(Run(run_id="run_1", goal="g"))
    assert db.exists(), "database was created somewhere other than the requested path"


def test_the_absolute_url_form_writes_where_it_says(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with open_storage(f"sqlite://{db}") as store:
        store.create_run(Run(run_id="run_1", goal="g"))
    assert db.exists(), "database was created somewhere other than the requested path"


def test_an_empty_url_falls_back_to_memory() -> None:
    from continuum.storage.sqlite import _resolve_path

    assert _resolve_path("sqlite:///") == ":memory:"
    assert _resolve_path("") == ":memory:"


def test_a_path_object_is_accepted(tmp_path: Path) -> None:
    with SQLiteStorage(tmp_path / "agent.db") as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        assert store.get_run("run_1").goal == "g"


# --- foreign keys ----------------------------------------------------------- #


def test_deleting_a_run_cascades_to_its_records(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        store.put_version(make_state())
        store.put_checkpoint(StateCheckpoint(run_id="run_1", state=make_state()))

    raw = sqlite3.connect(db)
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("DELETE FROM runs WHERE run_id = 'run_1'")
    raw.commit()
    for table in ("events", "versions", "checkpoints"):
        remaining = raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert remaining == 0, f"{table} kept orphaned rows"
    raw.close()


def test_checkpoints_require_an_existing_run(tmp_path: Path) -> None:
    from continuum.storage import RunNotFound

    with SQLiteStorage(tmp_path / "agent.db") as store, pytest.raises(RunNotFound):
        store.put_checkpoint(StateCheckpoint(run_id="ghost", state=make_state("ghost")))


def test_stored_json_is_canonical_so_rows_are_comparable(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.TOOL_CALLED, {"b": 2, "a": 1})

    raw = sqlite3.connect(db)
    payload = raw.execute("SELECT payload FROM events WHERE sequence = 1").fetchone()[0]
    raw.close()
    assert payload == json.dumps({"a": 1, "b": 2}, sort_keys=True)
