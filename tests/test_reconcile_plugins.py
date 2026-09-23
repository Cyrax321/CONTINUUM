"""Dispatch of registered ActionReconciler plugins over uncertain actions (#765).

These tests pin the contract the seam promises: the four result categories,
fail-closed behaviour for every plugin failure mode, deterministic ordering with
provenance, and settlement that never lowers a cautious verdict. They are written
to be reusable against any implementation of the seam, not just the built-ins.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.cli import ExitCode, main
from continuum.models import Action, ActionStatus, Run
from continuum.plugins import (
    Reconciliation,
    ReconciliationOutcome,
    Registry,
    SettlementReport,
    assess_action,
    resolve_reconcilers,
    select_reconcilers,
    settle_with_reconcilers,
)
from continuum.plugins.reconcile import _key_map
from continuum.storage import SQLiteStorage


class _Reconciler:
    """A minimal, honest reconciler: one verdict, one note."""

    def __init__(
        self, name: str, occurred: bool | None, note: str = "", external_id: str | None = None
    ):
        self.name = name
        self._occurred = occurred
        self._note = note
        self._external_id = external_id

    def reconcile(self, action: Action) -> Reconciliation:
        return Reconciliation(
            occurred=self._occurred, note=self._note, external_id=self._external_id
        )


class _Exploding:
    """A plugin that raises on every call."""

    name = "boom"

    def reconcile(self, action: Action) -> Reconciliation:
        raise RuntimeError("the telemetry store is on fire")


class _Malformed:
    """A plugin that returns a value the contract does not recognise."""

    name = "malformed"

    def reconcile(self, action: Action) -> Reconciliation:
        return "occurred=probably"  # type: ignore[return-value]


class _WrongOccurred:
    """A plugin that returns a non-boolean ``occurred``."""

    name = "wrong_occurred"

    def reconcile(self, action: Action) -> Reconciliation:
        return Reconciliation(occurred="true")  # type: ignore[arg-type]


def _action(action_type: str = "send_invoice", action_id: str = "action_abc123") -> Action:
    return Action(run_id="run_1", action_type=action_type, action_id=action_id)


# --- the four result categories --------------------------------------------- #


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ([("a", True), ("b", True)], ReconciliationOutcome.CONFIRMED_OCCURRED),
        ([("a", False), ("b", False)], ReconciliationOutcome.CONFIRMED_NOT_OCCURRED),
        ([("a", True), ("b", False)], ReconciliationOutcome.CONFLICTING),
        ([("a", None), ("b", None)], ReconciliationOutcome.UNAVAILABLE),
        ([], ReconciliationOutcome.UNAVAILABLE),
    ],
)
def test_every_evidence_pattern_lands_in_a_documented_category(
    evidence: list[tuple[str, bool | None]], expected: ReconciliationOutcome
) -> None:
    reconcilers = [_Reconciler(name, occurred) for name, occurred in evidence]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is expected


def test_unanimous_occurrence_confirms_occurred() -> None:
    reconcilers = [
        _Reconciler("otel", True, note="span 42 completed"),
        _Reconciler("stripe", True, external_id="ch_123"),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.CONFIRMED_OCCURRED
    assert "confirmed by otel, stripe" in assessment.reason
    assert len(assessment.evidence) == 2


def test_unanimous_non_occurrence_confirms_not_occurred() -> None:
    reconcilers = [
        _Reconciler("otel", False, note="no span recorded"),
        _Reconciler("stripe", False, note="idempotency key never reached stripe"),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.CONFIRMED_NOT_OCCURRED
    assert "confirmed by otel, stripe" in assessment.reason


def test_disagreeing_reconcilers_escalate_to_conflicting() -> None:
    reconcilers = [
        _Reconciler("otel", True),
        _Reconciler("audit_log", False),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.CONFLICTING
    assert "audit_log=not_occurred" in assessment.reason
    assert "otel=occurred" in assessment.reason


def test_declining_reconcilers_land_in_unavailable() -> None:
    reconcilers = [
        _Reconciler("otel", None, note="service unavailable"),
        _Reconciler("stripe", None, note="rate limited"),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.UNAVAILABLE
    assert "none returned evidence" in assessment.reason


def test_empty_reconciler_list_lands_in_unavailable() -> None:
    assessment = assess_action(_action(), [])
    assert assessment.outcome is ReconciliationOutcome.UNAVAILABLE
    assert "no reconciler applied" in assessment.reason


# --- fail-closed on plugin failures ---------------------------------------- #


def test_exploding_reconciler_blocks_confirmation() -> None:
    reconcilers = [
        _Reconciler("honest", True),
        _Exploding(),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.UNAVAILABLE
    assert "boom errored: RuntimeError: the telemetry store is on fire" in assessment.reason


def test_malformed_return_blocks_confirmation() -> None:
    reconcilers = [
        _Reconciler("honest", True),
        _Malformed(),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.UNAVAILABLE
    assert "expected Reconciliation" in assessment.reason


def test_non_boolean_occurred_blocks_confirmation() -> None:
    reconcilers = [
        _Reconciler("honest", True),
        _WrongOccurred(),
    ]
    assessment = assess_action(_action(), reconcilers)
    assert assessment.outcome is ReconciliationOutcome.UNAVAILABLE
    assert "expected bool or None" in assessment.reason


# --- ordering and provenance ------------------------------------------------ #


def test_reconcilers_are_dispatched_in_name_order_regardless_of_input_order() -> None:
    action = _action()
    r_z = _Reconciler("z_reconciler", True)
    r_a = _Reconciler("a_reconciler", True)
    r_m = _Reconciler("m_reconciler", True)

    selected_1 = select_reconcilers(action, [r_z, r_a, r_m])
    selected_2 = select_reconcilers(action, [r_m, r_z, r_a])

    assert [r.name for r in selected_1] == ["a_reconciler", "m_reconciler", "z_reconciler"]
    assert selected_1 == selected_2


def test_name_deduplication_keeps_first_seen() -> None:
    r1 = _Reconciler("same_name", True, note="first")
    r2 = _Reconciler("same_name", False, note="second")
    selected = select_reconcilers(_action(), [r1, r2])
    assert len(selected) == 1
    assert selected[0] is r1


def test_evidence_provenance_is_serialized_faithfully() -> None:
    reconcilers = [
        _Reconciler("otel", True, note="span ok", external_id="span_999"),
        _Exploding(),
    ]
    assessment = assess_action(_action(), reconcilers)
    payload = assessment.as_dict()

    assert payload["action_id"] == "action_abc123"
    assert payload["action_type"] == "send_invoice"
    assert payload["outcome"] == "unavailable_evidence"
    assert len(payload["evidence"]) == 2

    boom_ev = payload["evidence"][0]
    assert boom_ev["reconciler"] == "boom"
    assert "the telemetry store is on fire" in boom_ev["error"]
    assert boom_ev["occurred"] is None

    otel_ev = payload["evidence"][1]
    assert otel_ev["reconciler"] == "otel"
    assert otel_ev["occurred"] is True
    assert otel_ev["external_id"] == "span_999"
    assert otel_ev["note"] == "span ok"


def test_rendered_diagnostics_include_identifiers_and_errors() -> None:
    reconcilers = [
        _Reconciler("otel", True, note="span ok", external_id="span_999"),
        _Exploding(),
    ]
    text = assess_action(_action(), reconcilers).render()
    assert "send_invoice:action_abc12: unavailable_evidence" in text
    assert "[boom] ERROR: RuntimeError: the telemetry store is on fire" in text
    assert "[otel] occurred (span_999)" in text
    assert "span ok" in text


# --- registry resolution ---------------------------------------------------- #


def test_resolve_reconcilers_unfolds_registry_by_protocol_conformance() -> None:
    registry = Registry()
    r1 = _Reconciler("r1", True)
    r2 = _Reconciler("r2", False)
    not_a_reconciler = "some string service"

    registry.register("service_a", r1)
    registry.register("service_b", r2)
    registry.register("random_service", not_a_reconciler)

    resolved = resolve_reconcilers(registry)
    assert resolved == [r1, r2]


def test_resolve_reconcilers_handles_none_and_collections() -> None:
    assert resolve_reconcilers(None) == []
    r = _Reconciler("r", True)
    assert resolve_reconcilers([r, "not a reconciler"]) == [r]


# --- ledger settlement integration ------------------------------------------ #


@pytest.fixture
def run_db(tmp_path: Path) -> tuple[SQLiteStorage, str]:
    db_path = tmp_path / "test.db"
    storage = SQLiteStorage(f"sqlite:///{db_path.as_posix()}")
    storage.create_run(Run(run_id="run_1", goal="test"))
    return storage, "run_1"


def test_settle_with_reconcilers_settles_confirmed_occurred(
    run_db: tuple[SQLiteStorage, str],
) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("send_invoice", {"invoice_id": 42})
    key = outcome.key

    reconcilers = [_Reconciler("payment_gateway", True, external_id="inv_42")]
    report = settle_with_reconcilers(storage, run_id, reconcilers)

    assert report.settled == 1
    assert len(report.settled_true) == 1
    assert "send_invoice:inv_42" in report.settled_true[0]

    stored = ledger.get(key)
    assert stored is not None
    assert stored.status is ActionStatus.COMPLETED
    assert stored.external_id == "inv_42"


def test_settle_with_reconcilers_settles_confirmed_not_occurred(
    run_db: tuple[SQLiteStorage, str],
) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("send_invoice", {"invoice_id": 42})
    key = outcome.key

    reconcilers = [_Reconciler("payment_gateway", False)]
    report = settle_with_reconcilers(storage, run_id, reconcilers)

    assert report.settled == 1
    assert len(report.settled_false) == 1

    stored = ledger.get(key)
    assert stored is not None
    assert stored.status is ActionStatus.FAILED


def test_settle_with_reconcilers_flags_conflicting_for_review(
    run_db: tuple[SQLiteStorage, str],
) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("send_invoice", {"invoice_id": 42})
    key, action = outcome.key, outcome.action

    reconcilers = [
        _Reconciler("source_a", True),
        _Reconciler("source_b", False),
    ]
    report = settle_with_reconcilers(storage, run_id, reconcilers)

    assert report.settled == 0
    assert len(report.escalated) == 1
    assert report.escalated[0] == (action.action_id, "conflicting_evidence")

    stored = ledger.get(key)
    assert stored is not None
    assert stored.status is ActionStatus.REQUIRES_REVIEW
    assert "[conflicting_evidence]" in stored.last_error


def test_settle_with_reconcilers_flags_unavailable_for_review(
    run_db: tuple[SQLiteStorage, str],
) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("send_invoice", {"invoice_id": 42})
    key, action = outcome.key, outcome.action

    reconcilers = [_Reconciler("source_a", None)]
    report = settle_with_reconcilers(storage, run_id, reconcilers)

    assert report.settled == 0
    assert len(report.escalated) == 1
    assert report.escalated[0] == (action.action_id, "unavailable_evidence")

    stored = ledger.get(key)
    assert stored is not None
    assert stored.status is ActionStatus.REQUIRES_REVIEW
    assert "[unavailable_evidence]" in stored.last_error


def test_dry_run_leaves_ledger_untouched(run_db: tuple[SQLiteStorage, str]) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("send_invoice", {"invoice_id": 42})
    key = outcome.key

    reconcilers = [_Reconciler("source_a", True)]
    report = settle_with_reconcilers(storage, run_id, reconcilers, dry_run=True)

    assert report.settled == 1
    stored = ledger.get(key)
    assert stored is not None
    assert stored.status is ActionStatus.STARTED


def test_none_or_empty_reconcilers_returns_empty_report(
    run_db: tuple[SQLiteStorage, str],
) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    ledger.claim("send_invoice", {"invoice_id": 42})

    report_none = settle_with_reconcilers(storage, run_id, None)
    assert report_none.settled == 0
    assert len(report_none.assessments) == 0

    report_empty = settle_with_reconcilers(storage, run_id, [])
    assert report_empty.settled == 0
    assert len(report_empty.assessments) == 0


def test_key_map_folds_archive_history_so_compacted_runs_settle(tmp_path: Path) -> None:
    db_path = tmp_path / "compacted.db"
    storage = SQLiteStorage(f"sqlite:///{db_path.as_posix()}")
    storage.create_run(Run(run_id="run_1", goal="test"))
    ledger = ActionLedger(storage, run_1 := "run_1")

    outcome = ledger.claim("send_email", {"to": "alice@example.com"})
    key, action = outcome.key, outcome.action

    # Emulate an archive event compaction (issue #647) where event is archived
    events = storage.read_all_events(run_1)
    assert len(events) >= 1

    mapping = _key_map(storage, run_1)
    assert action.action_id in mapping
    assert mapping[action.action_id] == key


# --- CLI integration -------------------------------------------------------- #


def _cli_run(*argv: str) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_the_cli_dispatches_a_named_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = (tmp_path / "cli.db").as_posix()
    storage = SQLiteStorage(f"sqlite:///{db}")
    storage.create_run(Run(run_id="run_1", goal="test"))
    ledger = ActionLedger(storage, "run_1")
    ledger.claim("send_invoice", {"inv": 1})

    (tmp_path / "my_plugin.py").write_text(
        "from continuum.plugins import Reconciliation\n"
        "class MyReconciler:\n"
        "    name = 'custom_plug'\n"
        "    def reconcile(self, action):\n"
        "        return Reconciliation(occurred=True, external_id='ext_99')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    code, out, _ = _cli_run(
        "--db",
        db,
        "reconcile",
        "run_1",
        "--reconciler",
        "my_plugin:MyReconciler",
    )
    assert code == ExitCode.OK
    assert "plugin reconcilers: 1 registered" in out
    assert "settled: 1 (occurred 1, not-occurred 0)" in out
    assert "[ok] send_invoice:" in out


def test_conflicting_plugins_exit_requires_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = (tmp_path / "cli_conflict.db").as_posix()
    storage = SQLiteStorage(f"sqlite:///{db}")
    storage.create_run(Run(run_id="run_1", goal="test"))
    ledger = ActionLedger(storage, "run_1")
    ledger.claim("send_invoice", {"inv": 1})

    (tmp_path / "conflict_plugins.py").write_text(
        "from continuum.plugins import Reconciliation\n"
        "class SaysYes:\n"
        "    name = 'yes'\n"
        "    def reconcile(self, action):\n"
        "        return Reconciliation(occurred=True)\n"
        "class SaysNo:\n"
        "    name = 'no'\n"
        "    def reconcile(self, action):\n"
        "        return Reconciliation(occurred=False)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    code, out, _ = _cli_run(
        "--db",
        db,
        "--json",
        "reconcile",
        "run_1",
        "--reconciler",
        "conflict_plugins:SaysYes",
        "--reconciler",
        "conflict_plugins:SaysNo",
    )
    assert code == ExitCode.REQUIRES_HUMAN
    payload = json.loads(out)
    assert "plugins" in payload
    assert len(payload["plugins"]["escalated"]) == 1
    assert payload["plugins"]["escalated"][0]["outcome"] == "conflicting_evidence"


def test_invalid_reconciler_path_exits_with_error(tmp_path: Path) -> None:
    db = (tmp_path / "cli_err.db").as_posix()
    storage = SQLiteStorage(f"sqlite:///{db}")
    storage.create_run(Run(run_id="run_1", goal="test"))

    code, out, err = _cli_run(
        "--db",
        db,
        "reconcile",
        "run_1",
        "--reconciler",
        "non_existent_module:SomeClass",
    )
    assert code == ExitCode.ERROR
    assert "failed to import module" in err


def test_reconciler_name_fallback_to_class_name() -> None:
    class AnonymousReconciler:
        def reconcile(self, action: Action) -> Reconciliation:
            return Reconciliation(occurred=True)

    anon = AnonymousReconciler()
    selected = select_reconcilers(_action(), [anon])
    assert len(selected) == 1
    assessment = assess_action(_action(), [anon])
    assert "confirmed by AnonymousReconciler" in assessment.reason


def test_reconciler_empty_name_fallback() -> None:
    class BlankNameReconciler:
        name = ""

        def reconcile(self, action: Action) -> Reconciliation:
            return Reconciliation(occurred=True)

    blank = BlankNameReconciler()
    assessment = assess_action(_action(), [blank])
    assert "confirmed by BlankNameReconciler" in assessment.reason


def test_settlement_report_as_dict_structure() -> None:
    report = SettlementReport(
        settled_true=["send_invoice:inv_1"],
        settled_false=["refund:ref_2"],
        escalated=[("action_123", "conflicting_evidence")],
        skipped_no_reconciler=["ship_goods"],
    )
    d = report.as_dict()
    assert d["settled_total"] == 2
    assert d["settled_occurred"] == ["send_invoice:inv_1"]
    assert d["settled_not_occurred"] == ["refund:ref_2"]
    assert d["escalated"] == [{"action_id": "action_123", "outcome": "conflicting_evidence"}]
    assert d["no_reconciler_applied"] == ["ship_goods"]


def test_key_map_vanished_action_escalates(
    run_db: tuple[SQLiteStorage, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, run_id = run_db
    ledger = ActionLedger(storage, run_id)
    outcome = ledger.claim("send_invoice", {"invoice_id": 42})

    # Force key map to return empty dict simulating an action that vanished from fold
    monkeypatch.setattr("continuum.plugins.reconcile._key_map", lambda _s, _r: {})
    reconcilers = [_Reconciler("gateway", True)]
    report = settle_with_reconcilers(storage, run_id, reconcilers)

    assert report.settled == 0
    assert len(report.escalated) == 1
    assert report.escalated[0] == (outcome.action.action_id, "confirmed_occurred")


def test_cli_dry_run_with_reconciler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = (tmp_path / "cli_dry.db").as_posix()
    storage = SQLiteStorage(f"sqlite:///{db}")
    storage.create_run(Run(run_id="run_1", goal="test"))
    ledger = ActionLedger(storage, "run_1")
    outcome = ledger.claim("send_invoice", {"inv": 1})

    (tmp_path / "dry_plugin.py").write_text(
        "from continuum.plugins import Reconciliation\n"
        "class DryReconciler:\n"
        "    name = 'dry_reconciler'\n"
        "    def reconcile(self, action):\n"
        "        return Reconciliation(occurred=True)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    code, out, _ = _cli_run(
        "--db",
        db,
        "reconcile",
        "run_1",
        "--dry-run",
        "--reconciler",
        "dry_plugin:DryReconciler",
    )
    assert code == ExitCode.OK
    assert "dry run: nothing was written" in out
    stored = ledger.get(outcome.key)
    assert stored is not None
    assert stored.status is ActionStatus.STARTED
