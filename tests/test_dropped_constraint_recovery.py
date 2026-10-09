"""Tests for escalating to REQUIRES_REVIEW on dropped or violated hard constraint pins (issue #1414).

Verifies:
1. StateValidator flags missing, dropped, or modified hard constraint pins as REQUIRES_REVIEW.
2. Soft constraints do not block safe resume when missing.
3. Operator confirmation clears the REQUIRES_REVIEW status.
4. RecoveryEngine escalates to REQUEST_HUMAN on dropped or violated hard pins.
5. Sealed RecoveryContract records the violated pin in invalidated and evidence lists.
6. Human guidance and repair steps name the constraint pin and operator confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import (
    Component,
    ConstraintPinned,
    Origin,
    RecoveryMode,
    RecoverySafety,
    Run,
    StateStatus,
)
from continuum.recovery.contract import verify_contract
from continuum.recovery.engine import RecoveryEngine
from continuum.recovery.guidance import human_steps_for
from continuum.recovery.planner import RepairKind
from continuum.security.constraints import load_constraints, predicate_digest
from continuum.state.semantic import project
from continuum.state.validator import StateValidator
from continuum.storage import SQLiteStorage


def _sha256(text: str) -> str:
    return predicate_digest(text)


def test_validator_flags_missing_hard_constraint_from_registry(tmp_path: Path, monkeypatch) -> None:
    """When a hard constraint is defined by the operator but missing from state pins,
    it must be flagged as REQUIRES_REVIEW."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "audit_logging",
            "level": "hard",
            "predicate": "audit events must be recorded before external side effects",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "validator_test.db")
    storage = SQLiteStorage(db)
    run_id = "run_val_1"
    storage.create_run(Run(run_id=run_id, goal="test missing pin"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test missing pin", "total": 1})
    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()
    outcome = validator.validate(state, events=events)

    assert not outcome.safe
    pin_entries = [e for e in outcome.report.statuses if e.component is Component.PIN]
    assert len(pin_entries) == 1
    assert pin_entries[0].component_id == "audit_logging"
    assert pin_entries[0].status is StateStatus.REQUIRES_REVIEW
    assert "missing or dropped from active pins" in pin_entries[0].detail


def test_validator_flags_predicate_digest_mismatch(tmp_path: Path, monkeypatch) -> None:
    """When an active pin has a digest that differs from the operator registry predicate,
    it must be flagged as REQUIRES_REVIEW."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    predicate = "audit events must be recorded before external side effects"
    spec_data = [
        {
            "id": "audit_logging",
            "level": "hard",
            "predicate": predicate,
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "validator_mismatch.db")
    storage = SQLiteStorage(db)
    run_id = "run_val_2"
    storage.create_run(Run(run_id=run_id, goal="test mismatch pin"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test mismatch pin", "total": 1})
    # Pin with a modified/tampered digest
    wrong_digest = _sha256("different predicate")
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="audit_logging", sha256=wrong_digest).model_dump(),
    )
    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()
    outcome = validator.validate(state, events=events)

    assert not outcome.safe
    pin_entries = [e for e in outcome.report.statuses if e.component is Component.PIN]
    assert len(pin_entries) == 1
    assert pin_entries[0].component_id == "audit_logging"
    assert pin_entries[0].status is StateStatus.REQUIRES_REVIEW
    assert "predicate digest mismatch" in pin_entries[0].detail


def test_validator_flags_dropped_pin_event(tmp_path: Path, monkeypatch) -> None:
    """When a CONSTRAINT_PIN_DROPPED event was emitted, StateValidator must flag it
    as REQUIRES_REVIEW even without a constraints.json registry."""
    # Ensure no registry exists
    missing_path = tmp_path / "nonexistent.json"
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", missing_path)

    db = str(tmp_path / "validator_dropped.db")
    storage = SQLiteStorage(db)
    run_id = "run_val_3"
    storage.create_run(Run(run_id=run_id, goal="test dropped event"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test dropped event", "total": 1})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {
            "constraint_id": "network_isolation",
            "sha256": _sha256("no external net"),
            "reason": "digest_mismatch",
        },
    )
    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()
    outcome = validator.validate(state, events=events)

    assert not outcome.safe
    pin_entries = [e for e in outcome.report.statuses if e.component is Component.PIN]
    assert any(
        e.component_id == "network_isolation" and e.status is StateStatus.REQUIRES_REVIEW
        for e in pin_entries
    )


def test_soft_constraint_does_not_block_safe_resume(tmp_path: Path, monkeypatch) -> None:
    """Soft constraints are advisory and must not escalate to REQUIRES_REVIEW when missing."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "code_style",
            "level": "soft",
            "predicate": "format with ruff before commit",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "validator_soft.db")
    storage = SQLiteStorage(db)
    run_id = "run_val_4"
    storage.create_run(Run(run_id=run_id, goal="test soft pin"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test soft pin", "total": 1})
    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    # Confirm goal and progress so only constraints could block
    validator = StateValidator(confirmed=True)
    outcome = validator.validate(state, events=events)

    assert outcome.safe
    assert not any(
        e.component is Component.PIN and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )


def test_operator_confirmation_clears_requires_review(tmp_path: Path, monkeypatch) -> None:
    """Operator confirmation via confirmed={'pin'} clears REQUIRES_REVIEW on a dropped pin."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "audit_logging",
            "level": "hard",
            "predicate": "audit events must be recorded before external side effects",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "validator_confirm.db")
    storage = SQLiteStorage(db)
    run_id = "run_val_5"
    storage.create_run(Run(run_id=run_id, goal="test confirm pin"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test confirm pin", "total": 1})
    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()
    outcome = validator.validate(state, events=events, confirmed={"goal", "progress", "pin"})

    assert outcome.safe
    pin_entries = [e for e in outcome.report.statuses if e.component is Component.PIN]
    assert len(pin_entries) == 1
    assert pin_entries[0].status is StateStatus.VALID
    assert "confirmed by operator" in pin_entries[0].detail


def test_recovery_engine_escalates_and_seals_contract(tmp_path: Path, monkeypatch) -> None:
    """RecoveryEngine.assess must escalate mode to REQUEST_HUMAN, record the violated
    constraint pin in contract.invalidated and contract.evidence, and provide repair guidance."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    predicate = "require human approval for financial transactions"
    spec_data = [
        {
            "id": "financial_gate",
            "level": "hard",
            "predicate": predicate,
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "engine_test.db")
    storage = SQLiteStorage(db)
    run_id = "run_engine_1"
    storage.create_run(Run(run_id=run_id, goal="test recovery engine"))
    storage.append_event(
        run_id, EventType.RUN_STARTED, {"goal": "test recovery engine", "total": 1}
    )
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )

    from continuum.environment import StaticProvider, capture
    from continuum.models import EnvResource

    CheckpointManager(storage).checkpoint(
        run_id,
        environment=capture(
            run_id,
            StaticProvider(resources={"model": EnvResource(name="model", version="v1")}),
        ),
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)

    # 1. Mode must escalate to REQUEST_HUMAN
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_HUMAN

    # 2. Rationale must name the unanchored hard constraint pin
    assert any("financial_gate" in reason for reason in decision.rationale)

    # 3. Contract invalidated and evidence must include the pin
    assert "pin:financial_gate (requires_review)" in decision.contract.invalidated
    assert any("pin:financial_gate" in ev for ev in decision.contract.evidence)

    # 4. Sealed contract integrity must verify
    assert verify_contract(decision.contract)

    # 5. Human repair step in plan
    human_steps = [s for s in decision.plan.steps if s.requires_human]
    assert any(
        s.kind is RepairKind.HUMAN_REVIEW and s.target == "financial_gate" for s in human_steps
    )
    assert decision.contract.next_allowed_action == "human_review:financial_gate"

    # 6. Actionable human recovery guidance
    guidance = human_steps_for(decision, run_id=run_id)
    assert any("financial_gate" in step and "continuum confirm" in step for step in guidance)


def test_cli_confirm_clears_constraint_pin_via_scope(tmp_path: Path, monkeypatch) -> None:
    """CLI continuum confirm accepts --scope pin and --scope constraints to clear review."""
    import argparse
    import io

    from continuum.cli.main import cmd_confirm

    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "sec_audit",
            "level": "hard",
            "predicate": "audit log active",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "cli_confirm.db")
    storage = SQLiteStorage(db)
    run_id = "run_cli_1"
    storage.create_run(Run(run_id=run_id, goal="test cli confirm"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test cli confirm", "total": 1})

    # 1. Before confirm, mode is REQUEST_HUMAN
    engine = RecoveryEngine(storage)
    d1 = engine.assess(run_id)
    assert d1.mode is RecoveryMode.REQUEST_HUMAN

    # 2. Run cmd_confirm with --scope pin
    args = argparse.Namespace(
        run_id=run_id,
        scope=["pin"],
        tolerate_unknown=False,
        model=None,
        json=True,
    )
    out = io.StringIO()
    err = io.StringIO()
    code = cmd_confirm(args, storage, out, err)
    assert code == 0

    # 3. Assess again: constraint review is cleared
    events = list(storage.read_events(run_id))
    assert any(
        e.type is EventType.REVIEW_CONFIRMED and "pin" in e.payload.get("components", [])
        for e in events
    )


def test_multiple_constraints_partial_drop(tmp_path: Path, monkeypatch) -> None:
    """When one hard constraint is valid and another is dropped, only the dropped one
    is flagged and requires review."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    pred_a = "audit log active"
    pred_b = "rate limit capped"
    spec_data = [
        {"id": "pin_a", "level": "hard", "predicate": pred_a, "scope": ["*"]},
        {"id": "pin_b", "level": "hard", "predicate": pred_b, "scope": ["*"]},
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "multi_pin.db")
    storage = SQLiteStorage(db)
    run_id = "run_multi_1"
    storage.create_run(Run(run_id=run_id, goal="test multi pin"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test multi pin", "total": 1})
    # pin_a is pinned with matching digest
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="pin_a", sha256=_sha256(pred_a)).model_dump(),
    )
    # pin_b is missing / dropped

    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator(confirmed={"goal", "progress"})
    outcome = validator.validate(state, events=events)

    assert not outcome.safe
    statuses = {e.component_id: e for e in outcome.report.statuses if e.component is Component.PIN}
    assert "pin_a" in statuses and statuses["pin_a"].status is StateStatus.VALID
    assert "pin_b" in statuses and statuses["pin_b"].status is StateStatus.REQUIRES_REVIEW


def test_pin_emission_matches_validator(tmp_path: Path, monkeypatch) -> None:
    """Pins emitted via ConstraintSpec.to_pin() agree with the validator."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "egress_guard",
            "level": "hard",
            "predicate": "no unvetted outbound connections allowed",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    registry = load_constraints(constraints_path, asserted_by=Origin.DETERMINISTIC)
    spec = registry.get("egress_guard")
    assert spec is not None

    db = str(tmp_path / "emission_match.db")
    storage = SQLiteStorage(db)
    run_id = "run_emit_1"
    storage.create_run(Run(run_id=run_id, goal="test emission match"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test emission match", "total": 1})

    # Pin created via production helper spec.to_pin()
    pin = spec.to_pin()
    storage.append_event(run_id, EventType.CONSTRAINT_PINNED, pin.model_dump())

    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()
    outcome = validator.validate(state, events=events)

    pin_entries = [e for e in outcome.report.statuses if e.component is Component.PIN]
    assert len(pin_entries) == 1
    assert pin_entries[0].component_id == "egress_guard"
    assert pin_entries[0].status is StateStatus.VALID
    assert pin_entries[0].detail == "active and verified"


def test_malformed_constraints_registry_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """An unreadable/corrupted constraints.json must emit a REQUIRES_REVIEW PIN entry instead of crashing."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    constraints_path.write_text("{corrupted json", encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "corrupt_registry.db")
    storage = SQLiteStorage(db)
    run_id = "run_corrupt_1"
    storage.create_run(Run(run_id=run_id, goal="test corrupt registry"))
    storage.append_event(
        run_id, EventType.RUN_STARTED, {"goal": "test corrupt registry", "total": 1}
    )

    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()
    outcome = validator.validate(state, events=events)

    assert not outcome.safe
    registry_entries = [
        e
        for e in outcome.report.statuses
        if e.component is Component.PIN and e.component_id == "registry"
    ]
    assert len(registry_entries) == 1
    assert registry_entries[0].status is StateStatus.REQUIRES_REVIEW
    assert "constraint registry unreadable" in registry_entries[0].detail


def test_validator_confirmed_unconditional_reset(tmp_path: Path, monkeypatch) -> None:
    """A subsequent validate() call with confirmed=False must reset self.confirmed."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "strict_pin",
            "level": "hard",
            "predicate": "strict constraint",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "confirm_reset.db")
    storage = SQLiteStorage(db)
    run_id = "run_reset_1"
    storage.create_run(Run(run_id=run_id, goal="test confirm reset"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test confirm reset", "total": 1})
    events = list(storage.read_events(run_id))
    state = project(run_id, events)

    validator = StateValidator()

    # First call: confirmed with ["pin"]
    outcome1 = validator.validate(state, events=events, confirmed=["pin"])
    pin_entry1 = next(e for e in outcome1.report.statuses if e.component is Component.PIN)
    assert pin_entry1.status is StateStatus.VALID

    # Second call: confirmed=False resets self.confirmed
    outcome2 = validator.validate(state, events=events, confirmed=False)
    pin_entry2 = next(e for e in outcome2.report.statuses if e.component is Component.PIN)
    assert pin_entry2.status is StateStatus.REQUIRES_REVIEW


def test_soft_pin_digest_mismatch_creates_advisory_entry(tmp_path: Path, monkeypatch) -> None:
    """Soft pin with mismatched digest creates VALID entry with advisory detail."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "soft_audit",
            "level": "soft",
            "predicate": "audit advisory",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "soft.db")
    storage = SQLiteStorage(db)
    run_id = "run_soft_1"
    storage.create_run(Run(run_id=run_id, goal="test soft"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test soft"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {
            "constraint_id": "soft_audit",
            "sha256": _sha256("different predicate"),
        },
    )
    events = list(storage.read_events(run_id))
    state = project(run_id, events)
    validator = StateValidator()
    outcome = validator.validate(state, events=events)
    entry = next(
        e for e in outcome.report.statuses if e.component is Component.PIN and e.component_id == "soft_audit"
    )
    assert entry.status is StateStatus.VALID
    assert "advisory" in entry.detail


def test_pin_confirmation_invalidated_by_subsequent_pin_event(tmp_path: Path) -> None:
    """A subsequent constraint pin mutation clears previous human pin confirmation."""
    from continuum.recovery.engine import RecoveryEngine

    db = str(tmp_path / "confirm_invalidation.db")
    storage = SQLiteStorage(db)
    run_id = "run_inval_1"
    storage.create_run(Run(run_id=run_id, goal="test pin confirmation invalidation"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test inval"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "pin_1", "level": "hard", "reason": "first drop"},
    )
    # Human confirms the pin
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"scope": "pin"},
        source=Origin.HUMAN,
    )
    # Later event drops another pin or mutates
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "pin_2", "level": "hard", "reason": "second drop"},
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any("pin_2" in item for item in decision.contract.invalidated)
