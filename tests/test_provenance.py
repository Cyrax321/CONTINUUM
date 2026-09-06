"""Provenance: who asserted a fact, and what that permits.

The property under test is narrow and load-bearing: **state an agent asserted
about its own work can never, on its own, establish that a run is safe to
resume.** Before provenance was carried on events, an agent could report 9,999
of 10,000 documents complete, checkpoint, ask CONTINUUM whether it was safe to
continue, and be told yes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import Event, EventLog, EventType
from continuum.mcp.authz import AuthorizationPolicy
from continuum.mcp.server import build_server
from continuum.models import Goal, Origin, Progress, RecoveryMode, Run, StateStatus
from continuum.recovery import RecoveryEngine
from continuum.state.semantic import project
from continuum.state.validator import validate_state
from continuum.storage import SQLiteStorage
from tests.mcp_helpers import fake_context


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    yield storage
    storage.close()


MCP_CLIENT = "pytest-client"


async def call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(name, arguments, context=fake_context(MCP_CLIENT))
    return json.loads(result.content[0].text)


# --- the origin vocabulary -------------------------------------------------- #


def test_self_certified_origins_are_exactly_the_unverified_ones() -> None:
    assert Origin.EXTERNAL_AGENT.self_certified
    assert Origin.LLM.self_certified
    assert Origin.IMPORTED.self_certified
    assert not Origin.DETERMINISTIC.self_certified
    assert not Origin.HUMAN.self_certified


# --- the event carries it, and it is signed --------------------------------- #


def test_source_defaults_to_deterministic() -> None:
    assert Event(run_id="r", sequence=1, type=EventType.RUN_STARTED).source is (
        Origin.DETERMINISTIC
    )


def test_source_is_covered_by_the_hash() -> None:
    """A trust marker outside the digest could be edited undetected."""
    base = Event(run_id="r", sequence=1, type=EventType.RUN_STARTED).sealed()
    forged = base.model_copy(update={"source": Origin.DETERMINISTIC})
    escalated = base.model_copy(update={"source": Origin.EXTERNAL_AGENT})

    assert base.digest() == forged.digest()
    assert base.digest() != escalated.digest()


def test_tampering_with_source_breaks_verification() -> None:
    log = EventLog()
    log.append("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.EXTERNAL_AGENT)
    assert log.verify("r").ok

    # promote an agent's claim to trusted, leaving the stored hash intact
    log._by_run["r"][0] = log._by_run["r"][0].model_copy(update={"source": Origin.DETERMINISTIC})
    report = log.verify("r")
    assert not report.ok
    assert any(v.kind == "TAMPERED_CONTENT" for v in report.violations)


def test_source_survives_a_storage_round_trip(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="r", goal="g"))
    store.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.EXTERNAL_AGENT)
    assert store.read_events("r")[0].source is Origin.EXTERNAL_AGENT
    assert store.verify_events("r").ok


# --- the projector carries it forward --------------------------------------- #


def test_projection_inherits_the_event_source(store: SQLiteStorage) -> None:
    """Folding a self-report faithfully still yields a self-report."""
    store.create_run(Run(run_id="r", goal="g"))
    store.append_event(
        "r", EventType.RUN_STARTED, {"goal": "g", "total": 10}, source=Origin.EXTERNAL_AGENT
    )
    store.append_event(
        "r",
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "agent asserts this"},
        source=Origin.EXTERNAL_AGENT,
    )
    state = project("r", store.read_events("r"))

    assert state.goal.provenance.origin is Origin.EXTERNAL_AGENT
    assert state.progress.provenance.origin is Origin.EXTERNAL_AGENT
    assert state.findings[0].provenance.origin is Origin.EXTERNAL_AGENT


def test_deterministic_events_stay_deterministic(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="r", goal="g"))
    store.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 10})
    store.append_event("r", EventType.WORK_COMPLETED, {})
    state = project("r", store.read_events("r"))

    assert state.goal.provenance.origin is Origin.DETERMINISTIC
    assert state.progress.provenance.origin is Origin.DETERMINISTIC


def test_progress_stays_tainted_once_an_agent_contributes(store: SQLiteStorage) -> None:
    """Progress is cumulative, so the weakest contributor wins.

    A trusted event appended after an agent's self-report does not launder the
    running total, the agent's contribution is still inside it.
    """
    store.create_run(Run(run_id="r", goal="g"))
    store.append_event("r", EventType.RUN_STARTED, {"goal": "g", "total": 100})
    store.append_event("r", EventType.TASK_UPDATED, {"completed": 50}, source=Origin.EXTERNAL_AGENT)
    store.append_event("r", EventType.WORK_COMPLETED, {})  # deterministic

    state = project("r", store.read_events("r"))
    assert state.progress.completed == 51
    assert state.progress.provenance.origin is Origin.EXTERNAL_AGENT


# --- the validator acts on it ----------------------------------------------- #


def test_self_certified_progress_requires_review() -> None:
    state = project("r", _agent_events("r"))
    outcome = validate_state(state)
    statuses = {(e.component.value, e.status) for e in outcome.report.statuses}
    assert ("progress", StateStatus.REQUIRES_REVIEW) in statuses
    assert ("goal", StateStatus.REQUIRES_REVIEW) in statuses
    assert not outcome.safe


def _agent_events(run_id: str) -> list[Event]:
    log = EventLog()
    log.append(
        run_id,
        EventType.RUN_STARTED,
        {"goal": "Analyze 10,000 docs", "total": 10_000},
        source=Origin.EXTERNAL_AGENT,
    )
    log.append(run_id, EventType.TASK_UPDATED, {"completed": 9999}, source=Origin.EXTERNAL_AGENT)
    return list(log.events(run_id))


def test_deterministic_state_is_still_reported_valid() -> None:
    """The gate must not degrade into refusing everything."""
    log = EventLog()
    log.append("r", EventType.RUN_STARTED, {"goal": "g", "total": 10})
    log.append("r", EventType.WORK_COMPLETED, {})
    outcome = validate_state(project("r", log.events("r")))

    statuses = {(e.component.value, e.status) for e in outcome.report.statuses}
    assert ("progress", StateStatus.VALID) in statuses
    assert ("goal", StateStatus.VALID) in statuses
    assert outcome.safe


def test_a_state_object_built_by_hand_is_trusted_by_default() -> None:
    """Constructing state in Python is trusted code, not a remote assertion."""
    from continuum.models import SemanticState

    outcome = validate_state(
        SemanticState(
            run_id="r",
            goal=Goal(description="g"),
            progress=Progress(completed=5),
            source_sequence=6,  # 0 would trip the unrelated "no source events" check
        )
    )
    assert outcome.safe


# --- end to end: the original exploit --------------------------------------- #


@pytest.mark.asyncio
async def test_an_agent_cannot_certify_its_own_fabricated_progress(
    store: SQLiteStorage,
) -> None:
    """The exact reproduction that previously returned mode=resume, safe=True."""
    server, ctx = build_server(storage=store, policy=AuthorizationPolicy([MCP_CLIENT]))

    await call(
        server,
        "continuum_record_progress",
        run_id="r",
        completed=9999,
        total=10_000,
        goal="Analyze 10,000 docs",
    )
    await call(server, "continuum_checkpoint", run_id="r")
    decision = await call(server, "continuum_resume", run_id="r")

    assert decision["mode"] != RecoveryMode.RESUME.value
    assert decision["safe"] is False
    assert decision["next_allowed_action"] is not None


@pytest.mark.asyncio
async def test_the_same_run_written_deterministically_does_resume(
    store: SQLiteStorage,
) -> None:
    """Identical figures, trusted writer: the difference is provenance alone."""
    store.create_run(Run(run_id="r", goal="Analyze 10,000 docs"))
    store.append_event("r", EventType.RUN_STARTED, {"goal": "Analyze 10,000 docs", "total": 10_000})
    store.append_event("r", EventType.TASK_UPDATED, {"completed": 9999, "pending": 1})
    CheckpointManager(store).checkpoint("r", environment=capture("r", StaticProvider(dataset="v3")))

    decision = RecoveryEngine(store).assess(
        "r", current_environment=capture("r", StaticProvider(dataset="v3"))
    )
    assert decision.mode is RecoveryMode.RESUME
    assert decision.safe
