"""Provenance that survives compaction (issue #294).

Property: checkpoint compaction must not launder untrusted state into trusted.
Every checkpoint component carries its Origin tag; compaction output preserves
per-fact origin rather than single summary trust level. Summaries cannot
upgrade EXTERNAL_AGENT to DETERMINISTIC. Hash-chained provenance survives
checkpoint and compaction, with resolvable links to original chain entries.
"""

from __future__ import annotations

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Origin, Run, StateStatus
from continuum.provenance_map import provenance_for_run
from continuum.recovery.summary import build_informed_retry
from continuum.state.semantic import _weakest_from_state, project, project_incremental
from continuum.state.validator import validate_state
from continuum.storage import SQLiteStorage


def test_weakest_seed_includes_external_dependencies_no_upward_laundering() -> None:
    """Resume-from-checkpoint must clamp exactly like a full re-projection when
    the base's weakest origin came from an external dependency (issue #1373).

    ``_weakest_from_state`` seeds ``_weakest_seen`` from a checkpoint's per-fact
    origins, but it used to skip ``external_dependencies``. A run that declared
    a dependency from a weak source (EXTERNAL_AGENT), checkpointed, then folded a
    later derived fact on resume would stamp that fact at *higher* trust than a
    full re-projection of the same prefix -- breaking the incremental==full
    invariant and the #294 trust-monotonicity guarantee.
    """
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "r",
        EventType.DEPENDENCY_DECLARED,
        {"resource": "api", "version": "1"},
        source=Origin.EXTERNAL_AGENT,
    )
    storage.append_event(
        "r",
        EventType.DECISION_CREATED,
        {"decision_id": "d1", "decision": "ship it", "derived_origin": Origin.DETERMINISTIC.value},
        source=Origin.DETERMINISTIC,
    )
    events = list(storage.read_all_events("r"))

    state_full = project("r", events)
    base = project("r", events[:2])  # checkpoint state after the weak dependency
    state_incr, _ = project_incremental("r", events[2:], base=base)

    # The dependency's EXTERNAL_AGENT origin must be part of the seed.
    assert _weakest_from_state(base) is Origin.EXTERNAL_AGENT
    # Same log prefix -> same trust stamp on the later decision.
    assert state_full.decisions[0].provenance.origin is Origin.EXTERNAL_AGENT
    assert state_incr.decisions[0].provenance.origin is state_full.decisions[0].provenance.origin

    storage.close()


def test_untrusted_fact_survives_compaction_still_requires_review() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event(
        "r", EventType.RUN_STARTED, {"goal": "g", "total": 10}, source=Origin.DETERMINISTIC
    )
    storage.append_event(
        "r",
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "agent claim"},
        source=Origin.EXTERNAL_AGENT,
    )
    storage.append_event(
        "r", EventType.TASK_UPDATED, {"completed": 5, "total": 10}, source=Origin.EXTERNAL_AGENT
    )

    # Force checkpoint and compaction
    mgr = CheckpointManager(storage)
    cp = mgr.checkpoint("r")
    assert cp.state.findings[0].provenance.origin is Origin.EXTERNAL_AGENT
    assert cp.state.progress.provenance.origin is Origin.EXTERNAL_AGENT

    storage.compact_run("r")

    # Live log should be bounded, archive holds history
    live = storage.read_events("r")
    archived = storage.read_archived_events("r")
    assert len(archived) >= 3
    assert any(e.type is EventType.EVENT_LOG_ANCHORED for e in live)

    # Restore via checkpoint + tail
    restored = mgr.restore("r")
    finding = next(f for f in restored.state.findings if f.finding_id == "f1")
    assert finding.provenance.origin is Origin.EXTERNAL_AGENT
    assert finding.provenance.source_sequence == 2
    assert restored.state.progress.provenance.origin is Origin.EXTERNAL_AGENT

    # Validator must still degrade to REQUIRES_REVIEW, cannot clear
    outcome = validate_state(restored.state)
    assert any(
        e.component.value == "finding"
        and e.component_id == "f1"
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )
    assert any(
        e.component.value == "progress" and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )
    assert not outcome.safe

    # Hash-chained provenance survives: verify walks archive + live
    report = storage.verify_events("r")
    assert report.ok
    # Provenance map preserved: source_sequence resolves to archived row
    archived_ids = {e.event_id for e in archived}
    assert finding.provenance.source_event_id in archived_ids
    # Full-history derived provenance still external_agent (no laundering)
    assert provenance_for_run(storage, "r") is Origin.EXTERNAL_AGENT

    storage.close()


