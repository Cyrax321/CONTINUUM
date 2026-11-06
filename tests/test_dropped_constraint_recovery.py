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
    assert any(
        "financial_gate" in step and "continuum confirm" in step and "--scope pin" in step
        for step in guidance
    )


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
    d2 = RecoveryEngine(storage).assess(run_id)
    assert not any(
        e.component is Component.PIN and e.status is not StateStatus.VALID
        for e in d2.validation.report.statuses
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
        e
        for e in outcome.report.statuses
        if e.component is Component.PIN and e.component_id == "soft_audit"
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


def test_repinned_constraint_clears_pending_drop(tmp_path: Path, monkeypatch) -> None:
    """When a constraint was dropped but subsequently re-pinned with CONSTRAINT_PINNED,
    the pending drop is cleared and the pin evaluates to VALID."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    predicate = "audit log active"
    spec_data = [{"id": "audit_pin", "level": "hard", "predicate": predicate, "scope": ["*"]}]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "repin.db")
    storage = SQLiteStorage(db)
    run_id = "run_repin"
    storage.create_run(Run(run_id=run_id, goal="test repin"))

    # Start run with pin
    pin_payload = ConstraintPinned(constraint_id="audit_pin", sha256=predicate_digest(predicate))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(run_id, EventType.CONSTRAINT_PINNED, pin_payload.model_dump())

    # Pin dropped
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "audit_pin", "level": "hard", "reason": "context compacted"},
    )

    # Re-pinned with same valid digest
    storage.append_event(run_id, EventType.CONSTRAINT_PINNED, pin_payload.model_dump())

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    pin_entries = [
        e
        for e in decision.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "audit_pin"
    ]
    assert len(pin_entries) == 1
    assert pin_entries[0].status is StateStatus.VALID
    assert "active and verified" in pin_entries[0].detail


def test_repinned_constraint_no_registry_clears_pending_drop(tmp_path: Path, monkeypatch) -> None:
    """Without an on-disk registry, re-pinning after a drop clears the pending drop."""
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "repin_noreg.db")
    storage = SQLiteStorage(db)
    run_id = "run_repin_noreg"
    storage.create_run(Run(run_id=run_id, goal="test repin no registry"))

    pin_payload = ConstraintPinned(constraint_id="sec_pin", sha256="abc" * 21 + "a")
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(run_id, EventType.CONSTRAINT_PINNED, pin_payload.model_dump())
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "sec_pin", "level": "hard", "reason": "dropped"},
    )
    storage.append_event(run_id, EventType.CONSTRAINT_PINNED, pin_payload.model_dump())

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    pin_entries = [
        e
        for e in decision.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "sec_pin"
    ]
    assert not any(e.status is StateStatus.REQUIRES_REVIEW for e in pin_entries)


def test_cli_argparse_accepts_pin_and_constraints_scopes() -> None:
    """Argparse parser for continuum confirm must accept --scope pin and --scope constraints."""
    from continuum.cli.main import build_parser

    parser = build_parser()
    args_pin = parser.parse_args(["confirm", "run_123", "--scope", "pin"])
    assert args_pin.scope == ["pin"]
    assert args_pin.run_id == "run_123"

    args_constraints = parser.parse_args(["confirm", "run_123", "--scope", "constraints"])
    assert args_constraints.scope == ["constraints"]

    args_multi = parser.parse_args(["confirm", "run_123", "--scope", "pin", "goal"])
    assert args_multi.scope == ["pin", "goal"]


def test_constraint_pin_dropped_supports_soft_level(tmp_path: Path, monkeypatch) -> None:
    """ConstraintPinDropped supports level='soft' and validator treats it as advisory."""
    from continuum.models import ConstraintPinDropped

    drop = ConstraintPinDropped(
        constraint_id="soft_cid",
        sha256="abc" * 21 + "a",
        reason="soft_warning",
        level="soft",
    )
    assert drop.level == "soft"

    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "soft_drop.db")
    storage = SQLiteStorage(db)
    run_id = "run_soft_drop"
    storage.create_run(Run(run_id=run_id, goal="test soft drop"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(run_id, EventType.CONSTRAINT_PIN_DROPPED, drop.model_dump())

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    pin_entries = [
        e
        for e in decision.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "soft_cid"
    ]
    assert not any(e.status is StateStatus.REQUIRES_REVIEW for e in pin_entries)


def test_guidance_for_unreadable_registry_advises_repair(tmp_path: Path, monkeypatch) -> None:
    """When the constraints registry is unreadable, human guidance instructs repairing it."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    constraints_path.write_text("invalid json {{{", encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "guidance_repair.db")
    storage = SQLiteStorage(db)
    run_id = "run_unreadable_reg"
    storage.create_run(Run(run_id=run_id, goal="test unreadable registry"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    guidance = human_steps_for(decision, run_id=run_id)
    assert any("repair unreadable constraints registry file" in g for g in guidance)
    assert not any("--scope pin" in g for g in guidance)


def test_failed_verification_invalidates_earlier_pin_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    """A subsequent failed verification invalidates earlier pin confirmations."""
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "failed_audit_inval.db")
    storage = SQLiteStorage(db)
    run_id = "run_audit_inval"
    storage.create_run(Run(run_id=run_id, goal="test audit invalidation"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "audit_pin", "sha256": "abc" * 21 + "a", "level": "hard"},
    )
    # Human confirms the pin
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["pin"]},
        source=Origin.HUMAN,
    )
    engine = RecoveryEngine(storage)
    d1 = engine.assess(run_id)
    assert not any(
        e.component is Component.PIN and e.status is StateStatus.REQUIRES_REVIEW
        for e in d1.validation.report.statuses
    )

    # Now a verification event arrives with verified: false
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINS_VERIFIED,
        {"verified": False, "unverified": ["audit_pin"]},
        source=Origin.DETERMINISTIC,
    )
    d2 = engine.assess(run_id)
    assert d2.mode is RecoveryMode.REQUEST_HUMAN
    assert any(
        e.component is Component.PIN
        and e.component_id == "audit_pin"
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in d2.validation.report.statuses
    )


def test_registry_predicate_change_invalidates_prior_pin_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    """When an operator modifies a predicate in constraints.json, prior pin confirmations do not mask it."""
    import argparse

    from continuum.cli.main import cmd_confirm

    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "mutating_pin",
            "level": "hard",
            "predicate": "initial predicate text",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "reg_change.db")
    storage = SQLiteStorage(db)
    run_id = "run_reg_change"
    storage.create_run(Run(run_id=run_id, goal="test registry change"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    # Pinned to initial predicate
    initial_sha = predicate_digest("initial predicate text")
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "mutating_pin", "sha256": initial_sha},
    )

    # Operator confirms
    args = argparse.Namespace(run_id=run_id, scope=["pin"], tolerate_unknown=False, model=None)
    cmd_confirm(args, storage, None, None)

    engine = RecoveryEngine(storage)
    d1 = engine.assess(run_id)
    assert not any(
        e.component is Component.PIN and e.status is StateStatus.REQUIRES_REVIEW
        for e in d1.validation.report.statuses
    )

    # Operator modifies the predicate in constraints.json
    spec_data[0]["predicate"] = "changed predicate text that demands new compliance"
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")

    d2 = engine.assess(run_id)
    assert d2.mode is RecoveryMode.REQUEST_HUMAN
    pin_entry = next(
        e
        for e in d2.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "mutating_pin"
    )
    assert pin_entry.status is StateStatus.REQUIRES_REVIEW
    assert "predicate digest mismatch" in pin_entry.detail


def test_registry_change_does_not_drop_goal_progress_confirmations(
    tmp_path: Path, monkeypatch
) -> None:
    """Modifying constraints.json must not drop human confirmation of goal and progress (#1967)."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "c1",
            "level": "soft",
            "predicate": "initial predicate",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "goal_prog_persist.db")
    storage = SQLiteStorage(db)
    run_id = "run_persist"
    storage.create_run(Run(run_id=run_id, goal="test goal"))
    storage.append_event(
        run_id, EventType.RUN_STARTED, {"goal": "test goal"}, source=Origin.EXTERNAL_AGENT
    )

    reg = load_constraints(constraints_path, asserted_by=Origin.DETERMINISTIC)
    storage.append_event(
        run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"], "registry_digest": reg.digest},
        source=Origin.HUMAN,
    )

    engine = RecoveryEngine(storage)
    d1 = engine.assess(run_id)
    assert d1.mode is RecoveryMode.RESUME

    # Modify constraints.json with an unrelated edit
    spec_data[0]["predicate"] = "updated predicate text"
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")

    d2 = engine.assess(run_id)
    assert d2.mode is RecoveryMode.RESUME


def test_untrusted_verification_event_rejected(tmp_path: Path, monkeypatch) -> None:
    """An agent-originated CONSTRAINT_PINS_VERIFIED event with verified: true cannot clear review."""
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "untrusted_audit.db")
    storage = SQLiteStorage(db)
    run_id = "run_untrusted_audit"
    storage.create_run(Run(run_id=run_id, goal="test untrusted audit"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "agent_pin", "sha256": "abc" * 21 + "a"},
    )
    # Trusted audit failed
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINS_VERIFIED,
        {"verified": False, "unverified": ["agent_pin"]},
        source=Origin.DETERMINISTIC,
    )
    # Agent tries to self-certify with verified: true
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINS_VERIFIED,
        {"verified": True},
        source=Origin.EXTERNAL_AGENT,
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any(
        e.component is Component.PIN
        and e.component_id == "agent_pin"
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in decision.validation.report.statuses
    )


def test_soft_drop_does_not_mask_prior_hard_drop(tmp_path: Path, monkeypatch) -> None:
    """A later soft drop event for the same CID does not overwrite sticky hard-drop status."""
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "sticky_hard.db")
    storage = SQLiteStorage(db)
    run_id = "run_sticky_hard"
    storage.create_run(Run(run_id=run_id, goal="test sticky hard drop"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    # Hard drop
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "sticky_cid", "sha256": "abc" * 21 + "a", "level": "hard"},
    )
    # Later soft drop arrives
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "sticky_cid", "sha256": "abc" * 21 + "a", "level": "soft"},
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    entry = next(
        e
        for e in decision.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "sticky_cid"
    )
    assert entry.status is StateStatus.REQUIRES_REVIEW


def test_event_only_retraction_requires_review(tmp_path: Path, monkeypatch) -> None:
    """Retraction of an event-log-only hard pin requires human confirmation."""
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "retraction_rev.db")
    storage = SQLiteStorage(db)
    run_id = "run_retraction_rev"
    storage.create_run(Run(run_id=run_id, goal="test retraction review"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "retracted_cid", "sha256": "abc" * 21 + "a"},
    )
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_RETRACTED,
        {"constraint_id": "retracted_cid"},
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    entry = next(
        e
        for e in decision.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "retracted_cid"
    )
    assert entry.status is StateStatus.REQUIRES_REVIEW
    assert "retracted" in entry.detail


def test_registry_does_not_hide_event_only_pins_or_failed_verification(
    tmp_path: Path, monkeypatch
) -> None:
    """A registry on disk does not bypass event-log dropped pins or failed verifications."""
    constraints_path = tmp_path / ".continuum" / "constraints.json"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    spec_data = [
        {
            "id": "registered_pin",
            "level": "hard",
            "predicate": "audit events must be recorded",
            "scope": ["*"],
        }
    ]
    constraints_path.write_text(json.dumps({"constraints": spec_data}), encoding="utf-8")
    monkeypatch.setattr("continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", constraints_path)

    db = str(tmp_path / "reg_no_hide.db")
    storage = SQLiteStorage(db)
    run_id = "run_reg_no_hide"
    storage.create_run(Run(run_id=run_id, goal="test registry no hide"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    # Registered pin is pinned but failed verification
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {
            "constraint_id": "registered_pin",
            "sha256": predicate_digest("audit events must be recorded"),
        },
    )
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINS_VERIFIED,
        {"verified": False, "unverified": ["registered_pin"]},
        source=Origin.DETERMINISTIC,
    )
    # Event-only pin was dropped
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "event_only_pin", "sha256": "123" * 21 + "1", "level": "hard"},
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    pin_statuses = {
        e.component_id: e.status
        for e in decision.validation.report.statuses
        if e.component is Component.PIN
    }
    assert pin_statuses.get("registered_pin") is StateStatus.REQUIRES_REVIEW
    assert pin_statuses.get("event_only_pin") is StateStatus.REQUIRES_REVIEW


def test_concurrent_validations_do_not_leak_confirmations() -> None:
    """A shared StateValidator instance must not leak confirmations across different validate() calls."""
    from continuum.events import Event

    validator = StateValidator()
    run_id = "run_thread_safe"
    events_unconfirmed = [
        Event(
            event_id="e1",
            run_id=run_id,
            sequence=1,
            type=EventType.RUN_STARTED,
            payload={"goal": "test", "total": 1},
            source=Origin.EXTERNAL_AGENT,
        ),
    ]
    state_unconfirmed = project(run_id, events_unconfirmed)

    # First call with confirmed=True
    outcome_confirmed = validator.validate(state_unconfirmed, confirmed=True)
    assert outcome_confirmed.safe

    # Second call with confirmed=False must NOT inherit confirmation from the first call
    outcome_unconfirmed = validator.validate(state_unconfirmed, confirmed=False)
    assert not outcome_unconfirmed.safe
    assert any(
        e.component is Component.GOAL and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome_unconfirmed.report.statuses
    )


def test_one_shot_events_iterator_not_exhausted(tmp_path: Path, monkeypatch) -> None:
    """When events is a generator/iterator, earlier validation checks must not exhaust it before _check_constraints."""
    from continuum.events import Event

    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    run_id = "run_iter_events"
    events = [
        Event(
            event_id="e1",
            run_id=run_id,
            sequence=1,
            type=EventType.RUN_STARTED,
            payload={"goal": "test", "total": 1},
        ),
        Event(
            event_id="e2",
            run_id=run_id,
            sequence=2,
            type=EventType.CONSTRAINT_PIN_DROPPED,
            payload={"constraint_id": "iter_pin", "sha256": "abc" * 21 + "a", "level": "hard"},
        ),
    ]
    state = project(run_id, events)

    # Pass as a generator (one-shot iterator)
    event_iter = (ev for ev in events)
    validator = StateValidator()
    outcome = validator.validate(state, events=event_iter)
    assert not outcome.safe
    assert any(
        e.component is Component.PIN
        and e.component_id == "iter_pin"
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in outcome.report.statuses
    )


def test_repinned_constraint_with_different_digest_requires_review(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-pinning a dropped constraint with a different predicate digest retains review requirement."""
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "repin_diff_digest.db")
    storage = SQLiteStorage(db)
    run_id = "run_repin_diff"
    storage.create_run(Run(run_id=run_id, goal="test repin diff digest"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test"})
    # Pinned with original sha
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "diff_pin", "sha256": "111" * 21 + "1"},
    )
    # Dropped
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "diff_pin", "sha256": "111" * 21 + "1", "level": "hard"},
    )
    # Re-pinned with DIFFERENT sha!
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "diff_pin", "sha256": "222" * 21 + "2"},
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    entry = next(
        e
        for e in decision.validation.report.statuses
        if e.component is Component.PIN and e.component_id == "diff_pin"
    )
    assert entry.status is StateStatus.REQUIRES_REVIEW


