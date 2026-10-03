"""Post-checkpoint observations surfaced in the recovery contract (#208).

The hooks record what landed on disk (#210); the contract shows it to whoever
resumes, disk-checked at assess time and honestly labelled when drift or
deletion happened since. A row that no longer matches disk is a verdict
signal, not decoration: it escalates the decision and names a repair step.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.models import Origin, RecoveryMode, Run
from continuum.recovery import RecoveryEngine, render_contract
from continuum.recovery.contract import verify_contract
from continuum.recovery.observations import collect_observations
from continuum.recovery.planner import RepairKind
from continuum.storage import SQLiteStorage


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def db(workspace: Path) -> str:
    return str(workspace / "obs.db")


def make_run(db: str, *, checkpointed: bool = True) -> None:
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="Write things"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "Write things"})
        if checkpointed:
            CheckpointManager(store).checkpoint("run_1")


def observe(db: str, path: Path, content: str = "body") -> str:
    """Record one observation exactly like `continuum observe` would."""
    data = content.encode()
    digest = hashlib.sha256(data).hexdigest()
    with SQLiteStorage(db) as store:
        store.append_event(
            "run_1",
            EventType.TOOL_COMPLETED,
            {"tool": "Write", "path": str(path), "bytes": len(data), "sha256": digest},
            source=Origin.EXTERNAL_AGENT,
        )
    return digest


def assess(db: str, root: Path):
    return RecoveryEngine(SQLiteStorage(db)).assess("run_1", _root=root)


# The engine resolves relative paths against its cwd; tests pass an explicit
# root through assess by monkeypatching cwd instead of adding API surface.


@pytest.fixture
def assess_in_root(monkeypatch: pytest.MonkeyPatch, workspace: Path):
    def _assess(db: str):
        monkeypatch.chdir(workspace)
        return RecoveryEngine(SQLiteStorage(db)).assess("run_1")

    return _assess


# --- projection --------------------------------------------------------------- #


def test_post_checkpoint_observation_appears_and_verifies(
    db: str, workspace: Path, assess_in_root
) -> None:
    make_run(db)
    artifact = workspace / "a.txt"
    artifact.write_text("body")
    observe(db, artifact)

    decision = assess_in_root(db)
    (entry,) = decision.contract.post_checkpoint_observations
    assert entry["status"] == "verified"
    assert entry["path"] == str(artifact)


def test_a_changed_file_is_reported_as_changed(db: str, workspace: Path, assess_in_root) -> None:
    make_run(db)
    artifact = workspace / "b.txt"
    observe(db, artifact, content="old body")  # observed but never written

    decision = assess_in_root(db)
    assert decision.contract.post_checkpoint_observations[0]["status"] == "missing"


def test_observation_before_the_last_checkpoint_is_excluded(
    db: str, workspace: Path, assess_in_root
) -> None:
    make_run(db, checkpointed=False)
    artifact = workspace / "c.txt"
    artifact.write_text("early")
    observe(db, artifact)
    CheckpointManager(SQLiteStorage(db)).checkpoint("run_1")

    decision = assess_in_root(db)
    assert decision.contract.post_checkpoint_observations == []


def test_with_no_checkpoint_everything_is_included(
    db: str, workspace: Path, assess_in_root
) -> None:
    make_run(db, checkpointed=False)
    artifact = workspace / "d.txt"
    artifact.write_text("x")
    observe(db, artifact)

    decision = assess_in_root(db)
    assert len(decision.contract.post_checkpoint_observations) == 1


def test_the_cap_truncates_with_an_explicit_marker(
    db: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from continuum.recovery import observations as obs_module

    make_run(db)
    monkeypatch.setattr(obs_module, "MAX_CONTRACT_OBSERVATIONS", 3)
    for i in range(6):
        f = workspace / f"f{i}.txt"
        observe(db, f, content=f"x{i}")
        f.write_text(f"x{i}")  # disk matches the observation

    monkeypatch.chdir(workspace)
    entries = collect_observations(SQLiteStorage(db), "run_1", after_sequence=0)
    # Three real rows plus the explicit truncation marker.
    assert len(entries) == 4
    assert [e["status"] for e in entries[:3]] == ["verified"] * 3
    assert entries[-1]["truncated"] is True
    assert entries[-1]["omitted"] == 3


def test_exactly_the_cap_emits_no_truncation_marker(
    db: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At exactly MAX_CONTRACT_OBSERVATIONS nothing was dropped, so the result
    is the cap-many real rows and no marker -- not cap+1 rows with a spurious
    ``omitted: 0`` (issue #1363)."""
    from continuum.recovery import observations as obs_module

    make_run(db)
    monkeypatch.setattr(obs_module, "MAX_CONTRACT_OBSERVATIONS", 3)
    for i in range(3):
        f = workspace / f"c{i}.txt"
        observe(db, f, content=f"x{i}")
        f.write_text(f"x{i}")

    monkeypatch.chdir(workspace)
    entries = collect_observations(SQLiteStorage(db), "run_1", after_sequence=0)
    assert len(entries) == 3
    assert all("truncated" not in e for e in entries)


