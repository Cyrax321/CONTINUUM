"""Registered reconciliation probes (issue #218).

A probe is an operator-configured command that checks the external system
for one action type and prints a verdict. These tests pin the parsing
contract, the settle loop against the real ledger, the provenance of
auto-settled events, and the CLI's exit-code behaviour.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from continuum import reconcilers
from continuum.actions import ActionLedger
from continuum.actions.authority import record_authority_consumed
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Origin, Run
from continuum.reconcilers import (
    _AUTHORITY_VERDICT_CONTRACT,
    _DEFAULT_TIMEOUT,
    _VERDICT_CONTRACT,
    AuthoritySettleReport,
    ReconcilerConfigError,
    _parse_authority_verdict,
    _parse_verdict,
    _verdict_hint,
    load_reconcilers,
    probe_authority_verdict,
    probe_verdict,
    settle_authority,
    settle_run,
)
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "rec.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


def seed_pending(db: str, key: str = "invoice:1") -> str:
    """Claim (and leave uncertain) one action; returns its action_id."""
    with SQLiteStorage(db) as store:
        outcome = ActionLedger(store, "run_1").claim(
            "send_invoice", {}, key=key, scoped_to_run=True
        )
    assert outcome.fresh is True
    return outcome.action.action_id


# --- config loading ------------------------------------------------------------ #


def test_missing_registry_loads_empty(tmp_path: Path) -> None:
    assert load_reconcilers(tmp_path / "nope.json") == {}


def test_broken_json_raises_instead_of_degrading(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    p.write_text("{")
    with pytest.raises(ReconcilerConfigError, match="not valid JSON"):
        load_reconcilers(p)


def test_probe_requires_a_command_and_positive_timeout(tmp_path: Path) -> None:
    """The two arms a probe spec has to clear before it is registered at all."""
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"probes": {"send_invoice": {"timeout": 5}}}))
    with pytest.raises(ReconcilerConfigError, match="command"):
        load_reconcilers(p)
    p.write_text(json.dumps({"probes": {"send_invoice": {"command": "x", "timeout": -1}}}))
    with pytest.raises(ReconcilerConfigError, match="timeout"):
        load_reconcilers(p)


def test_a_boolean_timeout_is_refused_rather_than_read_as_one_second(tmp_path: Path) -> None:
    """``bool`` is an ``int`` in Python, and JSON ``true`` reaches this loader.

    Without an explicit check it passes the numeric arm above and registers as a
    one second timeout, so every probe is killed just after it starts: the run
    still ends in the human queue, but reported as an external system nobody can
    reach instead of the registry typo it is (issue #322).
    """
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"probes": {"send_invoice": {"command": "x", "timeout": True}}}))
    with pytest.raises(ReconcilerConfigError, match="positive number"):
        load_reconcilers(p)


def test_probes_not_wrapped_under_the_probes_key_is_refused(tmp_path: Path) -> None:
    """A registry that maps action types at the top level is the wrong shape.

    ``raw.get("probes", {})`` and ``raw.get("probes") or {}`` both default to an
    empty dict for a file missing the ``probes`` key entirely, so this used to
    load silently as an empty registry instead of raising: every action of that
    type then read as having no probe registered, with nothing pointing at the
    missing wrapper as the cause (issue #1062).
    """
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"send_invoice": {"command": "check-outbox", "timeout": 5}}))
    with pytest.raises(ReconcilerConfigError, match="send_invoice"):
        load_reconcilers(p)


def test_an_empty_registry_file_is_still_a_valid_empty_registry(tmp_path: Path) -> None:
    """An empty dict has no top-level keys to mistake for a missing wrapper."""
    p = tmp_path / "r.json"
    p.write_text("{}")
    assert load_reconcilers(p) == {}


def test_unknown_probe_type_is_refused(tmp_path: Path) -> None:
    """A probe with an unrecognized type raises ReconcilerConfigError."""
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"probes": {"act": {"type": "unsupported_type"}}}))
    with pytest.raises(ReconcilerConfigError, match="unknown type 'unsupported_type'"):
        load_reconcilers(p)


def test_builtin_probe_rejects_unexpected_keys(tmp_path: Path) -> None:
    """A built-in probe rejects keys outside its allowed set."""
    p = tmp_path / "r.json"
    p.write_text(
        json.dumps(
            {
                "probes": {
                    "act": {
                        "type": "otel_span",
                        "tool": "my_tool",
                        "disallowed_key": "val",
                    }
                }
            }
        )
    )
    with pytest.raises(ReconcilerConfigError, match="does not accept \\['disallowed_key'\\]"):
        load_reconcilers(p)


def test_artifact_check_probe_requires_path_or_path_key(tmp_path: Path) -> None:
    """artifact_check probe requires either path or path_key."""
    p = tmp_path / "r.json"
    p.write_text(
        json.dumps({"probes": {"act": {"type": "artifact_check", "expect_sha256": "abc"}}})
    )
    with pytest.raises(ReconcilerConfigError, match="needs 'path' or 'path_key'"):
        load_reconcilers(p)


def test_builtin_probes_valid_configurations_load_successfully(tmp_path: Path) -> None:
    """Valid built-in probe configurations load correctly."""
    p = tmp_path / "r.json"
    p.write_text(
        json.dumps(
            {
                "probes": {
                    "act_path": {"type": "artifact_check", "path": "/tmp/out.txt"},
                    "act_key": {"type": "artifact_check", "path_key": "output_file"},
                    "act_otel": {"type": "otel_span", "tool": "query", "identity": "arg1"},
                }
            }
        )
    )
    loaded = load_reconcilers(p)
    assert loaded["act_path"] == {"type": "artifact_check", "path": "/tmp/out.txt"}
    assert loaded["act_key"] == {"type": "artifact_check", "path_key": "output_file"}
    assert loaded["act_otel"] == {"type": "otel_span", "tool": "query", "identity": "arg1"}


# --- verdict parsing -------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("occurred=true", True),
        ("occurred=false", False),
        ("OCCURRED=FALSE", False),
        ('{"occurred": true}', True),
        ('{"occurred": null}', "unknown"),
        ("true", True),
        ("garbage", "unknown"),
        ("", "unknown"),
    ],
)
def test_verdict_parsing(line: str, expected: bool | str) -> None:
    assert (
        _parse_verdict(line) is expected
        if expected != "unknown"
        else _parse_verdict(line) == "unknown"
    )


def test_last_line_wins() -> None:
    assert _parse_verdict("noise\nmore noise\noccurred=false") is False


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("valid=true", True),
        ("VALID=TRUE", True),
        ("valid=false", False),
        ("VALID=FALSE", False),
        ("valid=unknown", "unknown"),
        ("valid=other", "unknown"),
        ('{"valid": true}', True),
        ('{"valid": false}', False),
        ('{"valid": null}', "unknown"),
        ('{"valid": "unknown"}', "unknown"),
        ('{"valid": "UNKNOWN"}', "unknown"),
        ('{"valid": "other"}', "unknown"),
        ('{"valid": 123}', "unknown"),
        ("[1, 2, 3]", "unknown"),
        ("not json", "unknown"),
        ("", "unknown"),
        ("   ", "unknown"),
    ],
)
def test_authority_verdict_parsing(line: str, expected: bool | str) -> None:
    assert (
        _parse_authority_verdict(line) is expected
        if expected != "unknown"
        else _parse_authority_verdict(line) == "unknown"
    )


def test_authority_verdict_last_line_wins() -> None:
    """Only the last non-empty line of output determines the authority verdict."""
    output = "checking authority...\npreliminary info\nvalid=true"
    assert _parse_authority_verdict(output) is True
    output_json = 'checking authority...\npreliminary info\n{"valid": false}'
    assert _parse_authority_verdict(output_json) is False


@pytest.mark.parametrize("line", ["occurred:false", "occurred:true", "OCCURRED:FALSE"])
def test_a_colon_separated_verdict_is_not_a_verdict(line: str) -> None:
    """``occurred:false`` is the shape a shell ``echo`` suggests, not an answer.

    The contract's separator is ``=``; accepting a colon would be generous in
    the wrong direction, since a probe printing ``occurred:false`` in a config
    dialect means something the caller should state explicitly. It reads as
    unknown, and the diagnostic -- not the parse -- is what has to save the
    operator's time.
    """
    assert _parse_verdict(line) == "unknown"


def test_the_diagnostic_names_the_colon_not_the_probe() -> None:
    """``occurred:false`` used to report generic unparseable output.

    That sends an operator hunting in their probe's logic when the cause is one
    character away. The message now names the separator and shows the line to
    print instead.
    """
    hint = _verdict_hint(
        "checking outbox...\noccurred:false", "occurred", "occurred=true or occurred=false"
    )
    assert hint is not None
    assert ":" in hint
    assert "occurred=false" in hint
    assert "occurred:false" in hint


def test_the_diagnostic_stays_silent_on_output_that_is_not_an_attempted_verdict() -> None:
    """A colon in unrelated output, or the other parser's field, is not a miss."""
    assert _verdict_hint("invoice I-42 not found", "occurred", _VERDICT_CONTRACT) is None
    assert _verdict_hint("", "occurred", _VERDICT_CONTRACT) is None
    # An authority verdict line is not an action verdict near-miss, and vice versa.
    assert _verdict_hint("valid:false", "occurred", _VERDICT_CONTRACT) is None
    assert _verdict_hint("occurred:false", "valid", _AUTHORITY_VERDICT_CONTRACT) is None


def test_probe_verdict_names_the_colon_in_its_detail() -> None:
    action = Action(
        run_id="run_1",
        action_type="send_invoice",
        arguments={},
        status=ActionStatus.STARTED,
    )
    verdict, detail = probe_verdict({"command": "echo occurred:false", "timeout": 5}, action)
    assert verdict is None
    assert "the verdict uses ':'" in detail
    assert "occurred=false" in detail


def test_probe_verdict_states_the_contract_on_any_other_miss() -> None:
    action = Action(
        run_id="run_1",
        action_type="send_invoice",
        arguments={},
        status=ActionStatus.STARTED,
    )
    verdict, detail = probe_verdict({"command": "echo not a verdict", "timeout": 5}, action)
    assert verdict is None
    assert "could not determine the outcome" in detail
    for form in ("occurred=true", "occurred=false", "occurred=unknown"):
        assert form in detail


def test_an_authority_probes_colon_verdict_names_the_colon_too() -> None:
    """``probe_authority_verdict`` parses ``valid=`` with the same shape and had
    the same opaque message; the diagnosis is not action-probe specific.
    """
    verdict, detail = probe_authority_verdict(
        {"command": "echo valid:false", "timeout": 5}, {"authority_id": "approval-7"}
    )
    assert verdict is None
    assert "the verdict uses ':'" in detail
    assert "valid=false" in detail


# --- settle loop -------------------------------------------------------------------- #


def registry(tmp_path: Path, entries: dict[str, str]) -> Path:
    p = tmp_path / "reconcilers.json"
    p.write_text(
        json.dumps({"probes": {k: {"command": v, "timeout": 5} for k, v in entries.items()}})
    )
    return p


def test_a_definitive_probe_settles_the_action(db: str, tmp_path: Path) -> None:
    seed_pending(db, key="invoice:1")
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=true"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes)
    assert report.settled_true and report.settled == 1
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_events("run_1"))
    (action,) = folded.values()
    assert action.status is ActionStatus.COMPLETED


def test_false_frees_the_action_for_retry(db: str, tmp_path: Path) -> None:
    seed_pending(db, key="invoice:2")
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=false"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes)
    assert report.settled_false
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        (action,) = fold_action_events(store.read_events("run_1")).values()
    assert action.status is ActionStatus.FAILED


def test_an_unknown_verdict_leaves_the_action_untouched(db: str, tmp_path: Path) -> None:
    seed_pending(db)
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=unknown"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes)
    assert not report.settled
    assert len(report.unresolved) == 1
    assert "could not determine" in report.unresolved[0][1]


def test_probe_failures_never_settle(db: str, tmp_path: Path) -> None:
    seed_pending(db)
    cases = {
        "nonzero": "exit 3",
        "junk output": "echo hello",
        "timeout": "sleep 30",
    }
    for name, command in cases.items():
        entry = {} if name != "timeout" else {"command": command, "timeout": 0.2}
        probes = {**load_reconcilers(registry(tmp_path, {"send_invoice": command}))}
        if name == "timeout":
            probes = {"send_invoice": {"command": command, "timeout": 0.2}}
        report = settle_run(SQLiteStorage(db), "run_1", probes)
        del entry
        assert not report.settled, name
        assert report.unresolved and report.unresolved[0][0] == "send_invoice", name


def test_actions_without_registered_probes_are_skipped(db: str, tmp_path: Path) -> None:
    seed_pending(db)
    probes = load_reconcilers(registry(tmp_path, {"other_tool": "echo occurred=true"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes)
    assert not report.settled
    assert len(report.skipped_no_probe) == 1


def test_dry_run_reports_without_writing(db: str, tmp_path: Path) -> None:
    seed_pending(db)
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=true"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes, dry_run=True)
    assert report.settled == 1
    with SQLiteStorage(db) as store:
        (action,) = ActionLedger(store, "run_1").all()
    assert action.status is ActionStatus.STARTED


def test_settled_events_are_sourced_deterministic(db: str, tmp_path: Path) -> None:
    seed_pending(db)
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=true"}))
    settle_run(SQLiteStorage(db), "run_1", probes)
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    reconciled = [e for e in events if e.type is EventType.ACTION_RECONCILED]
    assert reconciled, "expected an ACTION_RECONCILED event"
    assert all(e.source is Origin.DETERMINISTIC for e in reconciled)


def test_settle_run_settles_archived_actions_after_compaction(db: str, tmp_path: Path) -> None:
    """Compaction must not break the operator path for clearing risk (issue #647).

    pending() is archive-aware, so an action claimed before a compaction is
    still listed; settle_run used to resolve its ledger key from the live tail
    only and abort the whole report with LookupError.
    """
    action_id = seed_pending(db, key="invoice:9")
    with SQLiteStorage(db) as store:
        store.compact_run("run_1", through_sequence=store.last_sequence("run_1"))
        assert action_id in {a.action_id for a in ActionLedger(store, "run_1").pending()}
    probes = load_reconcilers(registry(tmp_path, {"send_invoice": "echo occurred=true"}))
    report = settle_run(SQLiteStorage(db), "run_1", probes)
    assert report.settled == 1 and report.settled_true
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        folded = fold_action_events(store.read_all_events("run_1"))
    action = next(a for a in folded.values() if a.action_id == action_id)
    assert action.status is ActionStatus.COMPLETED


# --- authority probes (issue #1994) ------------------------------------------------- #


def python_probe(code: str) -> str:
    """A probe command running *code* in this interpreter, on any shell.

    ``code`` sticks to single quotes, which survive both sh grouping and the C
    runtime unescaping behind cmd. stderr and exit codes cannot be scripted
    with an ``echo`` that behaves the same on both.
    """
    return f'"{sys.executable}" -c "{code}"'


@pytest.mark.parametrize(
    "text",
    [
        '{"valid": tru',
        '{"other": true}',
        '{"valid": "true"}',
        "true",
        "valid=true\nnot a verdict",
    ],
)
def test_authority_verdict_is_unknown_for_anything_short_of_a_verdict(text: str) -> None:
    """Settling clears a consumed authority, so a near miss has to stay blocked.

    The bare ``true`` is the one the action parser accepts and this one does not,
    and an earlier ``valid=true`` line does not survive later noise.
    """
    assert _parse_authority_verdict(text) == "unknown"


def test_authority_probe_past_its_timeout_is_an_error() -> None:
    verdict, detail = probe_authority_verdict(
        {"command": python_probe("import time; time.sleep(1)"), "timeout": 0.1},
        {"authority_id": "approval-7"},
    )
    assert verdict == "error"
    assert detail == "authority probe timed out after 0.1s"


def test_authority_probe_that_cannot_start_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patched because a shell launch only raises OSError when the shell itself is gone."""

    def cannot_start(*args: object, **kwargs: object) -> None:
        raise OSError("no shell")

    monkeypatch.setattr(reconcilers.subprocess, "run", cannot_start)
    verdict, detail = probe_authority_verdict(
        {"command": "anything", "timeout": 5}, {"authority_id": "approval-7"}
    )
    assert verdict == "error"
    assert detail == "authority probe failed to run: no shell"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import sys; sys.stderr.write('denied'); sys.exit(3)", "exited 3: denied"),
        ("import sys; print('out'); sys.stderr.write('denied'); sys.exit(4)", "exited 4: denied"),
        ("import sys; print('valid=true'); sys.exit(5)", "exited 5: valid=true"),
        ("import sys; sys.exit(6)", "exited 6:"),
        ("import sys; sys.stderr.write('x' * 500); sys.exit(7)", f"exited 7: {'x' * 200}"),
    ],
)
def test_authority_probe_nonzero_exit_is_an_error_whatever_it_printed(
    code: str, expected: str
) -> None:
    """A failed probe cannot vouch for an authority, even by printing ``valid=true``."""
    verdict, detail = probe_authority_verdict(
        {"command": python_probe(code), "timeout": 5}, {"authority_id": "approval-7"}
    )
    assert verdict == "error"
    assert detail.rstrip() == f"authority probe {expected}"


@pytest.mark.parametrize(
    ("command", "shown"),
    [
        ("echo not a verdict", "not a verdict"),
        ("echo valid=unknown", "valid=unknown"),
        (python_probe("pass"), ""),
        (python_probe("print('x' * 500)"), "x" * 120),
    ],
)
def test_authority_probe_miss_quotes_the_output_it_could_not_read(command: str, shown: str) -> None:
    verdict, detail = probe_authority_verdict(
        {"command": command, "timeout": 5}, {"authority_id": "approval-7"}
    )
    assert verdict is None
    assert detail == f"authority probe could not determine validity from output {shown!r}"


def test_authority_probe_hint_quotes_the_output_and_the_line_it_blames() -> None:
    verdict, detail = probe_authority_verdict(
        {"command": python_probe("print('checking'); print('valid:true')"), "timeout": 5},
        {"authority_id": "approval-7"},
    )
    assert verdict is None
    assert detail.startswith("authority probe printed 'checking\\nvalid:true'; ")
    assert "rather than 'valid:true'" in detail
    assert _AUTHORITY_VERDICT_CONTRACT in detail


def test_authority_probe_reports_a_none_verdict_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patched because the real parser returns ``"unknown"``, never the ``None`` it is typed for."""
    monkeypatch.setattr(reconcilers, "_parse_authority_verdict", lambda text: None)
    verdict, detail = probe_authority_verdict(
        {"command": "echo anything", "timeout": 5}, {"authority_id": "approval-7"}
    )
    assert verdict is None
    assert detail == "authority probe returned unknown"


def test_authority_probe_gets_its_payload_on_stdin() -> None:
    spec = {
        "command": python_probe(
            "import json, sys; "
            "print('valid=' + str(json.load(sys.stdin)['authority_id'] == 'approval-7').lower())"
        ),
        "timeout": 5,
    }
    assert probe_authority_verdict(spec, {"authority_id": "approval-7"})[0] is True
    assert probe_authority_verdict(spec, {"authority_id": "approval-8"})[0] is False


def test_authority_report_as_dict() -> None:
    assert AuthoritySettleReport("approval-7", True, "ok", settled=True).as_dict() == {
        "authority_id": "approval-7",
        "valid": True,
        "detail": "ok",
        "settled": True,
    }


def test_settle_authority_without_a_matching_probe_appends_nothing(db: str) -> None:
    probes = {"approval-8": {"command": "echo valid=true", "timeout": 5}}
    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "approval-7")
        head = store.last_sequence("run_1")
        report = settle_authority(store, "run_1", "approval-7", probes)
        assert store.last_sequence("run_1") == head
    assert report == AuthoritySettleReport(
        "approval-7", None, "no probe registered for authority", settled=False
    )


@pytest.mark.parametrize(
    ("command", "detail"),
    [
        ("exit 3", "authority probe exited 3"),
        ("echo not a verdict", "could not determine validity"),
    ],
)
def test_settle_authority_leaves_it_unsettled_without_a_definitive_verdict(
    db: str, command: str, detail: str
) -> None:
    probes = {"approval-7": {"command": command, "timeout": 5}}
    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "approval-7")
        head = store.last_sequence("run_1")
        report = settle_authority(store, "run_1", "approval-7", probes)
        assert store.last_sequence("run_1") == head
    assert report.valid is None
    assert report.settled is False
    assert detail in report.detail


@pytest.mark.parametrize(("line", "valid"), [("valid=true", True), ("valid=false", False)])
def test_settle_authority_dry_run_reports_the_verdict_without_writing(
    db: str, line: str, valid: bool
) -> None:
    probes = {"approval-7": {"command": f"echo {line}", "timeout": 5}}
    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "approval-7")
        head = store.last_sequence("run_1")
        report = settle_authority(store, "run_1", "approval-7", probes, dry_run=True)
        assert store.last_sequence("run_1") == head
    assert report.valid is valid
    assert report.settled is False


