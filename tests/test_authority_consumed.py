"""AUTHORITY_CONSUMED event type, hash-chained and provenance-stamped (issue #555)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from continuum.actions.authority import record_authority_consumed
from continuum.events import EventType
from continuum.models import AuthorityConsumed, Origin, Run
from continuum.storage import SQLiteStorage


def _make_storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    return storage


def test_helper_appends_deterministic_hash_chained_event() -> None:
    storage = _make_storage()
    try:
        before = storage.last_sequence("run_1")
        event = record_authority_consumed(storage, "run_1", "auth-123")
        assert event.type == EventType.AUTHORITY_CONSUMED
        assert event.source == Origin.DETERMINISTIC
        assert event.sequence == before + 1
        assert event.hash is not None
        assert event.hash == event.digest()
        assert event.prev_hash is not None
        report = storage.verify_events("run_1")
        assert report.ok
        assert report.trusted_through["run_1"] == storage.last_sequence("run_1")
        assert event.payload["authority_id"] == "auth-123"
        assert event.payload["consumer_run_id"] == "run_1"
        assert "consumed_at" in event.payload
        assert event.payload["via_action_id"] is None
        event2 = record_authority_consumed(storage, "run_1", "auth-456", via_action_id="action_1")
        assert event2.payload["via_action_id"] == "action_1"
        assert event2.source == Origin.DETERMINISTIC
    finally:
        storage.close()


def test_provenance_is_deterministic() -> None:
    storage = _make_storage()
    try:
        event = record_authority_consumed(storage, "run_1", "tok-1")
        assert event.source == Origin.DETERMINISTIC
        assert not event.source.self_certified
        events = list(storage.read_events("run_1"))
        consumed = [e for e in events if e.type == EventType.AUTHORITY_CONSUMED]
        assert len(consumed) == 1
        assert consumed[0].source == Origin.DETERMINISTIC
    finally:
        storage.close()


def test_hash_chain_tamper_is_detected() -> None:
    storage = _make_storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-tamper")
        events = list(storage.read_events("run_1"))
        tampered_event = None
        for ev in events:
            if ev.type == EventType.AUTHORITY_CONSUMED:
                tampered_event = ev
                break
        assert tampered_event is not None
        bad_payload = dict(tampered_event.payload)
        bad_payload["authority_id"] = "hacked"
        bad = tampered_event.model_copy(update={"payload": bad_payload})
        from continuum.events import AppendOnlyViolation, EventLog

        log = EventLog()
        for e in events:
            if e.event_id == tampered_event.event_id:
                try:
                    log.extend([bad])
                except AppendOnlyViolation as exc:
                    assert "hash does not match content" in str(exc).lower()
                    break
                else:
                    rep = log.verify("run_1")
                    assert not rep.ok
                    assert any(v.kind == "TAMPERED_CONTENT" for v in rep.violations)
                    break
            else:
                log.extend([e])
        assert storage.verify_events("run_1").ok
    finally:
        storage.close()


def test_bounded_size() -> None:
    storage = _make_storage()
    try:
        max_id = "a" * 128
        event = record_authority_consumed(storage, "run_1", max_id)
        payload_json = json.dumps(event.payload, sort_keys=True)
        assert len(max_id) == 128
        assert len(payload_json) < 2048
        with pytest.raises(ValueError):
            record_authority_consumed(storage, "run_1", "a" * 129)
        with pytest.raises(ValueError):
            record_authority_consumed(storage, "run_1", "")
        with pytest.raises(ValueError):
            record_authority_consumed(storage, "run_1", "   ")
        with pytest.raises(ValueError):
            AuthorityConsumed(
                authority_id="ok",
                consumer_run_id="run_1",
                consumed_at=datetime.now(UTC),
                via_action_id="x" * 257,
            )
        with pytest.raises(ValueError):
            record_authority_consumed(storage, "run_1", "auth-ok", via_action_id="   ")
    finally:
        storage.close()


def test_no_dedup_every_consumption_is_distinct_row() -> None:
    storage = _make_storage()
    try:
        event1 = record_authority_consumed(storage, "run_1", "same-id")
        event2 = record_authority_consumed(storage, "run_1", "same-id")
        assert event1.event_id != event2.event_id
        assert event1.hash != event2.hash
        assert event1.sequence + 1 == event2.sequence
        assert event2.prev_hash == event1.hash
        events = [e for e in storage.read_events("run_1") if e.type == EventType.AUTHORITY_CONSUMED]
        assert len(events) == 2
        assert events[0].payload["authority_id"] == "same-id"
        assert events[1].payload["authority_id"] == "same-id"
        assert storage.verify_events("run_1").ok
    finally:
        storage.close()


def test_replay_preserves_rows() -> None:
    storage = _make_storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-1")
        record_authority_consumed(storage, "run_1", "auth-2", via_action_id="act-1")
        record_authority_consumed(storage, "run_1", "auth-1")
        events = list(storage.read_events("run_1"))
        consumed = [e for e in events if e.type == EventType.AUTHORITY_CONSUMED]
        assert len(consumed) == 3
        from continuum.events import EventLog

        log = EventLog()
        log.extend(events)
        replayed = [e for e in log.events("run_1") if e.type == EventType.AUTHORITY_CONSUMED]
        assert len(replayed) == 3
        assert [e.payload["authority_id"] for e in replayed] == ["auth-1", "auth-2", "auth-1"]
        assert replayed[1].payload["via_action_id"] == "act-1"
        all_events = list(storage.read_all_events("run_1"))
        all_consumed = [e for e in all_events if e.type == EventType.AUTHORITY_CONSUMED]
        assert len(all_consumed) == 3
    finally:
        storage.close()


def test_consumer_run_id_and_consumed_at_overrides() -> None:
    storage = _make_storage()
    try:
        custom_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        event = record_authority_consumed(
            storage,
            "run_1",
            "auth-custom",
            consumer_run_id="run_2",
            consumed_at=custom_time,
            via_action_id="act-99",
        )
        assert event.payload["consumer_run_id"] == "run_2"
        assert event.payload["consumed_at"] in (
            custom_time.isoformat(),
            custom_time.isoformat().replace("+00:00", "Z"),
        )
        assert event.payload["via_action_id"] == "act-99"
        event2 = record_authority_consumed(storage, "run_1", "auth-default")
        assert event2.payload["consumer_run_id"] == "run_1"
    finally:
        storage.close()


def test_model_validation_strips_and_bounds() -> None:
    m = AuthorityConsumed(
        authority_id="  spaced  ",
        consumer_run_id=" run_1 ",
        consumed_at=datetime.now(UTC),
        via_action_id="  act  ",
    )
    assert m.authority_id == "spaced"
    assert m.consumer_run_id == "run_1"
    assert m.via_action_id == "act"
    with pytest.raises(ValueError):
        AuthorityConsumed(
            authority_id="   ", consumer_run_id="run_1", consumed_at=datetime.now(UTC)
        )
    with pytest.raises(ValueError):
        AuthorityConsumed(authority_id="auth", consumer_run_id="   ", consumed_at=datetime.now(UTC))


def test_payload_is_json_native_and_hash_stable() -> None:
    storage = _make_storage()
    try:
        event = record_authority_consumed(storage, "run_1", "auth-stable")
        dumped = json.dumps(event.payload, sort_keys=True)
        loaded = json.loads(dumped)
        assert loaded == event.payload
        events = list(storage.read_events("run_1"))
        stored = [e for e in events if e.type == EventType.AUTHORITY_CONSUMED][0]
        assert stored.hash == event.hash
        assert stored.digest() == event.digest()
    finally:
        storage.close()
