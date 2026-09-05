"""External probe via reconciler registry plus negative test, restore does not resurrect (issue #557, #289c)."""

from __future__ import annotations

import json
import pathlib
import sys

from continuum.actions.authority import record_authority_consumed
from continuum.events import EventType
from continuum.gate import collect_consumed_authorities, decide
from continuum.models import Run
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
