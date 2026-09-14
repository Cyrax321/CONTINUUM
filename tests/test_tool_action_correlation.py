"""Tool-to-action correlation identifiers (issue #785)."""

from __future__ import annotations

import pytest

from continuum.events import Event, EventType
from continuum.provenance.correlation import correlate_events, normalize_correlation_id


def test_normalize_accepts_absent() -> None:
    assert normalize_correlation_id(None) is None
    assert normalize_correlation_id("") is None


def test_normalize_accepts_bounded() -> None:
    assert normalize_correlation_id("a1-_B2") == "a1-_B2"
    assert normalize_correlation_id("x" * 64) == "x" * 64


def test_normalize_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        normalize_correlation_id("has space")
    with pytest.raises(ValueError):
        normalize_correlation_id("x" * 65)
    with pytest.raises(ValueError):
        normalize_correlation_id(123)
    with pytest.raises(ValueError):
        normalize_correlation_id("semi;colon")


def _event(sequence: int, event_type: EventType, payload: dict) -> Event:
    return Event(
        run_id="run_1",
        sequence=sequence,
        event_id=f"e{sequence:04d}",
        type=event_type,
        payload=payload,
    )


def test_correlate_matched_pair() -> None:
    events = [
        _event(1, EventType.ACTION_RECORDED, {"correlation_id": "c1"}),
        _event(2, EventType.TOOL_COMPLETED, {"correlation_id": "c1", "tool": "Write"}),
    ]
    result = correlate_events(events)
    assert list(result["chains"]) == ["c1"]
    assert [r["sequence"] for r in result["chains"]["c1"]] == [1, 2]
    assert result["unmatched_tool"] == []
    assert result["ambiguous"] == []


def test_correlate_unmatched_tool() -> None:
    events = [
        _event(1, EventType.TOOL_COMPLETED, {"tool": "Write"}),
        _event(2, EventType.TOOL_COMPLETED, {"correlation_id": "orphan", "tool": "Bash"}),
    ]
    result = correlate_events(events)
    assert result["chains"] == {}
    assert [r["sequence"] for r in result["unmatched_tool"]] == [1, 2]


def test_correlate_ambiguous_duplicate_action() -> None:
    events = [
        _event(1, EventType.ACTION_RECORDED, {"correlation_id": "dup"}),
        _event(2, EventType.ACTION_RECORDED, {"correlation_id": "dup"}),
        _event(3, EventType.TOOL_COMPLETED, {"correlation_id": "dup"}),
    ]
    result = correlate_events(events)
    assert result["ambiguous"] == ["dup"]
    assert [r["sequence"] for r in result["chains"]["dup"]] == [1, 2, 3]


def test_ledger_claim_stores_correlation_id() -> None:
    from continuum.actions import ActionLedger
    from continuum.models import Run
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("send_email", {"to": "x"}, correlation_id="c-1")
        assert outcome.fresh
        payloads = [
            e.payload for e in storage.read_events("run_1") if e.type is EventType.ACTION_RECORDED
        ]
        assert payloads and payloads[0].get("correlation_id") == "c-1"
    finally:
        storage.close()


def test_ledger_claim_rejects_malformed_correlation_id() -> None:
    from continuum.actions import ActionLedger, LedgerError
    from continuum.models import Run
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")
        with pytest.raises(LedgerError):
            ledger.claim("send_email", {"to": "x"}, correlation_id="bad id!")
    finally:
        storage.close()


def test_matched_claim_and_observation_group_together() -> None:
    from continuum.actions import ActionLedger
    from continuum.models import Origin, Run
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")
        ledger.claim("send_email", {"to": "x"}, correlation_id="chain-1")
        storage.append_event(
            "run_1",
            EventType.TOOL_COMPLETED,
            {"tool": "smtp", "correlation_id": "chain-1"},
            source=Origin.EXTERNAL_AGENT,
        )
        result = correlate_events(storage.read_all_events("run_1"))
        assert [r["sequence"] for r in result["chains"]["chain-1"]] == sorted(
            r["sequence"] for r in result["chains"]["chain-1"]
        )
        assert len(result["chains"]["chain-1"]) == 2
    finally:
        storage.close()


