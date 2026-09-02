"""Registered reconciliation probes (issue #218).

A probe is an operator-configured command that checks the external system
for one action type and prints a verdict. These tests pin the parsing
contract, the settle loop against the real ledger, the provenance of
auto-settled events, and the CLI's exit-code behaviour.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum import reconcilers
from continuum.actions import ActionLedger
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import ActionStatus, Origin, Run
from continuum.reconcilers import (
    _DEFAULT_TIMEOUT,
    ReconcilerConfigError,
    _parse_verdict,
    load_reconcilers,
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
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"probes": {"send_invoice": {"timeout": 5}}}))
    with pytest.raises(ReconcilerConfigError, match="command"):
        load_reconcilers(p)
    p.write_text(json.dumps({"probes": {"send_invoice": {"command": "x", "timeout": -1}}}))
    with pytest.raises(ReconcilerConfigError, match="timeout"):
        load_reconcilers(p)


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


def test_the_documented_default_timeout_is_the_one_applied() -> None:
    """Three places quote the default and only ``_DEFAULT_TIMEOUT`` decides it.

    A registry that omits ``timeout`` still gets one, so the number is part of
    what an operator sizes a probe against. Pinning the prose to the constant is
    what stops a change to it from leaving three stale mentions behind.
    """
    default = f"{_DEFAULT_TIMEOUT:g} seconds"

    assert default in (reconcilers.__doc__ or "")
    assert default in (load_reconcilers.__doc__ or "")
    assert default in CLI_DOC.read_text(encoding="utf-8")
