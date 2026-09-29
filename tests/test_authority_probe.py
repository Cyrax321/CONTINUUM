"""External probe via reconciler registry plus negative test, restore does not resurrect (issue #557, #289c)."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

import pytest

from continuum.actions.authority import record_authority_consumed
from continuum.events import EventType
from continuum.gate import collect_consumed_authorities, decide
from continuum.models import RecoveryMode, Run
from continuum.reconcilers import load_reconcilers, settle_authority
from continuum.recovery.engine import RecoveryEngine
from continuum.storage import SQLiteStorage


def _probe_command(body: str) -> str:
    """A probe command that prints *body* on any shell.

    ``echo`` with single quotes is a POSIX-ism: Windows cmd keeps the quotes
    and the output stops parsing as JSON. The interpreter is already here,
    so use it directly; backslash-escaped quotes survive both sh grouping
    and the C runtime unescaping behind cmd.
    """
    inner = "'" + body.replace('"', '\\"') + "'"
    return f'"{sys.executable}" -c "print({inner})"'


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    return storage


def test_probe_via_reconcilers_json_valid_true_unblocks(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-probe-1")
        cfg = tmp_path / "reconcilers.json"
        cfg.write_text(
            json.dumps(
                {
                    "probes": {
                        "auth-probe-1": {"command": _probe_command('{"valid": true}'), "timeout": 5}
                    }
                }
            )
        )
        probes = load_reconcilers(cfg)
        consumed = collect_consumed_authorities(storage.read_events("run_1"))
        assert "auth-probe-1" in consumed
        config = {"tool": {"key_template": "{x}"}}
        decision = decide(
            config,
            "tool",
            {"x": "1", "authority_id": "auth-probe-1"},
            run_id="run_1",
            actions_by_key={},
            consumed_authorities=consumed,
        )
        assert not decision.allow

        report = settle_authority(storage, "run_1", "auth-probe-1", probes)
        assert report.valid is True
        assert report.settled is True
        consumed2 = collect_consumed_authorities(storage.read_events("run_1"))
        assert "auth-probe-1" not in consumed2
        decision2 = decide(
            config,
            "tool",
            {"x": "1", "authority_id": "auth-probe-1"},
            run_id="run_1",
            actions_by_key={},
            consumed_authorities=consumed2,
        )
        assert "consumed at seq" not in decision2.reason
        events = list(storage.read_events("run_1"))
        reconciled = [e for e in events if e.type == EventType.AUTHORITY_RECONCILED]
        assert len(reconciled) == 1
        assert reconciled[0].payload["valid"] is True
        assert reconciled[0].payload["authority_id"] == "auth-probe-1"
    finally:
        storage.close()


def test_probe_valid_false_keeps_blocked(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-probe-2")
        cfg = tmp_path / "reconcilers.json"
        cfg.write_text(
            json.dumps(
                {
                    "probes": {
                        "auth-probe-2": {
                            "command": _probe_command('{"valid": false}'),
                            "timeout": 5,
                        }
                    }
                }
            )
        )
        probes = load_reconcilers(cfg)
        report = settle_authority(storage, "run_1", "auth-probe-2", probes)
        assert report.valid is False
        assert report.settled is True
        consumed = collect_consumed_authorities(storage.read_events("run_1"))
        assert "auth-probe-2" in consumed
        config = {"tool": {"key_template": "{x}"}}
        decision = decide(
            config,
            "tool",
            {"x": "1", "authority_id": "auth-probe-2"},
            run_id="run_1",
            actions_by_key={},
            consumed_authorities=consumed,
        )
        assert not decision.allow
        assert "auth-probe-2" in decision.reason
    finally:
        storage.close()


def test_probe_unknown_leaves_blocked(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-probe-3")
        cfg = tmp_path / "reconcilers.json"
        cfg.write_text(
            json.dumps(
                {
                    "probes": {
                        "auth-probe-3": {
                            "command": _probe_command('{"valid": "unknown"}'),
                            "timeout": 5,
                        }
                    }
                }
            )
        )
        probes = load_reconcilers(cfg)
        report = settle_authority(storage, "run_1", "auth-probe-3", probes)
        assert report.valid is None
        assert report.settled is False
        consumed = collect_consumed_authorities(storage.read_events("run_1"))
        assert "auth-probe-3" in consumed
    finally:
        storage.close()


def test_restore_does_not_resurrect_negative(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    try:
        ev = record_authority_consumed(storage, "run_1", "auth-restore-neg")
        from continuum.checkpoint.manager import CheckpointManager

        mgr = CheckpointManager(storage)
        state = mgr.project_current("run_1")
        checkpoint = mgr.checkpoint("run_1", state=state)
        assert checkpoint is not None

        consumed = collect_consumed_authorities(storage.read_all_events("run_1"))
        assert "auth-restore-neg" in consumed
        config = {"tool": {"key_template": "{x}"}}
        decision = decide(
            config,
            "tool",
            {"x": "1", "authority_id": "auth-restore-neg"},
            run_id="run_1",
            actions_by_key={},
            consumed_authorities=consumed,
        )
        assert not decision.allow
        assert "auth-restore-neg" in decision.reason
        assert str(ev.sequence) in decision.reason

        engine = RecoveryEngine(storage)
        decision_recovery = engine.assess("run_1")
        assert decision_recovery.mode.value == "request_human"
        assert (
            any("authority" in r.lower() for r in decision_recovery.rationale)
            or "consumed authority" in " ".join(decision_recovery.rationale).lower()
        )

        cfg = tmp_path / "reconcilers2.json"
        cfg.write_text(
            json.dumps(
                {
                    "probes": {
                        "auth-restore-neg": {
                            "command": _probe_command('{"valid": true}'),
                            "timeout": 5,
                        }
                    }
                }
            )
        )
        probes = load_reconcilers(cfg)
        report = settle_authority(storage, "run_1", "auth-restore-neg", probes)
        assert report.valid is True
        consumed2 = collect_consumed_authorities(storage.read_events("run_1"))
        assert "auth-restore-neg" not in consumed2
        decision2 = decide(
            config,
            "tool",
            {"x": "1", "authority_id": "auth-restore-neg"},
            run_id="run_1",
            actions_by_key={},
            consumed_authorities=consumed2,
        )
        assert "consumed at seq" not in decision2.reason
    finally:
        storage.close()


def test_authority_probe_no_probe_registered_leaves_blocked(tmp_path: pathlib.Path) -> None:
    storage = _storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-no-probe")
        probes: dict = {}
        report = settle_authority(storage, "run_1", "auth-no-probe", probes)
        assert report.valid is None
        assert not report.settled
        assert "no probe" in report.detail.lower()
        consumed = collect_consumed_authorities(storage.read_events("run_1"))
        assert "auth-no-probe" in consumed
    finally:
        storage.close()


def test_probe_payload_keeps_consumption_context_after_compaction(
    tmp_path: pathlib.Path,
) -> None:
    """Post-compaction probes must still see how the authority was consumed (issue #647).

    The AUTHORITY_CONSUMED row can predate a compaction. settle_authority used
    to scan the live tail only, silently handing the probe a bare authority_id
    payload with no consumer_run_id, via_action_id, or sequence.
    """
    storage = _storage()
    try:
        record_authority_consumed(storage, "run_1", "auth-compact-1", via_action_id="act-1")
        storage.compact_run("run_1", through_sequence=storage.last_sequence("run_1"))
        # Premise guard: the consumption row really is archived, not live.
        live_types = [e.type for e in storage.read_events("run_1")]
        assert EventType.AUTHORITY_CONSUMED not in live_types

        payload_file = tmp_path / "payload.json"
        # Single quotes inside the double-quoted -c argument survive both sh
        # grouping and cmd's C-runtime unescaping (see _probe_command).
        cmd = f"\"{sys.executable}\" -c \"import sys; open('{payload_file.as_posix()}', 'w').write(sys.stdin.read())\""
        cfg = tmp_path / "reconcilers.json"
        cfg.write_text(json.dumps({"probes": {"auth-compact-1": {"command": cmd, "timeout": 5}}}))
        probes = load_reconcilers(cfg)
        report = settle_authority(storage, "run_1", "auth-compact-1", probes)
        assert report.settled is False  # prints no verdict line: stays unknown

        received = json.loads(payload_file.read_text(encoding="utf-8"))
        assert received["authority_id"] == "auth-compact-1"
        assert received["consumer_run_id"] == "run_1"
        assert received["via_action_id"] == "act-1"
        assert isinstance(received["sequence"], int)
        assert received["consumed_at"]
    finally:
        storage.close()


def test_consumed_authority_does_not_downgrade_a_stricter_verdict() -> None:
    """A consumed authority escalates to REQUEST_HUMAN, never past it (issue #1146).

    The module documents that "the engine always returns the maximum proposed
    mode" (SEVERITY is ascending caution). The consumed-authority block used to
    *overwrite* the mode unconditionally, so a risk policy that had already
    proposed ABORT or ROLLBACK — strictly more cautious — was silently
    downgraded to REQUEST_HUMAN, and the rationale still named the abort while
    the verdict no longer delivered it.
    """
    from continuum.models import Origin
    from continuum.recovery.engine import SEVERITY

    storage = _storage()
    try:
        record_authority_consumed(storage, "run_1", "authz:stripe-1")
        # The default risk policy maps side_effect_duplicate -> abort.
        storage.append_event(
            "run_1",
            EventType.RISK_OBSERVED,
            {"trigger": "side_effect_duplicate", "score": 0.9, "detail": "dup"},
            source=Origin.EXTERNAL_MONITOR,
        )

        decision = RecoveryEngine(storage).assess("run_1")
        assert decision.mode == RecoveryMode.ABORT
        assert SEVERITY[decision.mode] >= SEVERITY[RecoveryMode.REQUEST_HUMAN]
        assert "consumed authority blocks resume" in " ".join(decision.rationale)
        assert "side_effect_duplicate" in " ".join(decision.rationale)
    finally:
        storage.close()


def test_probe_for_an_authority_that_was_never_consumed_settles_with_a_bare_id(
    tmp_path: pathlib.Path,
) -> None:
    """No AUTHORITY_CONSUMED row at all: the scan exhausts and the probe still runs.

    Covers the loop-exhaustion branch of the consumption scan (every other
    test finds a row and breaks). The payload is intentionally bare: only
    authority_id, with no consumption context to lose.
    """
    storage = _storage()
    try:
        cfg = tmp_path / "reconcilers.json"
        cfg.write_text(
            json.dumps(
                {
                    "probes": {
                        "auth-never-consumed": {
                            "command": _probe_command('{"valid": true}'),
                            "timeout": 5,
                        }
                    }
                }
            )
        )
        probes = load_reconcilers(cfg)
        report = settle_authority(storage, "run_1", "auth-never-consumed", probes)
        assert report.valid is True
        assert report.settled is True
    finally:
        storage.close()


def test_unreadable_authority_ledger_degrades_to_request_human(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1066: a failed ledger read must not clear the resume block.

    The consumed-authority block used to substitute an empty map when the
    event read failed, and an empty map is the *unblocked* answer: a run
    holding an unreconciled consumed credential got a resume verdict the
    moment the log became unreadable mid-assessment.
    """
    import continuum.recovery.engine as engine_mod
    from continuum.storage.base import CorruptedRecord

    db = str(tmp_path / "auth_unreadable.db")
    with SQLiteStorage(db) as storage:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        record_authority_consumed(storage, "run_1", "auth-123")

    # Baseline: a readable ledger blocks the run, as designed.
    with SQLiteStorage(db) as storage:
        baseline = RecoveryEngine(storage).assess("run_1", replay=False)
    assert baseline.mode.value == "request_human"

    def unreadable(events: object) -> dict[str, Any]:
        raise CorruptedRecord("events table: unreadable page (transient)")

    monkeypatch.setattr(engine_mod, "collect_consumed_authorities", unreadable)
    with SQLiteStorage(db) as storage:
        decision = RecoveryEngine(storage).assess("run_1", replay=False)

    # Degrade to the most cautious verdict, not the unblocked one.
    assert decision.mode.value == "request_human"
    assert decision.contract.recovery_status.value == "requires_human"
    assert decision.permits("anything") is False
    assert any("unreadable" in line for line in decision.rationale)


def test_unreadable_ledger_raises_a_floor_it_does_not_overwrite_a_higher_verdict(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1356: the unreadable branch imposes REQUEST_HUMAN as a floor.

    #1146 made the *readable* consumed-authority branch escalate rather than
    overwrite, so a risk-driven ABORT/ROLLBACK survives. The unreadable branch
    (added by #1066) kept a bare ``mode = REQUEST_HUMAN``, so a degraded log
    *downgraded* a risk-driven ABORT (severity 6) to REQUEST_HUMAN (4) --
    softening the decision in exactly the scenario the branch exists to harden.
    """
    import continuum.recovery.engine as engine_mod
    from continuum.checkpoint import CheckpointManager
    from continuum.environment import StaticProvider, capture
    from continuum.models import Origin
    from continuum.storage.base import CorruptedRecord

    def env(v: str) -> Any:
        return capture("run_1", StaticProvider(dataset=v))

    db = str(tmp_path / "auth_unreadable_risk.db")
    with SQLiteStorage(db) as storage:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 100})
        storage.append_event(
            "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
        )
        for i in range(20):
            storage.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})
        CheckpointManager(storage).checkpoint("run_1", environment=env("v3"))
        # A monitor observes a risk that drives the verdict to ABORT.
        storage.append_event(
            "run_1",
            EventType.RISK_OBSERVED,
            {"trigger": "side_effect_duplicate", "score": 1.0},
            source=Origin.EXTERNAL_MONITOR,
        )

    # Readable ledger: the risk policy's ABORT stands.
    with SQLiteStorage(db) as storage:
        readable = RecoveryEngine(storage).assess("run_1", current_environment=env("v4"))
    assert readable.mode is RecoveryMode.ABORT

    def unreadable(events: object) -> dict[str, Any]:
        raise CorruptedRecord("events table: unreadable page (transient)")

    monkeypatch.setattr(engine_mod, "collect_consumed_authorities", unreadable)
    with SQLiteStorage(db) as storage:
        degraded = RecoveryEngine(storage).assess("run_1", current_environment=env("v4"))

    # The unreadable ledger raises the floor but must not lower the verdict.
    assert degraded.mode is RecoveryMode.ABORT
    assert any("unreadable" in line for line in degraded.rationale)