@pytest.mark.parametrize(("line", "valid"), [("valid=true", True), ("valid=false", False)])
def test_settle_authority_appends_a_deterministic_reconciled_event(
    db: str, line: str, valid: bool
) -> None:
    code = f"import sys; sys.stderr.write('checked upstream'); print('{line}')"
    probes = {"approval-7": {"command": python_probe(code), "timeout": 5}}
    with SQLiteStorage(db) as store:
        consumed = record_authority_consumed(store, "run_1", "approval-7", via_action_id="act-1")
        report = settle_authority(store, "run_1", "approval-7", probes)
        events = store.read_events("run_1")
    assert report == AuthoritySettleReport("approval-7", valid, "checked upstream", settled=True)
    (reconciled,) = [e for e in events if e.type is EventType.AUTHORITY_RECONCILED]
    assert reconciled.source is Origin.DETERMINISTIC
    assert reconciled.payload == {
        "authority_id": "approval-7",
        "valid": valid,
        "reason": "checked upstream",
        "probed_payload": {
            "authority_id": "approval-7",
            "consumer_run_id": "run_1",
            "via_action_id": "act-1",
            "consumed_at": consumed.payload["consumed_at"],
            "sequence": consumed.sequence,
        },
    }


def test_settle_authority_probes_with_its_latest_consumption(db: str) -> None:
    """Duplicate consumptions are kept for audit, and another authority's row is not this one's."""
    probes = {"approval-7": {"command": "echo valid=true", "timeout": 5}}
    with SQLiteStorage(db) as store:
        record_authority_consumed(store, "run_1", "approval-7", via_action_id="act-1")
        latest = record_authority_consumed(store, "run_1", "approval-7", via_action_id="act-2")
        record_authority_consumed(store, "run_1", "approval-8", via_action_id="act-3")
        settle_authority(store, "run_1", "approval-7", probes)
        (reconciled,) = [
            e for e in store.read_events("run_1") if e.type is EventType.AUTHORITY_RECONCILED
        ]
    assert reconciled.payload["probed_payload"]["via_action_id"] == "act-2"
    assert reconciled.payload["probed_payload"]["sequence"] == latest.sequence


