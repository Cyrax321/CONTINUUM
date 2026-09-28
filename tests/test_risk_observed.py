"""RISK_OBSERVED payload schema, provenance and projection (issue #1421).

The event type itself and EXTERNAL_MONITOR provenance predate this change
(issues #303/#563). What #1421 adds is the typed payload schema, the fold into
``SemanticState.observed_risks``, and the invariant that an observation is
knowledge rather than a change: folding one must not mint a semantic version.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from continuum.events import EventLog, EventType
from continuum.models import (
    ObservedRisk,
    Origin,
    RiskObservedPayload,
    Run,
    utcnow,
)
from continuum.recovery.risk import ingest_risk
from continuum.state.semantic import ProjectionError, project, project_incremental
from continuum.state.versioning import VersionChain, canonical_state_json, state_fingerprint
from continuum.storage import SQLiteStorage


def started(log: EventLog, **payload: object) -> EventLog:
    log.append("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 3, **payload})
    return log


def risk_event(log: EventLog, **payload: object) -> EventLog:
    merged: dict[str, object] = {"trigger": "loop", "score": 0.8, **payload}
    log.append("run_1", EventType.RISK_OBSERVED, merged, source=Origin.EXTERNAL_MONITOR)
    return log


# --- the typed payload schema --------------------------------------------- #


def test_schema_accepts_an_epoch_timestamp() -> None:
    schema = RiskObservedPayload(trigger="meltdown", score=1.0, ts=1577880000)
    assert schema.ts.year == 2020
    assert schema.ts.tzinfo is not None


def test_schema_accepts_an_epoch_string_and_iso() -> None:
    assert RiskObservedPayload(trigger="loop", ts="1577880000").ts.year == 2020
    assert RiskObservedPayload(trigger="loop", ts="2020-01-01T00:00:00+00:00").ts.year == 2020


def test_schema_normalises_trigger_and_clamps_score() -> None:
    schema = RiskObservedPayload(trigger="  ERROR_CASCADE ", score=1.7)
    assert schema.trigger == "error_cascade"
    assert schema.score == 1.0
    assert RiskObservedPayload(trigger="loop", score=-0.9).score == 0.0


def test_schema_keeps_structured_detail_and_wraps_a_legacy_string() -> None:
    structured = RiskObservedPayload(
        trigger="token_runaway", detail={"budget": "1M", "used": 900_000}
    )
    assert structured.detail == {"budget": "1M", "used": 900_000}

    legacy = RiskObservedPayload(trigger="token_runaway", detail="budget blown")
    assert legacy.detail == {"message": "budget blown"}

    assert RiskObservedPayload(trigger="token_runaway").detail == {}


def test_schema_bounds_detail_size() -> None:
    schema = RiskObservedPayload(
        trigger="loop",
        detail={f"key_{i}": "x" * 1024 for i in range(64)},
    )
    assert len(schema.detail) == 32
    assert all(len(v) == 512 for v in schema.detail.values())


def test_schema_rejects_an_unnamed_risk_class() -> None:
    with pytest.raises(ValidationError):
        RiskObservedPayload(trigger="", score=0.5)
    with pytest.raises(ValidationError):
        RiskObservedPayload(score=0.5)


def test_schema_tolerates_non_text_identifiers() -> None:
    schema = RiskObservedPayload(trigger="loop", episode_id=44, step_id=7)
    assert schema.episode_id == "44"
    assert schema.step_id == "7"


# --- provenance can never be self-certified ------------------------------- #


def test_ingestion_always_stamps_external_monitor(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_provenance.db")
    with SQLiteStorage(db) as store:
        run_id = "run_provenance"
        store.create_run_started(Run(run_id=run_id, goal="provenance"))
        # The payload has no way to influence source: the writer decides it.
        ingest_risk(store, run_id, {"trigger": "meltdown", "score": 0.9})
        event = store.read_events(run_id)[-1]
        assert event.type is EventType.RISK_OBSERVED
        assert event.source is Origin.EXTERNAL_MONITOR
        assert event.source.self_certified is False


def test_ingestion_writes_a_payload_the_schema_validates(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_roundtrip.db")
    with SQLiteStorage(db) as store:
        run_id = "run_roundtrip"
        store.create_run_started(Run(run_id=run_id, goal="roundtrip"))
        ingest_risk(
            store,
            run_id,
            {
                "trigger": "Latency_Anomaly",
                "score": 2.5,
                "episode_id": "ep_1",
                "step_id": "step_7",
                "detail": {"p99_ms": 4200},
                "ts": 1577880000,
            },
        )
        event = store.read_events(run_id)[-1]
        assert event.payload["trigger"] == "latency_anomaly"
        assert event.payload["score"] == 1.0
        assert event.payload["detail"] == {"p99_ms": 4200}
        # The event payload stays JSON-native and schema-shaped.
        assert RiskObservedPayload(**event.payload).trigger == "latency_anomaly"


def test_ingestion_keeps_a_monitor_supplied_epoch_ts(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_epoch.db")
    with SQLiteStorage(db) as store:
        run_id = "run_epoch"
        store.create_run_started(Run(run_id=run_id, goal="epoch"))
        ingest_risk(store, run_id, {"trigger": "loop", "ts": 1577880000})
        assert store.read_events(run_id)[-1].payload["ts"].startswith("2020-01-01")


def test_ingestion_drops_an_unnamed_risk_but_keeps_a_bad_timestamp(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_tolerance.db")
    with SQLiteStorage(db) as store:
        run_id = "run_tolerance"
        store.create_run_started(Run(run_id=run_id, goal="tolerance"))
        assert ingest_risk(store, run_id, {"score": 0.5}) is False
        assert len(store.read_events(run_id)) == 1

        # An unparseable ts re-dates the observation rather than losing it.
        assert ingest_risk(store, run_id, {"trigger": "loop", "ts": "yesterday"}) is True
        event = store.read_events(run_id)[-1]
        assert event.payload["trigger"] == "loop"
        assert "ts" in event.payload


# --- the fold ------------------------------------------------------------- #


def test_projection_records_observed_risks() -> None:
    log = risk_event(started(EventLog()), score=0.6, detail={"again": True})
    state = project("run_1", log.events("run_1"))
    assert len(state.observed_risks) == 1
    risk = state.observed_risks[0]
    assert isinstance(risk, ObservedRisk)
    assert risk.trigger == "loop"
    assert risk.score == 0.6
    assert risk.detail == {"again": True}
    assert risk.provenance.origin is Origin.EXTERNAL_MONITOR
    assert risk.ts is not None


def test_projection_accumulates_risks_in_order() -> None:
    log = started(EventLog())
    risk_event(log, trigger="latency_anomaly")
    risk_event(log, trigger="meltdown")
    state = project("run_1", log.events("run_1"))
    assert [r.trigger for r in state.observed_risks] == ["latency_anomaly", "meltdown"]


def test_incremental_projection_carries_observed_risks() -> None:
    log = started(EventLog())
    risk_event(log, trigger="loop")
    base, _ = project_incremental("run_1", log.events("run_1"))
    assert len(base.observed_risks) == 1

    new_event = log.append("run_1", EventType.WORK_COMPLETED, {})
    advanced, _ = project_incremental("run_1", [new_event], base=base)
    assert len(advanced.observed_risks) == 1
    assert advanced.progress.completed == 1
    # The two paths agree with a full re-projection of the same prefix.
    assert project("run_1", log.events("run_1")).observed_risks == advanced.observed_risks


def test_projection_tolerates_a_legacy_string_detail_and_garbage_ts() -> None:
    log = started(EventLog())
    log.append(
        "run_1",
        EventType.RISK_OBSERVED,
        {"trigger": "loop", "score": 1.4, "detail": "repetition", "ts": "yesterday"},
        source=Origin.EXTERNAL_MONITOR,
    )
    events = log.events("run_1")
    state = project("run_1", events)
    risk = state.observed_risks[0]
    assert risk.detail == {"message": "repetition"}
    assert risk.score == 1.0
    # Re-dated to the event's own timestamp, so a re-projection reproduces.
    assert risk.ts == events[-1].timestamp


def test_projection_refuses_an_unnamed_risk_class() -> None:
    log = started(EventLog())
    log.append("run_1", EventType.RISK_OBSERVED, {"score": 0.5})
    with pytest.raises(ProjectionError):
        project("run_1", log.events("run_1"))

    degraded, _ = project_incremental("run_1", log.events("run_1"), on_unprojectable="degrade")
    assert degraded.observed_risks == []


def test_risk_is_reported_as_applied_not_ignored() -> None:
    log = risk_event(started(EventLog()))
    _state, report = project_incremental("run_1", log.events("run_1"))
    assert report.applied == 2
    assert "RISK_OBSERVED" not in report.ignored_types
    assert report.complete is True


# --- an observation is knowledge, not a change ---------------------------- #


def test_a_risk_observation_does_not_mint_a_version() -> None:
    log = started(EventLog())
    chain = VersionChain("run_1")
    chain.commit(project("run_1", log.events("run_1")), reason="start")

    risk_event(log, trigger="meltdown", score=0.99)
    # The state gained a risk, but the fingerprint did not move.
    assert state_fingerprint(project("run_1", log.events("run_1"))) == chain.head.fingerprint
    assert chain.commit(project("run_1", log.events("run_1")), reason="risk") is None
    assert [e.version for e in chain] == [0]


def test_mitigation_that_alters_the_run_still_mints_a_version() -> None:
    log = started(EventLog())
    chain = VersionChain("run_1")
    chain.commit(project("run_1", log.events("run_1")), reason="start")

    risk_event(log, trigger="meltdown")
    log.append("run_1", EventType.WORK_COMPLETED, {})
    entry = chain.commit(project("run_1", log.events("run_1")), reason="mitigated")
    assert entry is not None
    assert entry.version == 1
    assert entry.state.progress.completed == 1
    assert len(entry.state.observed_risks) == 1


def test_observed_risks_leave_the_persisted_body_and_fingerprint_alone() -> None:
    log = risk_event(started(EventLog()))
    state = project("run_1", log.events("run_1"))
    assert len(state.observed_risks) == 1

    without = state.model_copy(update={"observed_risks": []})
    assert state_fingerprint(state) == state_fingerprint(without)

    body = canonical_state_json(state)
    assert "observed_risks" not in body
    # A reader built before #1421 can still load what was written.
    assert state.__class__.model_validate_json(body).observed_risks == []


def test_a_checkpoint_digest_ignores_observed_risks() -> None:
    from continuum.models import StateCheckpoint

    log = risk_event(started(EventLog()))
    state = project("run_1", log.events("run_1"))
    stamped = utcnow()
    common = {"run_id": "run_1", "created_at": stamped, "checkpoint_id": "ck_1"}
    checkpoint = StateCheckpoint(state=state, **common).sealed()

    stripped = state.model_copy(update={"observed_risks": []})
    stripped_checkpoint = StateCheckpoint(state=stripped, **common).sealed()
    assert checkpoint.integrity_hash == stripped_checkpoint.integrity_hash


def test_observed_risk_provenance_survives_a_weaker_neighbour() -> None:
    # An agent-reported event after the risk must not launder the risk's
    # provenance: the observation stays attributed to the monitor.
    log = started(EventLog())
    risk_event(log, trigger="loop")
    log.append("run_1", EventType.TOOL_CALLED, {"tool": "search"})
    state = project("run_1", log.events("run_1"))
    assert state.observed_risks[0].provenance.origin is Origin.EXTERNAL_MONITOR


def test_now_defaults_to_a_aware_timestamp() -> None:
    schema = RiskObservedPayload(trigger="loop")
    assert schema.ts.tzinfo is not None
    assert abs((utcnow() - schema.ts).total_seconds()) < 5
