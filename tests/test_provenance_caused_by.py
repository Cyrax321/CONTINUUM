"""Provenance caused_by payload tests (issue #551)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from continuum.events import EventLog, EventType
from continuum.models import ActionRecordPayload, DecisionPayload
from continuum.storage.sqlite import SQLiteStorage


def test_decision_payload_caused_by_defaults_to_empty() -> None:
    payload = DecisionPayload(decision="choose X")
    assert payload.caused_by == []
    restored = DecisionPayload.model_validate({"decision": "choose X"})
    assert restored.caused_by == []


def test_action_payload_caused_by_defaults_to_empty() -> None:
    payload = ActionRecordPayload(action_type="send_email")
    assert payload.caused_by == []
    restored = ActionRecordPayload.model_validate({"action_type": "send_email"})
    assert restored.caused_by == []


def test_decision_caused_by_round_trips_via_sqlite() -> None:
    storage = SQLiteStorage(":memory:")
    run_id = "run_caused_1"
    from continuum.models import Run

    storage.create_run(Run(run_id=run_id, goal="test"))
    ev = storage.append_event(
        run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "summary": "s"}
    )
    payload = DecisionPayload(decision="decide", caused_by=[ev.event_id]).model_dump()
    dec = storage.append_event(run_id, EventType.DECISION_CREATED, payload)
    assert dec.payload["caused_by"] == [ev.event_id]
    events = storage.read_events(run_id)
    found = [e for e in events if e.type == EventType.DECISION_CREATED][0]
    assert found.payload["caused_by"] == [ev.event_id]


def test_caused_by_survives_reopen() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        storage = SQLiteStorage(str(db))
        run_id = "run_reopen"
        from continuum.models import Run

        storage.create_run(Run(run_id=run_id, goal="g"))
        ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev2"})
        payload = DecisionPayload(decision="d", caused_by=[ev.event_id]).model_dump()
        storage.append_event(run_id, EventType.DECISION_CREATED, payload)
        storage.close()
        storage2 = SQLiteStorage(str(db))
        events = storage2.read_events(run_id)
        dec = [e for e in events if e.type == EventType.DECISION_CREATED][0]
        assert dec.payload["caused_by"] == [ev.event_id]
        storage2.close()


def test_unknown_caused_by_raises_via_storage() -> None:
    storage = SQLiteStorage(":memory:")
    run_id = "run_unknown_storage"
    from continuum.models import Run

    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    payload = DecisionPayload(decision="d", caused_by=["event_unknown_123"]).model_dump()
    with pytest.raises(ValueError, match="unknown caused_by"):
        storage.append_event(run_id, EventType.DECISION_CREATED, payload)


def test_unknown_caused_by_raises_via_eventlog() -> None:
    log = EventLog()
    run_id = "run_unknown_log"
    ev = log.append(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    with pytest.raises(ValueError, match="unknown caused_by"):
        log.append(run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": ["nope"]})
    ok = log.append(
        run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": [ev.event_id]}
    )
    assert ok.payload["caused_by"] == [ev.event_id]


def test_caused_by_is_hash_covered() -> None:
    log = EventLog()
    run_id = "run_hash"
    ev = log.append(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    dec = log.append(
        run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": [ev.event_id]}
    )
    original_hash = dec.hash
    tampered = dec.model_copy(update={"payload": {**dec.payload, "caused_by": ["tampered"]}})
    assert tampered.digest() != original_hash
    storage = SQLiteStorage(":memory:")
    from continuum.models import Run

    storage.create_run(Run(run_id="run_hash2", goal="g"))
    e1 = storage.append_event("run_hash2", EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    d1 = storage.append_event(
        "run_hash2", EventType.DECISION_CREATED, {"decision": "d", "caused_by": [e1.event_id]}
    )
    import json

    with storage._write() as c:
        c.execute(
            "UPDATE events SET payload = ? WHERE event_id = ?",
            (json.dumps({"decision": "d", "caused_by": ["x"]}), d1.event_id),
        )
    report2 = storage.verify_events("run_hash2")
    assert not report2.ok


def test_caused_by_caps_are_enforced() -> None:
    with pytest.raises(ValueError, match="at most 32"):
        DecisionPayload(decision="d", caused_by=[f"e{i}" for i in range(33)])
    with pytest.raises(ValueError, match="1-128"):
        DecisionPayload(decision="d", caused_by=[""])
    with pytest.raises(ValueError, match="1-128"):
        DecisionPayload(decision="d", caused_by=["x" * 129])
    ok = DecisionPayload(decision="d", caused_by=["x" * 128])
    assert len(ok.caused_by[0]) == 128
    ok2 = DecisionPayload(decision="d", caused_by=[f"e{i}" for i in range(32)])
    assert len(ok2.caused_by) == 32
    with pytest.raises(ValueError, match="1-128"):
        ActionRecordPayload(action_type="t", caused_by=["x" * 129])


def test_empty_caused_by_round_trips() -> None:
    storage = SQLiteStorage(":memory:")
    from continuum.models import Run

    run_id = "run_empty"
    storage.create_run(Run(run_id=run_id, goal="g"))
    dec = storage.append_event(
        run_id, EventType.DECISION_CREATED, {"decision": "d", "caused_by": []}
    )
    assert dec.payload["caused_by"] == []
    events = storage.read_events(run_id)
    assert events[0].payload["caused_by"] == []
    dec2 = storage.append_event(run_id, EventType.DECISION_CREATED, {"decision": "d2"})
    assert dec2.payload.get("caused_by", []) == []
    log = EventLog()
    e = log.append(run_id + "_log", EventType.DECISION_CREATED, {"decision": "d"})
    assert e.payload.get("caused_by", []) == []


def test_old_events_without_caused_by_project_as_empty() -> None:
    from continuum.state.semantic import project

    log = EventLog()
    run_id = "run_old"
    log.append(run_id, EventType.RUN_STARTED, {"goal": "g"})
    log.append(run_id, EventType.DECISION_CREATED, {"decision": "old", "decision_id": "dec_old"})
    state = project(run_id, log.events(run_id))
    assert any(d.decision_id == "dec_old" for d in state.decisions)


def test_action_record_caused_by_round_trip() -> None:
    storage = SQLiteStorage(":memory:")
    from continuum.models import Run

    run_id = "run_action"
    storage.create_run(Run(run_id=run_id, goal="g"))
    ev = storage.append_event(run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1"})
    payload = ActionRecordPayload(action_type="send", caused_by=[ev.event_id]).model_dump()
    payload.update(
        {"key": "k1", "action": {"action_id": "a1", "run_id": run_id, "action_type": "send"}}
    )
    rec = storage.append_event(run_id, EventType.ACTION_RECORDED, payload)
    assert rec.payload["caused_by"] == [ev.event_id]
    events = storage.read_events(run_id)
    assert [e for e in events if e.type == EventType.ACTION_RECORDED][0].payload["caused_by"] == [
        ev.event_id
    ]
