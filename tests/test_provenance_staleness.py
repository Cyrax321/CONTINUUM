"""N-hop staleness propagation via caused_by DAG (issue #553)."""

from __future__ import annotations

from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import Run
from continuum.provenance.graph import build_provenance_graph
from continuum.recovery import RecoveryEngine
from continuum.state.validator import validate_state
from continuum.storage.sqlite import SQLiteStorage


def test_n_hop_staleness_via_caused_by():
    """Evidence -> decision -> action chain, invalidation propagates 2 hops via caused_by."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_stale_n_hop"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    # Create dependency and evidence
    storage.append_event(
        run_id, EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v1"}
    )
    ev = storage.append_event(
        run_id,
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "ev1", "summary": "s", "source": "dataset"},
    )
    # Decision caused by evidence
    dec = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d1", "decision_id": "dec1", "caused_by": [ev.event_id]},
    )
    # Action caused by decision
    act = storage.append_event(
        run_id,
        EventType.ACTION_RECORDED,
        {
            "action_type": "send",
            "action_id": "act1",
            "caused_by": [dec.event_id],
            "action": {"action_id": "act1"},
        },
    )
    events = storage.read_events(run_id)
    # Build state via project (implicitly via validator's state)
    from continuum.state.semantic import project

    state = project(run_id, events)
    # Check graph has edges
    graph = build_provenance_graph(events)
    assert dec.event_id in graph.edges.get(ev.event_id, [])
    assert act.event_id in graph.edges.get(dec.event_id, [])

    # Validate with changed env and events
    env_checkpoint = capture(run_id, StaticProvider(dataset="v1"))
    env_current = capture(run_id, StaticProvider(dataset="v2"))
    # Need to set dependency in state: project should have dependency from DEPENDENCY_DECLARED
    # The state's external_dependencies should have dataset v1
    # Validate
    outcome = validate_state(
        state,
        checkpoint_environment=env_checkpoint,
        current_environment=env_current,
        events=events,
    )
    # Evidence should be stale
    from continuum.models import Component, StateStatus

    def status_for(comp, cid):
        for e in outcome.report.statuses:
            if e.component is comp and e.component_id == cid:
                return e.status
        return None

    assert status_for(Component.EVIDENCE, "ev1") is StateStatus.STALE
    # Decision should be stale via N-hop
    assert status_for(Component.DECISION, "dec1") is StateStatus.STALE
    # Action should be stale as well (via validation entry for Component.ACTION)
    assert status_for(Component.ACTION, "act1") is StateStatus.STALE


def test_three_hop_chain():
    """3-hop chain: ev -> dec1 -> dec2 -> act, all should go stale."""
    storage = SQLiteStorage(":memory:")
    run_id = "run_three_hop"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    storage.append_event(
        run_id, EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v1"}
    )
    ev = storage.append_event(
        run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "source": "dataset"}
    )
    dec1 = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d1", "decision_id": "dec1", "caused_by": [ev.event_id]},
    )
    dec2 = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d2", "decision_id": "dec2", "caused_by": [dec1.event_id]},
    )
    _act = storage.append_event(
        run_id,
        EventType.ACTION_RECORDED,
        {"action_type": "send", "action_id": "act1", "caused_by": [dec2.event_id]},
    )
    events = storage.read_events(run_id)
    from continuum.state.semantic import project

    state = project(run_id, events)
    env_checkpoint = capture(run_id, StaticProvider(dataset="v1"))
    env_current = capture(run_id, StaticProvider(dataset="v2"))
    outcome = validate_state(
        state,
        checkpoint_environment=env_checkpoint,
        current_environment=env_current,
        events=events,
    )
    from continuum.models import Component, StateStatus

    def status_for(comp, cid):
        for e in outcome.report.statuses:
            if e.component is comp and e.component_id == cid:
                return e.status
        return None

    assert status_for(Component.DECISION, "dec1") is StateStatus.STALE
    assert status_for(Component.DECISION, "dec2") is StateStatus.STALE
    assert status_for(Component.ACTION, "act1") is StateStatus.STALE


def test_engine_propagates_to_contract(tmp_path):
    """Engine surfaces N-hop staleness in RecoveryContract.invalidated."""

    db = str(tmp_path / "engine.db")
    storage = SQLiteStorage(db)
    run_id = "run_engine_stale"
    storage.create_run_started(Run(run_id=run_id, goal="g"))
    storage.append_event(
        run_id, EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v1"}
    )
    ev = storage.append_event(
        run_id, EventType.EVIDENCE_ADDED, {"evidence_id": "ev1", "source": "dataset"}
    )
    _dec = storage.append_event(
        run_id,
        EventType.DECISION_CREATED,
        {"decision": "d1", "decision_id": "dec1", "caused_by": [ev.event_id]},
    )
    storage.close()
    from continuum.checkpoint import CheckpointManager

    storage2 = SQLiteStorage(db)
    _mgr = CheckpointManager(storage2)
    from continuum.state.semantic import project

    state = project(run_id, storage2.read_events(run_id))
    _env_v1 = capture(run_id, StaticProvider(dataset="v1"))
    _mgr.checkpoint(run_id, state=state, environment=_env_v1)
    engine = RecoveryEngine(storage2)
    env_current = capture(run_id, StaticProvider(dataset="v2"))
    decision = engine.assess(run_id, current_environment=env_current)
    # Contract should have invalidated containing dec1
    assert "dec1" in str(decision.contract.invalidated) or any(
        "dec1" in x for x in decision.contract.invalidated
    )
    assert not decision.safe
