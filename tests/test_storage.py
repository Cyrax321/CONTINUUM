from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from continuum.adapters.generic import GenericAgentAdapter
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType, Origin
from continuum.models import (
    Decision,
    Goal,
    Progress,
    Run,
    RunStatus,
    SemanticState,
    StateCheckpoint,
)
from continuum.state.semantic import project
from continuum.storage import (
    CheckpointNotFound,
    ConcurrentWriteError,
    CorruptedRecord,
    RunNotFound,
    SQLiteStorage,
    open_storage,
)


@pytest.fixture
def storage() -> SQLiteStorage:
    store = SQLiteStorage(":memory:")
    yield store
    store.close()


@pytest.fixture
def run(storage: SQLiteStorage) -> Run:
    return storage.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))


def make_state(run_id: str = "run_1", **overrides: object) -> SemanticState:
    base: dict[str, object] = {"run_id": run_id, "goal": Goal(description="Analyze 100 documents")}
    base.update(overrides)
    return SemanticState(**base)  # type: ignore[arg-type]


# --- runs ------------------------------------------------------------------ #


def test_a_run_round_trips(storage: SQLiteStorage, run: Run) -> None:
    loaded = storage.get_run("run_1")
    assert loaded.goal == "Analyze 100 documents"
    assert loaded.status is RunStatus.STARTED
    assert loaded.created_at == run.created_at


def test_missing_runs_raise(storage: SQLiteStorage) -> None:
    with pytest.raises(RunNotFound):
        storage.get_run("ghost")


def test_duplicate_run_ids_are_refused(storage: SQLiteStorage, run: Run) -> None:
    with pytest.raises(ConcurrentWriteError, match="already exists"):
        storage.create_run(Run(run_id="run_1", goal="other"))


def test_updating_a_run_advances_its_timestamp(storage: SQLiteStorage, run: Run) -> None:
    updated = storage.update_run(run.model_copy(update={"status": RunStatus.COMPLETED}))
    assert updated.updated_at >= run.updated_at
    assert storage.get_run("run_1").status is RunStatus.COMPLETED


def test_updating_an_unknown_run_raises(storage: SQLiteStorage) -> None:
    with pytest.raises(RunNotFound):
        storage.update_run(Run(run_id="ghost", goal="g"))


def test_runs_are_listed_newest_first(storage: SQLiteStorage) -> None:
    for i in range(3):
        storage.create_run(Run(run_id=f"run_{i}", goal=f"goal {i}"))
    assert len(storage.list_runs()) == 3
    assert len(storage.list_runs(limit=2)) == 2


def test_run_metadata_survives(storage: SQLiteStorage) -> None:
    storage.create_run(Run(run_id="run_m", goal="g", metadata={"owner": "sam", "retries": 2}))
    assert storage.get_run("run_m").metadata == {"owner": "sam", "retries": 2}


def test_get_active_run_returns_none_when_there_are_no_runs(storage: SQLiteStorage) -> None:
    assert storage.get_active_run() is None


def test_get_active_run_excludes_terminal_runs(storage: SQLiteStorage) -> None:
    storage.create_run(Run(run_id="c", goal="g", status=RunStatus.COMPLETED))
    storage.create_run(Run(run_id="x", goal="g", status=RunStatus.ABORTED))
    storage.create_run(Run(run_id="f", goal="g", status=RunStatus.FAILED))
    assert storage.get_active_run() is None


def test_get_active_run_returns_the_most_recent_non_terminal_run(storage: SQLiteStorage) -> None:
    # A finished run touched more recently must still be excluded.
    finished = storage.create_run(Run(run_id="done", goal="g", status=RunStatus.COMPLETED))
    storage.update_run(finished.model_copy(update={"goal": "done again"}))
    active = storage.create_run(Run(run_id="active", goal="g"))
    assert storage.get_active_run().run_id == active.run_id


# --- events ---------------------------------------------------------------- #


def test_events_persist_with_sequence_and_chain(storage: SQLiteStorage, run: Run) -> None:
    first = storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    second = storage.append_event("run_1", EventType.WORK_COMPLETED, {})

    assert (first.sequence, second.sequence) == (1, 2)
    assert second.prev_hash == first.hash
    assert storage.last_sequence("run_1") == 2

    stored = storage.read_events("run_1")
    assert [e.sequence for e in stored] == [1, 2]
    assert stored[0].payload == {"goal": "g"}


def test_events_require_an_existing_run(storage: SQLiteStorage) -> None:
    with pytest.raises(RunNotFound):
        storage.append_event("ghost", EventType.RUN_STARTED, {})