def test_no_amplification_after_compaction() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "r",
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "evil"},
        source=Origin.EXTERNAL_AGENT,
    )
    CheckpointManager(storage).checkpoint("r")
    storage.compact_run("r")

    # Add post-compaction deterministic work
    for _i in range(3):
        storage.append_event("r", EventType.WORK_COMPLETED, {}, source=Origin.DETERMINISTIC)

    # Informed retry block derived after compaction must still be unverified

    # Create a dummy validation report with a failing entry to force block creation
    state = project("r", storage.read_all_events("r"))
    outcome = validate_state(state)
    block = build_informed_retry(storage, "r", validation_report=outcome.report)
    assert block is not None
    assert block["derived_origin"] == Origin.EXTERNAL_AGENT.value

    # Attempt to craft a summary that claims deterministic should be clamped
    # Simulate an event that tries to upgrade
    from continuum.events import Event

    malicious = Event(
        run_id="r",
        sequence=storage.last_sequence("r") + 1,
        type=EventType.FINDING_ADDED,
        payload={
            "finding_id": "f2",
            "claim": "laundered",
            "derived_origin": Origin.DETERMINISTIC.value,
        },
        source=Origin.EXTERNAL_AGENT,
    ).sealed()
    # Project with the malicious event; projector must clamp to external_agent
    all_events = list(storage.read_all_events("r")) + [malicious]
    state2 = project("r", all_events)
    f2 = next(f for f in state2.findings if f.finding_id == "f2")
    assert f2.provenance.origin is Origin.EXTERNAL_AGENT

    storage.close()


def test_provenance_map_survives_compaction_and_is_resolvable() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "r",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "e1", "summary": "s"},
        source=Origin.EXTERNAL_AGENT,
    )
    storage.append_event(
        "r",
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "c", "evidence": ["e1"]},
        source=Origin.EXTERNAL_AGENT,
    )
    CheckpointManager(storage).checkpoint("r")
    storage.compact_run("r")

    restored = CheckpointManager(storage).restore("r")
    # Each component's provenance points to archived sequence
    for comp in [*restored.state.evidence, *restored.state.findings]:
        seq = comp.provenance.source_sequence
        assert seq is not None
        # Sequence must be in archived prefix (since we compacted through checkpoint)
        archived_seqs = {e.sequence for e in storage.read_archived_events("r")}
        assert seq in archived_seqs

    # Hash chain still verifies across boundary
    assert storage.verify_events("r").ok

    # Recovery context preserves per-fact origin (not collapsed)
    from continuum.checkpoint.context import build_recovery_context

    ctx = build_recovery_context(restored.state).render()
    # Per-fact tags must appear, not a single summary level
    assert "provenance: external_agent" in ctx
    assert "PROVENANCE MAP" in ctx
    # Each fact's seq is present and resolvable
    for comp in [*restored.state.findings, *restored.state.evidence]:
        assert f"seq:{comp.provenance.source_sequence}" in ctx

    # Deterministic progress must not appear as trusted
    outcome = validate_state(restored.state)
    assert not outcome.safe

    storage.close()


def test_hash_chained_provenance_survives_checkpoint_and_compaction() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r", goal="g"))
    storage.append_event("r", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "r",
        EventType.FINDING_ADDED,
        {"finding_id": "f1", "claim": "x"},
        source=Origin.EXTERNAL_AGENT,
    )
    storage.append_event(
        "r",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "e1", "summary": "s"},
        source=Origin.DETERMINISTIC,
    )
    mgr = CheckpointManager(storage)
    cp = mgr.checkpoint("r")
    # Checkpoint body includes provenance and is hash-sealed
    assert cp.verify()
    # Tampering with provenance would break checkpoint hash
    tampered = cp.state.findings[0].model_copy(
        update={
            "provenance": cp.state.findings[0].provenance.model_copy(
                update={"origin": Origin.DETERMINISTIC}
            )
        }
    )
    tampered_state = cp.state.model_copy(
        update={"findings": [tampered] + list(cp.state.findings[1:])}
    )
    tampered_cp = cp.model_copy(update={"state": tampered_state})
    assert not tampered_cp.verify()

    storage.compact_run("r")
    # After compaction, hashes still chain across archive boundary
    assert storage.verify_events("r").ok
    # Editing archived provenance should be detected
    with storage._lock:
        storage._connection.execute(
            "UPDATE events_archive SET source = 'deterministic' WHERE sequence = 2"
        )
    assert not storage.verify_events("r").ok
    storage.close()