def test_one_past_the_cap_omits_exactly_one(
    db: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first observation over the cap makes the marker report ``omitted: 1``."""
    from continuum.recovery import observations as obs_module

    make_run(db)
    monkeypatch.setattr(obs_module, "MAX_CONTRACT_OBSERVATIONS", 3)
    for i in range(4):
        f = workspace / f"o{i}.txt"
        observe(db, f, content=f"x{i}")
        f.write_text(f"x{i}")

    monkeypatch.chdir(workspace)
    entries = collect_observations(SQLiteStorage(db), "run_1", after_sequence=0)
    assert len(entries) == 4
    assert entries[-1] == {"truncated": True, "omitted": 1}


# --- contract integration ------------------------------------------------------- #


def test_render_shows_the_section(db: str, workspace: Path, assess_in_root) -> None:
    make_run(db)
    artifact = workspace / "e.txt"
    artifact.write_text("body")
    observe(db, artifact)

    text = render_contract(assess_in_root(db).contract)
    assert "files changed since last checkpoint:" in text
    assert "[verified]" in text


def test_verified_observations_do_not_change_the_decision(
    db: str, workspace: Path, tmp_path: Path
) -> None:
    """Two identical runs, one carrying an observation whose file still matches
    disk: the decision fields must agree, because a file that still holds what
    the hook recorded needs no repair (issue #208)."""
    clean = str(tmp_path / "clean.db")
    make_run(clean)
    with SQLiteStorage(clean) as store:
        store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 1, "total": 2})

    observed = db
    make_run(observed)
    with SQLiteStorage(observed) as store:
        store.append_event("run_1", EventType.TASK_UPDATED, {"completed": 1, "total": 2})
    f = workspace / "g.txt"
    f.write_text("x")
    observe(observed, f, content="x")

    import os

    cwd = os.getcwd()
    try:
        os.chdir(workspace)
        d_clean = RecoveryEngine(SQLiteStorage(clean)).assess("run_1")
        d_obs = RecoveryEngine(SQLiteStorage(observed)).assess("run_1")
    finally:
        os.chdir(cwd)

    assert d_clean.mode is d_obs.mode
    assert d_clean.safe == d_obs.safe
    assert [s.action_name for s in d_clean.plan.steps] == [s.action_name for s in d_obs.plan.steps]
    # ...but the evidence itself is there.
    assert d_obs.contract.post_checkpoint_observations
    assert not d_clean.contract.post_checkpoint_observations


def test_a_drifted_observation_escalates_the_decision(
    db: str, workspace: Path, tmp_path: Path
) -> None:
    """Two identical runs, one whose observed file moved on disk after the hook
    recorded it: the verdict must rise to REPAIR_AND_RESUME and the plan must
    name the file, because a change the event log does not explain underlies
    any further work on it (issue #208)."""
    clean = str(tmp_path / "clean.db")
    make_run(clean)

    drifted = db
    make_run(drifted)
    f = workspace / "g.txt"
    f.write_text("tampered")
    observe(drifted, f, content="recorded")

    import os

    cwd = os.getcwd()
    try:
        os.chdir(workspace)
        d_clean = RecoveryEngine(SQLiteStorage(clean)).assess("run_1")
        d_obs = RecoveryEngine(SQLiteStorage(drifted)).assess("run_1")
    finally:
        os.chdir(cwd)

    assert d_clean.mode is RecoveryMode.RESUME
    assert d_obs.mode is RecoveryMode.REPAIR_AND_RESUME
    (step,) = d_obs.plan.of_kind(RepairKind.RECONCILE_FILE)
    assert step.target == str(f)
    assert step.blocking
    assert not step.requires_human
    assert any("drifted since the checkpoint" in line for line in d_obs.rationale)