def test_events_can_be_read_by_cursor_and_bound(storage: SQLiteStorage, run: Run) -> None:
    for _ in range(5):
        storage.append_event("run_1", EventType.WORK_COMPLETED, {})
    assert [e.sequence for e in storage.read_events("run_1", after_sequence=3)] == [4, 5]
    assert [e.sequence for e in storage.read_events("run_1", upto=2)] == [1, 2]


def test_last_sequence_of_an_empty_run_is_zero(storage: SQLiteStorage, run: Run) -> None:
    assert storage.last_sequence("run_1") == 0


def test_optimistic_concurrency_rejects_a_stale_writer(storage: SQLiteStorage, run: Run) -> None:
    storage.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=0)
    with pytest.raises(ConcurrentWriteError, match="expected 0"):
        storage.append_event("run_1", EventType.WORK_COMPLETED, {}, expected_sequence=0)


def test_persisted_chain_verifies(storage: SQLiteStorage, run: Run) -> None:
    for _ in range(4):
        storage.append_event("run_1", EventType.WORK_COMPLETED, {})
    report = storage.verify_events("run_1")
    assert report.ok
    assert report.checked == 4
    assert report.trusted_through["run_1"] == 4


def test_sealed_events_can_be_transplanted(storage: SQLiteStorage, run: Run) -> None:
    from continuum.events import EventLog

    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
    log.append("run_1", EventType.WORK_COMPLETED, {})

    assert storage.extend_events(log.events("run_1")) == 2
    assert storage.read_events("run_1") == list(log.events("run_1"))
    assert storage.verify_events("run_1").ok


def test_transplanting_out_of_order_is_refused(storage: SQLiteStorage, run: Run) -> None:
    from continuum.events import EventLog

    log = EventLog()
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
    log.append("run_1", EventType.WORK_COMPLETED, {})

    with pytest.raises(ConcurrentWriteError, match="expected sequence 1"):
        storage.append_sealed(log.events("run_1")[1])


def test_transplanting_a_forged_event_is_refused(storage: SQLiteStorage, run: Run) -> None:
    from continuum.events import EventLog

    log = EventLog()
    original = log.append("run_1", EventType.RUN_STARTED, {"goal": "g"})
    forged = original.model_copy(update={"payload": {"goal": "hijacked"}})

    with pytest.raises(CorruptedRecord, match="hash does not match"):
        storage.append_sealed(forged)


# --- durability across processes ------------------------------------------- #


def test_state_survives_closing_and_reopening_the_database(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"

    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 100})
        store.append_event("run_1", EventType.WORK_COMPLETED, {"count": 42})

    with SQLiteStorage(db) as reopened:
        assert reopened.get_run("run_1").goal == "Analyze 100 documents"
        state = project("run_1", reopened.read_events("run_1"))
        assert state.progress.completed == 42
        assert reopened.verify_events("run_1").ok


def test_a_crashed_writer_leaves_a_readable_prefix(tmp_path: Path) -> None:
    """Simulate death mid-run: whatever committed must still project cleanly."""
    db = tmp_path / "agent.db"
    store = SQLiteStorage(db)
    store.create_run(Run(run_id="run_1", goal="g"))
    store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 10})
    store.append_event("run_1", EventType.WORK_COMPLETED, {})
    del store  # no close(): the process simply vanished

    with SQLiteStorage(db) as recovered:
        assert recovered.last_sequence("run_1") == 2
        assert project("run_1", recovered.read_events("run_1")).progress.completed == 1


