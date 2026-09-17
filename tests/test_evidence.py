"""Evidence-backed reconciliation (issue #268).

The four acceptance criteria, against the real ledger and the real event log:

1. a crash between intercept and complete is settled by an otel_span probe,
   citing span ids;
2. an artifact_check probe proves occurred=false for a claimed-but-absent write;
3. contradictory evidence produces a review finding, not a settlement;
4. collector-dependent tests skip cleanly without the collector present.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.evidence import (
    ArtifactCheckReconciler,
    OtelSpanReconciler,
    ebpf_collector_available,
)
from continuum.models import ActionStatus, Origin, Run
from continuum.otel import record_span
from continuum.reconcilers import ReconcilerConfigError, load_reconcilers
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "ev.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


def claim(db: str, action_type: str, target: Path, *, complete: bool = False) -> str:
    """Claim an action against ``target``, leaving it uncertain unless complete."""
    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_1")
        outcome = ledger.claim(action_type, {"path": str(target)}, scoped_to_run=True)
        assert outcome.fresh is True
        if complete:
            ledger.complete(outcome.key, external_id="receipt")
    return outcome.action.action_id


def registry(tmp_path: Path, probes: dict[str, object]) -> str:
    """Write a probe registry and return its path."""
    path = tmp_path / "reconcilers.json"
    path.write_text(json.dumps({"probes": probes}), encoding="utf-8")
    return str(path)


def run(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue()


# --- AC1: otel_span settles a crash between intercept and complete --------- #


def test_otel_span_settles_and_cites_span_ids(db: str, tmp_path: Path) -> None:
    """A claimed-but-unclosed action is settled from a recorded span."""
    target = tmp_path / "out.txt"
    claim(db, "write_file", target)
    with SQLiteStorage(db) as store:
        # The span arrives after the claim, through the OTel bridge, with span
        # and trace ids the settlement must cite.
        record_span(
            SQLiteStorage(db),
            "execute_tool",
            {"gen_ai.tool.name": "write_file", "file_path": str(target)},
            span_id="span_a1",
            trace_id="trace_b2",
        )
        assert store.read_all_events("run_1")[-1].payload["span_id"] == "span_a1"

    cfg = registry(tmp_path, {"write_file": {"type": "otel_span", "identity": ["path"]}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg)
    assert code is ExitCode.OK, out

    with SQLiteStorage(db) as store:
        settled = [
            e for e in store.read_all_events("run_1") if e.type is EventType.ACTION_RECONCILED
        ]
    assert len(settled) == 1
    result = settled[0].payload["action"]["result"]
    assert result["span_id"] == "span_a1"
    assert result["trace_id"] == "trace_b2"
    note = settled[0].payload["action"]["last_error"]
    assert "span_a1" in note and "trace_b2" in note


def test_otel_span_ignores_a_span_before_the_claim(db: str, tmp_path: Path) -> None:
    """An older span is not evidence for a claim made after it."""
    target = tmp_path / "out.txt"
    with SQLiteStorage(db) as store:
        record_span(
            store,
            "execute_tool",
            {"gen_ai.tool.name": "write_file", "file_path": str(target)},
            span_id="span_old",
        )
    claim(db, "write_file", target)

    probe = OtelSpanReconciler(SQLiteStorage(db), "run_1")
    with SQLiteStorage(db) as store:
        action = ActionLedger(store, "run_1").pending()[0]
        assert probe.resolve(action) is None


def test_otel_span_ignores_a_span_for_a_different_path(db: str, tmp_path: Path) -> None:
    """A sibling write with the same tool but a different target is not a match."""
    target = tmp_path / "out.txt"
    claim(db, "write_file", target)
    with SQLiteStorage(db) as store:
        record_span(
            store,
            "execute_tool",
            {"gen_ai.tool.name": "write_file", "file_path": str(tmp_path / "other.txt")},
            span_id="span_other",
        )

    probe = OtelSpanReconciler(SQLiteStorage(db), "run_1")
    with SQLiteStorage(db) as store:
        action = ActionLedger(store, "run_1").pending()[0]
        assert probe.resolve(action) is None


def test_otel_span_ignores_every_span_when_the_action_has_no_identity_token(
    db: str, tmp_path: Path
) -> None:
    """An action with no path must not match the tool's next unrelated write.

    The matcher only rejects an explicit disagreement between tokens present on
    both sides. An action carrying none of them used to fall through to a
    blanket match, settling from a span that wrote an unrelated file.
    """
    # Claimed with no path argument, so no token can constrain a match.
    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("write_file", {}, scoped_to_run=True)
    with SQLiteStorage(db) as store:
        record_span(
            store,
            "execute_tool",
            {"gen_ai.tool.name": "write_file", "file_path": str(tmp_path / "unrelated.txt")},
            span_id="span_any",
        )

    probe = OtelSpanReconciler(SQLiteStorage(db), "run_1")
    with SQLiteStorage(db) as store:
        action = ActionLedger(store, "run_1").pending()[0]
        assert probe.resolve(action) is None


def test_failed_span_does_not_settle_as_not_occurred(db: str, tmp_path: Path) -> None:
    """A failed span is not evidence of absence, so the action stays uncertain."""
    target = tmp_path / "out.txt"
    claim(db, "write_file", target)
    with SQLiteStorage(db) as store:
        record_span(
            store,
            "execute_tool",
            {"gen_ai.tool.name": "write_file", "file_path": str(target)},
            ok=False,
            span_id="span_fail",
        )

    probe = OtelSpanReconciler(SQLiteStorage(db), "run_1")
    with SQLiteStorage(db) as store:
        action = ActionLedger(store, "run_1").pending()[0]
        assert probe.resolve(action) is None


# --- AC2: artifact_check proves absence ----------------------------------- #


def test_artifact_check_proves_absent(db: str, tmp_path: Path) -> None:
    """A claimed write whose file never lands settles as not-occurred."""
    target = tmp_path / "never.txt"
    claim(db, "write_report", target)

    cfg = registry(tmp_path, {"write_report": {"type": "artifact_check", "path_key": "path"}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg)
    assert code is ExitCode.OK, out

    with SQLiteStorage(db) as store:
        actions = list(ActionLedger(store, "run_1").folded().values())
    assert actions[0].status is ActionStatus.FAILED
    assert "absent" in (actions[0].last_error or "")


def test_artifact_check_proves_present_with_digest(db: str, tmp_path: Path) -> None:
    """A landed write settles as occurred, the digest as its receipt."""
    target = tmp_path / "out.txt"
    target.write_text("payload", encoding="utf-8")
    claim(db, "write_report", target)

    probe = ArtifactCheckReconciler(path_key="path")
    with SQLiteStorage(db) as store:
        action = ActionLedger(store, "run_1").pending()[0]
        resolution = probe.resolve(action)
    assert resolution is not None and resolution.occurred is True
    assert resolution.external_id == _sha256("payload")
    assert resolution.result and resolution.result["bytes"] == len("payload")


def test_artifact_check_expected_digest_mismatch_declines(db: str, tmp_path: Path) -> None:
    """A file that exists but is not this action's write is not attributed to it."""
    target = tmp_path / "out.txt"
    target.write_text("somebody else", encoding="utf-8")
    claim(db, "write_report", target)

    probe = ArtifactCheckReconciler(path_key="path", expect_sha256=_sha256("payload"))
    with SQLiteStorage(db) as store:
        action = ActionLedger(store, "run_1").pending()[0]
        assert probe.resolve(action) is None


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- AC3: contradictions become review findings --------------------------- #