def test_scoped_assess_still_escalates_dropped_hard_pin(tmp_path: Path, monkeypatch) -> None:
    """A scoped assessment must escalate a dropped hard pin, not resume past it.

    The scoped path skips the self-certification checks (they re-flag unrelated
    components), but a dropped hard pin is a governance violation independent
    of the dependency scope, so the pin audit runs on both paths (issue #1414).
    """
    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "scoped_drop.db")
    storage = SQLiteStorage(db)
    run_id = "run_scoped_drop"
    storage.create_run(Run(run_id=run_id, goal="test scoped drop"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test", "total": 1})
    storage.append_event(
        run_id,
        EventType.DEPENDENCY_DECLARED,
        {"resource": "dataset", "version": "v1"},
    )
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PIN_DROPPED,
        {"constraint_id": "scoped_pin", "sha256": "abc" * 21 + "a", "level": "hard"},
    )

    engine = RecoveryEngine(storage)
    decision = engine.assess_scoped(run_id, ["dataset"])
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any(
        e.component is Component.PIN
        and e.component_id == "scoped_pin"
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in decision.validation.report.statuses
    )


def test_cli_confirm_scope_pin_goes_through_the_parser(tmp_path: Path, monkeypatch) -> None:
    """`continuum confirm <run> --scope pin` must survive the argument parser.

    The handler allowlist alone is not enough: argparse validates --scope
    before cmd_confirm runs, so the parser must accept the pin scope too
    (review feedback on the original escalation change).
    """
    import io

    from continuum.cli import main

    monkeypatch.setattr(
        "continuum.security.constraints.DEFAULT_CONSTRAINTS_PATH", tmp_path / "nonexistent.json"
    )
    db = str(tmp_path / "cli_scope_pin.db")
    run_id = "run_cli_scope"
    with SQLiteStorage(db) as storage:
        storage.create_run(Run(run_id=run_id, goal="test cli scope pin"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "test", "total": 1})

    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "confirm", run_id, "--scope", "pin"], out=out, err=err)
    assert code == 0, err.getvalue()

    with SQLiteStorage(db) as reopened:
        events = reopened.read_events(run_id)
    confirmations = [e for e in events if e.type is EventType.REVIEW_CONFIRMED]
    assert len(confirmations) == 1
    assert confirmations[0].payload["components"] == ["pin"]
    assert confirmations[0].source is Origin.HUMAN