def test_two_connections_to_one_file_see_each_other(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as writer, SQLiteStorage(db) as reader:
        writer.create_run(Run(run_id="run_1", goal="g"))
        writer.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        assert reader.last_sequence("run_1") == 1

        # the second connection continues the same chain, it does not fork it
        appended = reader.append_event("run_1", EventType.WORK_COMPLETED, {})
        assert appended.sequence == 2
        assert writer.verify_events("run_1").ok


# --- versions -------------------------------------------------------------- #


def test_versions_are_numbered_and_addressable(storage: SQLiteStorage, run: Run) -> None:
    assert storage.put_version(make_state(), reason="start") == 0
    assert storage.put_version(make_state(progress=Progress(completed=1))) == 1
    assert list(storage.list_versions("run_1")) == [0, 1]
    assert storage.get_version("run_1", 1).progress.completed == 1


def test_an_unchanged_state_does_not_create_a_version(storage: SQLiteStorage, run: Run) -> None:
    assert storage.put_version(make_state()) == 0
    assert storage.put_version(make_state()) == 0
    assert list(storage.list_versions("run_1")) == [0]


def test_latest_version_reflects_the_newest_commit(storage: SQLiteStorage, run: Run) -> None:
    assert storage.latest_version("run_1") is None
    storage.put_version(make_state())
    storage.put_version(make_state(progress=Progress(completed=7)))
    latest = storage.latest_version("run_1")
    assert latest is not None and latest.progress.completed == 7


def test_a_missing_version_raises(storage: SQLiteStorage, run: Run) -> None:
    with pytest.raises(CheckpointNotFound):
        storage.get_version("run_1", 99)


def test_versions_require_an_existing_run(storage: SQLiteStorage) -> None:
    with pytest.raises(RunNotFound):
        storage.put_version(make_state(run_id="ghost"))


def test_rich_state_survives_the_round_trip(storage: SQLiteStorage, run: Run) -> None:
    original = make_state(
        decisions=[Decision(decision_id="d1", decision="peer-reviewed only", evidence=["u_1"])],
        progress=Progress(total=100, completed=3, pending=97),
    )
    storage.put_version(original)
    restored = storage.get_version("run_1", 0)

    assert restored.decisions[0].decision == "peer-reviewed only"
    assert restored.decisions[0].provenance == original.decisions[0].provenance
    assert restored.progress == original.progress


# --- checkpoints ----------------------------------------------------------- #


def test_checkpoints_are_sealed_on_write(storage: SQLiteStorage, run: Run) -> None:
    stored = storage.put_checkpoint(
        StateCheckpoint(run_id="run_1", version=17, trigger="milestone", state=make_state())
    )
    assert stored.integrity_hash is not None
    assert stored.verify()

    loaded = storage.get_checkpoint(stored.checkpoint_id)
    assert loaded == stored
    assert loaded.state.goal.description == "Analyze 100 documents"


def test_latest_checkpoint_tracks_the_highest_version(storage: SQLiteStorage, run: Run) -> None:
    assert storage.latest_checkpoint("run_1") is None
    for version in (1, 5, 3):
        storage.put_checkpoint(StateCheckpoint(run_id="run_1", version=version, state=make_state()))
    latest = storage.latest_checkpoint("run_1")
    assert latest is not None and latest.version == 5
    assert [c.version for c in storage.list_checkpoints("run_1")] == [1, 3, 5]


def test_a_missing_checkpoint_raises(storage: SQLiteStorage) -> None:
    with pytest.raises(CheckpointNotFound):
        storage.get_checkpoint("ghost")


def test_duplicate_checkpoint_ids_are_refused(storage: SQLiteStorage, run: Run) -> None:
    checkpoint = StateCheckpoint(checkpoint_id="cp_1", run_id="run_1", state=make_state())
    storage.put_checkpoint(checkpoint)
    with pytest.raises(ConcurrentWriteError):
        storage.put_checkpoint(checkpoint)


def test_checkpoint_environment_survives_reload_and_validates_dependency(
    tmp_path: Path,
) -> None:
    db = tmp_path / "reload.db"
    rid = "run_reload"

    store1 = SQLiteStorage(db)
    adapter1 = GenericAgentAdapter(store1)
    store1.create_run(Run(run_id=rid, goal="trusted task", status=RunStatus.STARTED))
    store1.append_event(
        rid, EventType.RUN_STARTED, {"goal": "trusted task"}, source=Origin.DETERMINISTIC
    )
    for _ in range(50):
        store1.append_event(
            rid, EventType.WORK_COMPLETED, {"count": 1}, source=Origin.DETERMINISTIC
        )
    store1.append_event(
        rid,
        EventType.DEPENDENCY_DECLARED,
        {"resource": "dataset", "version": "v1"},
        source=Origin.DETERMINISTIC,
    )
    env = capture(rid, StaticProvider(dataset="v1"))
    adapter1.capture_state(
        rid, adapter1.restore_state(rid), environment=env, reason="trusted checkpoint"
    )
    store1.close()

    store2 = SQLiteStorage(db)
    restored = CheckpointManager(store2).restore(rid, replay=False)
    assert restored.checkpoint is not None
    assert restored.checkpoint.environment is not None
    assert restored.checkpoint.environment.resources["dataset"].version == "v1"

    adapter2 = GenericAgentAdapter(store2)
    decision = adapter2.resume(rid, current_environment=env)
    assert decision.mode.value == "resume"
    assert decision.safe is True
    dep_entries = [
        e for e in decision.validation.report.statuses if e.component.value == "external_dependency"
    ]
    assert dep_entries
    assert all(e.status.value == "valid" for e in dep_entries)
    store2.close()


# --- corruption is refused, never returned --------------------------------- #


def test_a_tampered_event_row_is_detected_by_verify(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        store.append_event("run_1", EventType.WORK_COMPLETED, {})

    raw = sqlite3.connect(db)
    raw.execute(
        "UPDATE events SET payload = ? WHERE run_id = 'run_1' AND sequence = 1",
        (json.dumps({"goal": "hijacked"}),),
    )
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert not report.ok
        assert {v.kind for v in report.violations} == {"TAMPERED_CONTENT", "BROKEN_CHAIN"}
        assert report.trusted_through["run_1"] == 0


def test_a_deleted_event_row_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        for _ in range(3):
            store.append_event("run_1", EventType.WORK_COMPLETED, {})

    raw = sqlite3.connect(db)
    raw.execute("DELETE FROM events WHERE run_id = 'run_1' AND sequence = 2")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert not report.ok
        assert {v.kind for v in report.violations} >= {"SEQUENCE_GAP", "BROKEN_CHAIN"}
        assert report.trusted_through["run_1"] == 1


def test_an_unreadable_event_row_is_reported_not_raised(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})

    raw = sqlite3.connect(db)
    raw.execute("UPDATE events SET payload = 'not json' WHERE sequence = 1")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store:
        report = store.verify_events("run_1")
        assert not report.ok
        assert report.violations[0].kind == "UNREADABLE_RECORD"
        with pytest.raises(CorruptedRecord):
            store.read_events("run_1")


def test_a_tampered_version_row_refuses_to_load(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.put_version(make_state())

    raw = sqlite3.connect(db)
    forged = make_state(progress=Progress(completed=9999)).model_dump_json()
    raw.execute("UPDATE versions SET state = ? WHERE run_id = 'run_1'", (forged,))
    raw.commit()
    raw.close()

    with (
        SQLiteStorage(db) as store,
        pytest.raises(CorruptedRecord, match="fingerprint does not match"),
    ):
        store.get_version("run_1", 0)


def test_a_tampered_checkpoint_refuses_to_load(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.put_checkpoint(
            StateCheckpoint(checkpoint_id="cp_1", run_id="run_1", state=make_state())
        )

    raw = sqlite3.connect(db)
    row = raw.execute("SELECT body FROM checkpoints WHERE checkpoint_id = 'cp_1'").fetchone()
    body = json.loads(row[0])
    body["state"]["progress"]["completed"] = 9999
    raw.execute("UPDATE checkpoints SET body = ? WHERE checkpoint_id = 'cp_1'", (json.dumps(body),))
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store, pytest.raises(CorruptedRecord, match="integrity hash"):
        store.get_checkpoint("cp_1")


def test_a_corrupted_run_row_refuses_to_load(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))

    raw = sqlite3.connect(db)
    raw.execute("UPDATE runs SET status = 'not_a_status' WHERE run_id = 'run_1'")
    raw.commit()
    raw.close()

    with SQLiteStorage(db) as store, pytest.raises(CorruptedRecord):
        store.get_run("run_1")


# --- schema and URLs ------------------------------------------------------- #


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    from continuum.storage import SchemaVersionError
    from continuum.storage.sqlite import SCHEMA_VERSION

    db = tmp_path / "agent.db"
    SQLiteStorage(db).close()

    raw = sqlite3.connect(db)
    raw.execute(
        "UPDATE continuum_meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    raw.commit()
    raw.close()

    with pytest.raises(SchemaVersionError, match="newer CONTINUUM"):
        SQLiteStorage(db)


def test_an_older_schema_is_refused(tmp_path: Path) -> None:
    from continuum.storage import SchemaVersionError
    from continuum.storage.sqlite import SCHEMA_VERSION

    db = tmp_path / "agent.db"
    SQLiteStorage(db).close()

    raw = sqlite3.connect(db)
    raw.execute(
        "UPDATE continuum_meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION - 1),),
    )
    raw.commit()
    raw.close()

    with pytest.raises(SchemaVersionError, match="older CONTINUUM"):
        SQLiteStorage(db)


def test_reopening_an_existing_database_is_not_a_migration(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
    with SQLiteStorage(db) as reopened:
        assert reopened.get_run("run_1").goal == "g"


def test_storage_urls_are_accepted_in_several_forms(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with open_storage(f"sqlite:///{db}") as store:
        store.create_run(Run(run_id="run_1", goal="g"))
    with open_storage(str(db)) as store:
        assert store.get_run("run_1").goal == "g"
    with open_storage() as memory:
        assert memory.list_runs() == []


def test_postgres_fails_clearly_rather_than_silently_using_sqlite() -> None:
    with pytest.raises(NotImplementedError, match="not implemented yet"):
        open_storage("postgresql://localhost/continuum")


def test_an_unknown_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported storage URL scheme"):
        open_storage("mysql://localhost/continuum")