def test_settle_authority_prefers_its_own_probe_over_the_generic_one(db: str) -> None:
    generic = {"command": "echo valid=true", "timeout": 5}
    own = {"command": "echo valid=false", "timeout": 5}
    with SQLiteStorage(db) as store:
        report = settle_authority(
            store, "run_1", "approval-7", {"approval-7": own, "authority": generic}
        )
    assert report.valid is False


def test_settle_authority_falls_back_to_the_generic_probe_with_a_bare_id(db: str) -> None:
    """No consumption row to report, so the probe sees only the id it was asked about."""
    probes = {"authority": {"command": "echo valid=true", "timeout": 5}}
    with SQLiteStorage(db) as store:
        report = settle_authority(store, "run_1", "approval-7", probes)
        (reconciled,) = [
            e for e in store.read_events("run_1") if e.type is EventType.AUTHORITY_RECONCILED
        ]
    assert report.valid is True
    assert report.settled is True
    assert reconciled.payload["probed_payload"] == {"authority_id": "approval-7"}


# --- CLI ------------------------------------------------------------------------------ #


def test_cli_reconcile_exits_zero_when_everything_settles(db: str, tmp_path: Path) -> None:
    seed_pending(db, key="invoice:8")
    cfg = registry(tmp_path, {"send_invoice": "echo occurred=true"})
    code, out, err = run("--db", db, "--json", "reconcile", "run_1", "--config", str(cfg))
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["settled_total"] == 1
    assert payload["dry_run"] is False