def test_completed_but_absent_becomes_review(db: str, tmp_path: Path) -> None:
    """A completion with no artifact escalates instead of being re-settled."""
    target = tmp_path / "out.txt"
    target.write_text("payload", encoding="utf-8")
    claim(db, "write_report", target, complete=True)
    target.unlink()  # the artifact disappeared after the completion was recorded

    cfg = registry(tmp_path, {"write_report": {"type": "artifact_check", "path_key": "path"}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg)
    assert code is ExitCode.REQUIRES_HUMAN, out
    assert "completed but" in out

    with SQLiteStorage(db) as store:
        actions = list(ActionLedger(store, "run_1").folded().values())
    assert actions[0].status is ActionStatus.REQUIRES_REVIEW


def test_present_but_unrecorded_becomes_review(db: str, tmp_path: Path) -> None:
    """A recorded failure with the artifact present is a finding, not a settlement."""
    target = tmp_path / "out.txt"
    target.write_text("payload", encoding="utf-8")
    # The run recorded the write as failed, yet the file is there: the ledger
    # and the world disagree, and neither side should be picked silently.
    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "run_1")
        outcome = ledger.claim("write_report", {"path": str(target)}, scoped_to_run=True)
        ledger.fail(outcome.key, "write did not land")

    cfg = registry(tmp_path, {"write_report": {"type": "artifact_check", "path_key": "path"}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg)
    assert code is ExitCode.REQUIRES_HUMAN, out
    assert "never recorded" in out

    with SQLiteStorage(db) as store:
        actions = list(ActionLedger(store, "run_1").folded().values())
    assert actions[0].status is ActionStatus.REQUIRES_REVIEW


def test_contradiction_dry_run_reports_without_writing(db: str, tmp_path: Path) -> None:
    """A dry run names the contradiction but changes no action status."""
    target = tmp_path / "out.txt"
    target.write_text("payload", encoding="utf-8")
    claim(db, "write_report", target, complete=True)
    target.unlink()

    cfg = registry(tmp_path, {"write_report": {"type": "artifact_check", "path_key": "path"}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg, "--dry-run")
    assert "completed but" in out
    with SQLiteStorage(db) as store:
        actions = list(ActionLedger(store, "run_1").folded().values())
    assert actions[0].status is ActionStatus.COMPLETED


# --- strict mode -------------------------------------------------------- #


def test_strict_escalates_an_unsettled_action(db: str, tmp_path: Path) -> None:
    """In strict mode an unsettled action goes to review rather than pending."""
    claim(db, "write_file", tmp_path / "out.txt")
    # No span recorded, so the otel_span probe cannot decide.
    cfg = registry(tmp_path, {"write_file": {"type": "otel_span", "identity": ["path"]}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg, "--strict")
    assert code is ExitCode.REQUIRES_HUMAN, out

    with SQLiteStorage(db) as store:
        actions = list(ActionLedger(store, "run_1").folded().values())
    assert actions[0].status is ActionStatus.REQUIRES_REVIEW


def test_non_strict_leaves_an_unsettled_action_pending(db: str, tmp_path: Path) -> None:
    """Without --strict the same action stays pending for a later run."""
    claim(db, "write_file", tmp_path / "out.txt")
    cfg = registry(tmp_path, {"write_file": {"type": "otel_span", "identity": ["path"]}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg)
    assert code is ExitCode.REQUIRES_HUMAN, out

    with SQLiteStorage(db) as store:
        actions = list(ActionLedger(store, "run_1").folded().values())
    assert actions[0].status is ActionStatus.STARTED


# --- registry validation ------------------------------------------------- #


@pytest.mark.parametrize(
    ("probes", "needle"),
    [
        ({"x": {"type": "artifact_check"}}, "needs 'path' or 'path_key'"),
        ({"x": {"type": "nope"}}, "unknown type"),
        ({"x": {"type": "otel_span", "command": "c"}}, "does not accept"),
        ({"x": {}}, "needs a string 'command' or a 'type'"),
    ],
)
def test_bad_builtin_specs_are_refused(
    tmp_path: Path, probes: dict[str, object], needle: str
) -> None:
    path = tmp_path / "reconcilers.json"
    path.write_text(json.dumps({"probes": probes}), encoding="utf-8")
    with pytest.raises(ReconcilerConfigError) as exc:
        load_reconcilers(path)
    assert needle in str(exc.value)


def test_command_spec_without_type_still_loads(tmp_path: Path) -> None:
    """An existing registry has no type key and must keep working."""
    path = tmp_path / "reconcilers.json"
    path.write_text(
        json.dumps({"probes": {"send_invoice": {"command": "check-outbox", "timeout": 5}}}),
        encoding="utf-8",
    )
    loaded = load_reconcilers(path)
    assert loaded == {"send_invoice": {"command": "check-outbox", "timeout": 5.0}}


def test_settlement_provenance_is_deterministic(db: str, tmp_path: Path) -> None:
    """A settlement from evidence is DETERMINISTIC, like a command probe's."""
    target = tmp_path / "out.txt"
    target.write_text("payload", encoding="utf-8")
    claim(db, "write_report", target)

    cfg = registry(tmp_path, {"write_report": {"type": "artifact_check", "path_key": "path"}})
    code, out = run("--db", db, "reconcile", "run_1", "--config", cfg)
    assert code is ExitCode.OK, out

    with SQLiteStorage(db) as store:
        settled = [
            e for e in store.read_all_events("run_1") if e.type is EventType.ACTION_RECONCILED
        ]
    assert settled[0].source is Origin.DETERMINISTIC


# --- AC4: collector-dependent tests skip cleanly ------------------------- #


def test_ebpf_collector_available_is_bool() -> None:
    """The gate is a plain boolean, not a presence that could be faked."""
    assert isinstance(ebpf_collector_available(), bool)


@pytest.mark.skipif(
    not ebpf_collector_available(),
    reason="no eBPF collector on PATH; Tetragon and AgentSight are Linux-only (issue #268 AC4)",
)
def test_ebpf_collector_round_trip(tmp_path: Path) -> None:
    """Against a live collector, the adapter answers from its observations.

    Skipped everywhere CI runs it: the adapter's pure matching logic has its own
    tests below, so a machine without a collector covers the skip path and
    nothing is faked.
    """
    from examples.ebpf_reconciler_adapter import evaluate

    events = tmp_path / "agentsight.json"
    events.write_text(json.dumps({"tool": "write_file", "path": "/data/out.txt"}), encoding="utf-8")
    assert (
        evaluate({"action_type": "write_file", "arguments": {"path": "/data/out.txt"}}, events)
        == "occurred=true"
    )


def test_adapter_matches_agentsight_event(tmp_path: Path) -> None:
    """The adapter's own matching logic is exercised without a collector."""
    from examples.ebpf_reconciler_adapter import evaluate

    events = tmp_path / "agentsight.json"
    events.write_text(json.dumps({"tool": "write_file", "path": "/data/out.txt"}), encoding="utf-8")
    action = {"action_type": "write_file", "arguments": {"path": "/data/out.txt"}}
    assert evaluate(action, events) == "occurred=true"

    other = {"action_type": "write_file", "arguments": {"path": "/data/other.txt"}}
    assert evaluate(other, events) == "occurred=unknown"


def test_adapter_treats_missing_events_as_unknown(tmp_path: Path) -> None:
    """No collector output is unknown, not absent: silence is not evidence."""
    from examples.ebpf_reconciler_adapter import evaluate

    action = {"action_type": "write_file", "arguments": {"path": "/data/out.txt"}}
    assert evaluate(action, tmp_path / "absent.json") == "occurred=unknown"


def test_adapter_matches_tetragon_write(tmp_path: Path) -> None:
    """A Tetragon write syscall on the action's path settles as occurred."""
    from examples.ebpf_reconciler_adapter import evaluate

    events = tmp_path / "tetragon.json"
    events.write_text(
        json.dumps(
            {
                "process_kprobe": {
                    "syscall": {"syscall": "write", "args": [{"string_arg": "/data/out.txt"}]}
                }
            }
        ),
        encoding="utf-8",
    )
    action = {"action_type": "write_file", "arguments": {"path": "/data/out.txt"}}
    assert evaluate(action, events) == "occurred=true"


def test_adapter_reads_the_action_from_stdin(tmp_path: Path) -> None:
    """The probe envelope: action JSON on stdin, one verdict line on stdout."""
    import subprocess
    import sys

    events = tmp_path / "agentsight.json"
    events.write_text(json.dumps({"tool": "write_file", "path": "/data/out.txt"}), encoding="utf-8")
    adapter = Path(__file__).resolve().parents[1] / "examples" / "ebpf_reconciler_adapter.py"
    completed = subprocess.run(
        [sys.executable, str(adapter), "--events", str(events)],
        input=json.dumps({"action_type": "write_file", "arguments": {"path": "/data/out.txt"}}),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "occurred=true"