def test_repeated_similar_actions_need_distinct_ids() -> None:
    from continuum.actions import ActionLedger
    from continuum.models import Origin, Run
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")
        ledger.claim("send_email", {"to": "x"}, key="k1", correlation_id="first")
        ledger.claim("send_email", {"to": "x"}, key="k2", correlation_id="second")
        storage.append_event(
            "run_1",
            EventType.TOOL_COMPLETED,
            {"tool": "smtp", "correlation_id": "second"},
            source=Origin.EXTERNAL_AGENT,
        )
        result = correlate_events(storage.read_all_events("run_1"))
        assert set(result["chains"]) == {"first", "second"}
        assert len(result["chains"]["first"]) == 1
        assert len(result["chains"]["second"]) == 2
    finally:
        storage.close()


def test_match_does_not_settle_uncertain_action() -> None:
    from continuum.actions import ActionLedger
    from continuum.models import Origin, Run, UnknownSideEffect
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")
        outcome = ledger.claim("send_email", {"to": "x"}, correlation_id="u-1")
        assert outcome.fresh
        storage.append_event(
            "run_1",
            EventType.TOOL_COMPLETED,
            {"tool": "smtp", "correlation_id": "u-1"},
            source=Origin.EXTERNAL_AGENT,
        )
        with pytest.raises(UnknownSideEffect):
            ActionLedger(storage, "run_1").claim("send_email", {"to": "x"})
    finally:
        storage.close()


def test_correlation_survives_compaction_and_tamper_breaks_verify() -> None:
    from continuum.actions import ActionLedger
    from continuum.models import Origin, Run
    from continuum.storage import SQLiteStorage

    storage = SQLiteStorage(":memory:")
    try:
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(storage, "run_1")
        ledger.claim("send_email", {"to": "x"}, correlation_id="persist-1")
        storage.append_event(
            "run_1",
            EventType.TOOL_COMPLETED,
            {"tool": "smtp", "correlation_id": "persist-1"},
            source=Origin.EXTERNAL_AGENT,
        )
        events = storage.read_all_events("run_1")
        assert "persist-1" in correlate_events(events)["chains"]
        tampered = [
            e.model_copy(update={"payload": {**dict(e.payload), "correlation_id": "evil"}})
            for e in events
        ]
        assert correlate_events(tampered)["chains"].get("persist-1") is None
    finally:
        storage.close()


def test_correlate_cli_groups_and_filters(tmp_path) -> None:
    import io
    import json

    from continuum.cli import main
    from continuum.models import Origin, Run
    from continuum.storage import SQLiteStorage

    db = str(tmp_path / "corr.db")
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        from continuum.actions import ActionLedger

        ledger = ActionLedger(store, "run_1")
        ledger.claim("send_email", {"to": "x"}, correlation_id="cli-1")
        store.append_event(
            "run_1",
            EventType.TOOL_COMPLETED,
            {"tool": "smtp", "correlation_id": "cli-1"},
            source=Origin.EXTERNAL_AGENT,
        )
        store.append_event(
            "run_1", EventType.TOOL_COMPLETED, {"tool": "other"}, source=Origin.EXTERNAL_AGENT
        )
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", db, "--json", "correlate", "run_1"], out=out, err=err)
    assert code == 0, err.getvalue()
    payload = json.loads(out.getvalue())
    assert set(payload["chains"]) == {"cli-1"}
    assert len(payload["unmatched_tool"]) == 1
    out2, err2 = io.StringIO(), io.StringIO()
    code2 = main(
        ["--db", db, "--json", "correlate", "run_1", "--correlation-id", "cli-1"],
        out=out2,
        err=err2,
    )
    assert code2 == 0, err2.getvalue()
    assert set(json.loads(out2.getvalue())["chains"]) == {"cli-1"}