def test_cli_reconcile_requires_human_when_probes_cannot_settle(db: str, tmp_path: Path) -> None:
    seed_pending(db)
    cfg = registry(tmp_path, {"other_tool": "echo occurred=true"})
    code, out, err = run("--db", db, "--json", "reconcile", "run_1", "--config", str(cfg))
    assert code == ExitCode.REQUIRES_HUMAN
    assert json.loads(out)["settled_total"] == 0


def test_cli_dry_run_writes_nothing(db: str, tmp_path: Path) -> None:
    seed_pending(db, key="invoice:9")
    cfg = registry(tmp_path, {"send_invoice": "echo occurred=true"})
    code, out, _ = run(
        "--db", db, "--json", "reconcile", "run_1", "--config", str(cfg), "--dry-run"
    )
    assert code == ExitCode.OK
    assert json.loads(out)["dry_run"] is True

    # Still pending afterwards.
    code, out, _ = run("--db", db, "--json", "actions", "run_1")
    actions = json.loads(out)["actions"]
    assert any(a["status"] == ActionStatus.STARTED.value for a in actions)


def test_cli_reconcile_unknown_run_is_not_found(tmp_path: Path) -> None:
    path = str(tmp_path / "empty.db")
    with SQLiteStorage(path):
        pass
    code, _, _ = run("--db", path, "reconcile", "ghost")
    assert code == ExitCode.NOT_FOUND


