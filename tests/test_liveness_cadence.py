"""Cadence contracts and read-path liveness, injected clock, no sleeps (issue #561)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from continuum.actions import ActionLedger
from continuum.cli.main import _liveness_advisory, _liveness_text, main
from continuum.events import EventType
from continuum.liveness import CadenceContract, evaluate, load_cadence_contract
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_defaults_use_phase_scopes() -> None:
    contract = CadenceContract()
    assert contract.max_silence_seconds == 3600
    assert contract.phase_scopes["open_claim"] == 600
    assert contract.phase_scopes["otherwise"] == 3600
    assert contract.threshold_for(has_open_claim=True) == 600
    assert contract.threshold_for(has_open_claim=False) == 3600


def test_evaluate_respects_max_silence_vs_phase() -> None:
    contract = CadenceContract(
        max_silence_seconds=3600, phase_scopes={"open_claim": 600, "otherwise": 3600}
    )
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    last = now - timedelta(seconds=700)
    assert evaluate(now, last, contract=contract, has_open_claim=True).breached is True
    assert evaluate(now, last, contract=contract, has_open_claim=False).breached is False
    last2 = now - timedelta(seconds=4000)
    assert evaluate(now, last2, contract=contract, has_open_claim=True).breached is True
    assert evaluate(now, last2, contract=contract, has_open_claim=False).breached is True
    last3 = now - timedelta(seconds=500)
    assert evaluate(now, last3, contract=contract, has_open_claim=True).breached is False
    assert evaluate(now, last3, contract=contract, has_open_claim=False).breached is False


def test_evaluate_injected_clock_no_sleep() -> None:
    contract = CadenceContract()
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    last = now - timedelta(seconds=100)
    result = evaluate(now, last, contract=contract, has_open_claim=False)
    assert result.breached is False
    assert result.silence_seconds == 100.0
    assert result.threshold_seconds == 3600
    assert result.phase == "otherwise"
    future = now + timedelta(seconds=10)
    result2 = evaluate(now, future, contract=contract)
    assert result2.breached is False


def test_evaluate_none_last_event_not_breached() -> None:
    contract = CadenceContract()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    result = evaluate(now, None, contract=contract)
    assert result.breached is False
    assert result.silence_seconds is None


def test_cadence_contract_evaluate_method_matches_function() -> None:
    contract = CadenceContract()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    last = now - timedelta(seconds=800)
    r1 = contract.evaluate(now, last, has_open_claim=True)
    r2 = evaluate(now, last, contract=contract, has_open_claim=True)
    assert r1.breached == r2.breached
    assert r1.silence_seconds == r2.silence_seconds


def test_load_cadence_contract_defaults_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_such.json"
    contract = load_cadence_contract(missing)
    assert contract.max_silence_seconds == 3600
    assert contract.phase_scopes["open_claim"] == 600


def test_load_cadence_contract_from_file(tmp_path: Path) -> None:
    p = tmp_path / "liveness.json"
    p.write_text(
        json.dumps(
            {"max_silence_seconds": 1800, "phase_scopes": {"open_claim": 300, "otherwise": 1800}}
        ),
        encoding="utf-8",
    )
    contract = load_cadence_contract(p)
    assert contract.max_silence_seconds == 1800
    assert contract.threshold_for(True) == 300
    assert contract.threshold_for(False) == 1800


def test_liveness_advisory_computes_silence_vs_threshold(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    with SQLiteStorage(db) as store:
        run_id = "run_liveness_1"
        store.create_run(Run(run_id=run_id, goal="test"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        store.append_event(run_id, EventType.TASK_UPDATED, {"completed": 1})
        advisory = _liveness_advisory(store, run_id, now=now)
        assert "breached" in advisory
        assert "silence_seconds" in advisory
        assert advisory["threshold_seconds"] == 3600
        assert advisory["phase"] == "otherwise"


def test_liveness_phase_open_claim_vs_otherwise(tmp_path: Path) -> None:
    db = str(tmp_path / "test2.db")
    with SQLiteStorage(db) as store:
        run_id = "run_liveness_phase"
        store.create_run(Run(run_id=run_id, goal="test"))
        store.append_event(run_id, EventType.TASK_UPDATED, {"completed": 1})
        ledger = ActionLedger(store, run_id)
        ledger.claim("test.action", {"x": 1})
        now = datetime.now(UTC) + timedelta(seconds=700)
        advisory = _liveness_advisory(store, run_id, now=now)
        assert advisory["has_open_claim"] is True
        assert advisory["threshold_seconds"] == 600
        contract = CadenceContract()
        last_ts = datetime.now(UTC) - timedelta(seconds=700)
        future = last_ts + timedelta(seconds=700)
        adv2 = evaluate(future, last_ts, contract=contract, has_open_claim=True)
        assert adv2.breached is True
        adv3 = evaluate(future, last_ts, contract=contract, has_open_claim=False)
        assert adv3.breached is False


def test_breach_surfaces_as_advisory_not_mode_change(tmp_path: Path) -> None:
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "test3.db")
    with SQLiteStorage(db) as store:
        run_id = "run_liveness_advisory"
        store.create_run(Run(run_id=run_id, goal="ship"))
        store.append_event(run_id, EventType.RUN_STARTED, {"goal": "ship"})
        engine = RecoveryEngine(store)
        decision = engine.assess(run_id)
        assert decision.mode.value == "resume"
        far_future = datetime.now(UTC) + timedelta(seconds=10000)
        advisory = _liveness_advisory(store, run_id, now=far_future)
        assert advisory["breached"] is True
        assert decision.mode.value == "resume"
        text = _liveness_text(advisory)
        assert "BREACHED" in text
        assert "Advisory only" in text


def test_liveness_text_ok_and_breached() -> None:
    advisory_ok = {
        "breached": False,
        "silence_seconds": 100.0,
        "threshold_seconds": 600,
        "phase": "open_claim",
    }
    assert "ok" in _liveness_text(advisory_ok)
    advisory_breach = {
        "breached": True,
        "silence_seconds": 700.0,
        "threshold_seconds": 600,
        "phase": "open_claim",
    }
    assert "BREACHED" in _liveness_text(advisory_breach)
    advisory_none = {
        "breached": False,
        "silence_seconds": None,
        "threshold_seconds": 600,
        "phase": "otherwise",
    }
    assert "no events" in _liveness_text(advisory_none).lower()


def test_cli_validate_includes_liveness_advisory(tmp_path: Path) -> None:
    import io

    db = str(tmp_path / "cli_liveness.db")
    run_id = "run_cli_liveness"
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "start", run_id, "--goal", "test liveness"], out=out, err=err)
    assert code == 0
    out2, err2 = io.StringIO(), io.StringIO()
    code2 = main(["--db", db, "--json", "validate", run_id], out=out2, err=err2)
    assert code2 == 0
    payload = json.loads(out2.getvalue())
    assert "liveness" in payload
    assert "breached" in payload["liveness"]
    assert "threshold_seconds" in payload["liveness"]


def test_cli_resume_includes_liveness_advisory(tmp_path: Path) -> None:
    import io

    db = str(tmp_path / "cli_liveness2.db")
    run_id = "run_cli_liveness2"
    out, err = io.StringIO(), io.StringIO()
    main(["--db", db, "start", run_id, "--goal", "test resume liveness"], out=out, err=err)
    out2, err2 = io.StringIO(), io.StringIO()
    code2 = main(["--db", db, "--json", "resume", run_id], out=out2, err=err2)
    assert code2 == 0
    payload = json.loads(out2.getvalue())
    assert "liveness" in payload
    assert payload["liveness"]["phase"] in ("open_claim", "otherwise")


def test_dashboard_renders_liveness(tmp_path: Path) -> None:
    from continuum.dashboard.app import render_run_detail_html

    db = str(tmp_path / "dash_liveness.db")
    with SQLiteStorage(db) as store:
        run_id = "run_dash_liveness"
        store.create_run_started(Run(run_id=run_id, goal="dash test"))
        store.append_event(run_id, EventType.TASK_UPDATED, {"completed": 1})
        html = render_run_detail_html(store, run_id)
        assert "Liveness" in html


def test_mcp_read_tools_include_liveness(tmp_path: Path) -> None:
    db = str(tmp_path / "mcp_liveness.db")
    with SQLiteStorage(db) as store:
        run_id = "run_mcp_liveness"
        store.create_run_started(Run(run_id=run_id, goal="mcp test"))
        store.append_event(run_id, EventType.TASK_UPDATED, {"completed": 1})
        from continuum.recovery.health import advisory_for_storage

        adv = advisory_for_storage(store, run_id)
        assert "breached" in adv
        assert "threshold_seconds" in adv
        try:
            from continuum.mcp.server import build_server

            _ = build_server(storage=store)
        except ModuleNotFoundError:
            pass