def test_a_deleted_observation_escalates_the_decision(
    db: str, workspace: Path, tmp_path: Path
) -> None:
    """An observed file removed from disk escalates just as drift does: the
    digest can no longer be checked at all, which is the strongest form of
    mismatch (issue #208)."""
    make_run(db)
    f = workspace / "gone.txt"
    f.write_text("recorded")
    observe(db, f, content="recorded")
    f.unlink()

    import os

    cwd = os.getcwd()
    try:
        os.chdir(workspace)
        decision = RecoveryEngine(SQLiteStorage(db)).assess("run_1")
    finally:
        os.chdir(cwd)

    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    (step,) = decision.plan.of_kind(RepairKind.RECONCILE_FILE)
    assert step.target == str(f)
    assert "missing" in step.reason


def test_the_drift_rationale_counts_changed_and_missing_separately(
    db: str, workspace: Path, tmp_path: Path
) -> None:
    """One file tampered with and one deleted: the rationale names both counts
    so a reader knows whether files moved or vanished, and labels a run with
    only one kind without a zero for the other (issue #208)."""
    make_run(db)
    moved = workspace / "moved.txt"
    moved.write_text("recorded")
    observe(db, moved, content="recorded")
    gone = workspace / "gone.txt"
    gone.write_text("recorded")
    observe(db, gone, content="recorded")
    moved.write_text("tampered")
    gone.unlink()

    import os

    cwd = os.getcwd()
    try:
        os.chdir(workspace)
        decision = RecoveryEngine(SQLiteStorage(db)).assess("run_1")
    finally:
        os.chdir(cwd)

    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert "2 observed file(s) drifted since the checkpoint (1 changed, 1 missing)" in " ".join(
        decision.rationale
    )

    # A deletion-only run reports no spurious "0 changed".
    only_gone = str(tmp_path / "only_gone.db")
    make_run(only_gone)
    g2 = workspace / "gone2.txt"
    g2.write_text("recorded")
    observe(only_gone, g2, content="recorded")
    g2.unlink()
    try:
        os.chdir(workspace)
        only = RecoveryEngine(SQLiteStorage(only_gone)).assess("run_1")
    finally:
        os.chdir(cwd)
    assert "1 observed file(s) drifted since the checkpoint (1 missing)" in " ".join(only.rationale)


def test_drift_does_not_outrank_a_human_gate(db: str, workspace: Path, tmp_path: Path) -> None:
    """Drift proposes REPAIR_AND_RESUME; an uncertain side effect under
    ``strict_unknown`` proposes REQUEST_HUMAN. The verdict takes the maximum on
    the severity order, so the human gate must survive -- drift adds a repair,
    it does not soften an escalation."""
    from continuum.actions.ledger import ActionLedger

    make_run(db)
    f = workspace / "d.txt"
    f.write_text("recorded")
    observe(db, f, content="recorded")
    f.write_text("tampered")

    import os

    with SQLiteStorage(db) as store:
        # An action that never settled: under strict_unknown its reconciliation
        # needs a person, which proposes REQUEST_HUMAN.
        ActionLedger(store, "run_1").claim("github.create_issue", {})

    cwd = os.getcwd()
    try:
        os.chdir(workspace)
        decision = RecoveryEngine(SQLiteStorage(db), strict_unknown=True).assess("run_1")
    finally:
        os.chdir(cwd)

    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    # The drift repair still lands, alongside the human review the action asked for.
    assert decision.plan.of_kind(RepairKind.RECONCILE_FILE)
    assert decision.plan.requires_human


def test_sealed_contract_still_verifies_with_observations_present(
    db: str, workspace: Path, assess_in_root
) -> None:
    make_run(db)
    artifact = workspace / "h.txt"
    artifact.write_text("body")
    observe(db, artifact)

    contract = assess_in_root(db).contract
    assert verify_contract(contract)


def test_resume_payload_carries_the_rows_through_json(
    db: str, workspace: Path, assess_in_root
) -> None:
    make_run(db)
    artifact = workspace / "i.txt"
    artifact.write_text("body")
    observe(db, artifact)

    import io
    import os

    from continuum.cli import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    try:
        os.chdir(workspace)
        code = cli_main(["--db", db, "--json", "resume", "run_1"], out=out, err=err)
    finally:
        os.chdir(cwd)
    assert code in (ExitCodes_OK_UNSAFE), err
    payload = json.loads(out.getvalue())
    assert payload["contract"]["post_checkpoint_observations"][0]["status"] == "verified"


from continuum.cli.exitcodes import ExitCode  # noqa: E402

ExitCodes_OK_UNSAFE = {
    ExitCode.OK,
    ExitCode.REQUIRES_HUMAN,
    ExitCode.REQUIRES_REPAIR,
    ExitCode.UNSAFE,
}