# --- documented contract (issue #322) ------------------------------------------------ #


#: The reference page whose reconcile paragraph documents the registry.
CLI_DOC = Path(__file__).resolve().parents[1] / "docs" / "api" / "cli.md"


def documented_registry() -> str:
    """The registry example a reader copies out of the module docstring."""
    lines = [line.strip() for line in (reconcilers.__doc__ or "").splitlines()]
    return next(line for line in lines if line.startswith("{"))


def test_the_documented_registry_example_registers_its_probe(tmp_path: Path) -> None:
    """The example has to be the shape the loader actually reads.

    It showed a bare ``{action_type: spec}`` mapping, which is valid JSON and
    clears every validation arm above because the ``probes`` key is simply
    absent: the registry loads empty and ``continuum reconcile`` then reports the
    action as having no probe instead of running the command that was registered
    for it. A silent no-op is the one outcome an operator cannot debug from the
    output, so the copyable example is pinned here.
    """
    p = tmp_path / "reconcilers.json"
    p.write_text(documented_registry(), encoding="utf-8")

    assert load_reconcilers(p) == {"send_invoice": {"command": "check-outbox", "timeout": 10.0}}


def test_the_documented_default_timeout_is_the_one_applied(tmp_path: Path) -> None:
    """Three places quote the default and only ``_DEFAULT_TIMEOUT`` decides it.

    A registry that omits ``timeout`` still gets one, so the number is part of
    what an operator sizes a probe against. The loader is asked for that registry
    here rather than only the prose: pinning both to the constant is what stops a
    change to it from leaving three stale mentions behind, and stops the prose
    from being right about a default the loader no longer applies.
    """
    default = f"{_DEFAULT_TIMEOUT:g} seconds"

    assert default in (reconcilers.__doc__ or "")
    assert default in (load_reconcilers.__doc__ or "")
    assert default in CLI_DOC.read_text(encoding="utf-8")

    p = tmp_path / "reconcilers.json"
    p.write_text('{"probes": {"send_invoice": {"command": "check-outbox"}}}', encoding="utf-8")

    assert load_reconcilers(p)["send_invoice"]["timeout"] == _DEFAULT_TIMEOUT
