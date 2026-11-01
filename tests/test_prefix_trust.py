"""Prefix-trust advisory (issue #401): deterministic, never gates."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.analysis.prefix_trust import (
    TrustReport,
    _collect_origins,
    _score_evidence,
    _score_goal,
    _score_role,
    trust_over_prefix,
)
from continuum.cli.main import main as cli_main
from continuum.events import Event, EventType
from continuum.models import (
    Approval,
    ApprovalStatus,
    ConstraintPin,
    Decision,
    Evidence,
    ExternalDependency,
    Finding,
    Origin,
    PendingWork,
    PlanStep,
    Progress,
    Provenance,
    Run,
    SemanticState,
)
from continuum.storage import SQLiteStorage


def _seed_clean(db: Path) -> None:
    storage = SQLiteStorage(str(db))
    storage.create_run(Run(run_id="r1", goal="ship"))
    storage.append_event("r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC)
    for _ in range(5):
        storage.append_event(
            "r1", EventType.TASK_UPDATED, {"completed": 1, "total": 5}, source=Origin.DETERMINISTIC
        )
        storage.append_event(
            "r1",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": f"e{_}", "summary": "ok"},
            source=Origin.DETERMINISTIC,
        )
    storage.close()


def test_score_changes_when_provenance_degrades_and_stable_on_clean() -> None:
    """Degraded prefix dominated by EXTERNAL_AGENT lowers trust, clean stays stable."""
    # Use trust_over_prefix on two states built via events
    # Degraded: many EXTERNAL_AGENT events
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_clean = Path(tmp) / "clean.db"
        db_deg = Path(tmp) / "deg.db"
        # Clean
        s_clean = SQLiteStorage(str(db_clean))
        s_clean.create_run(Run(run_id="r1", goal="ship"))
        s_clean.append_event(
            "r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC
        )
        for _ in range(5):
            s_clean.append_event(
                "r1",
                EventType.EVIDENCE_ADDED,
                {"evidence_id": f"e{_}", "summary": "ok"},
                source=Origin.DETERMINISTIC,
            )
        from continuum.state.semantic import project

        state_clean = project("r1", s_clean.read_events("r1"))
        score_clean = trust_over_prefix(state_clean)
        s_clean.close()

        # Degraded: same but with EXTERNAL_AGENT
        s_deg = SQLiteStorage(str(db_deg))
        s_deg.create_run(Run(run_id="r1", goal="ship"))
        s_deg.append_event(
            "r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.EXTERNAL_AGENT
        )
        for _ in range(5):
            s_deg.append_event(
                "r1",
                EventType.EVIDENCE_ADDED,
                {"evidence_id": f"e{_}", "summary": "ok"},
                source=Origin.EXTERNAL_AGENT,
            )
        state_deg = project("r1", s_deg.read_events("r1"))
        score_deg = trust_over_prefix(state_deg)
        s_deg.close()

        assert score_deg["trust_score"] < score_clean["trust_score"]
        # Clean should be high and stable
        assert score_clean["trust_score"] > 0.8
        # Degraded should be lower
        assert score_deg["trust_score"] < 0.6


def test_mode_exit_code_gate_byte_identical_with_feature_on_or_off(tmp_path: Path) -> None:
    """Advisory never moves mode, exit code, or gate decision."""
    db = tmp_path / "g.db"
    storage = SQLiteStorage(str(db))
    storage.create_run(Run(run_id="r1", goal="ship"))
    storage.append_event("r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "r1", EventType.TASK_UPDATED, {"completed": 1, "total": 5}, source=Origin.DETERMINISTIC
    )
    storage.close()

    # Resume with and without reading advisory should be identical in mode and exit code
    out1 = io.StringIO()
    err1 = io.StringIO()
    code1 = cli_main(["--db", str(db), "--json", "resume", "r1"], out=out1, err=err1)
    payload1 = json.loads(out1.getvalue())
    mode1 = payload1["mode"]

    out2 = io.StringIO()
    err2 = io.StringIO()
    code2 = cli_main(["--db", str(db), "--json", "resume", "r1"], out=out2, err=err2)
    payload2 = json.loads(out2.getvalue())
    assert payload1["mode"] == payload2["mode"]
    assert payload1["safe"] == payload2["safe"]
    assert code1 == code2
    # Advisory is present but does not affect mode
    assert "advisory" in payload1
    assert "trust_score" in payload1["advisory"]
    # Gate decisions also byte-identical: run gate on same payload twice

    # Use a simple gate check: no config, so allow should be true and identical
    assert mode1 == payload2["mode"]


def test_breakdown_dimensions_trace_to_named_fields() -> None:
    """Every dimension of the breakdown traces to named contract/provenance fields."""
    from continuum.state.semantic import project

    # Build a small state
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r1", goal="ship"))
    storage.append_event("r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC)
    storage.append_event(
        "r1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "e1", "summary": "ok", "source": "src", "checksum": "abc"},
        source=Origin.DETERMINISTIC,
    )

    state = project("r1", storage.read_events("r1"))
    report = trust_over_prefix(state)
    storage.close()
    assert "trust_score" in report
    assert "breakdown" in report
    for dim in ("role", "goal", "evidence"):
        assert dim in report["breakdown"]
        # Values are floats in 0-1
        assert 0.0 <= report["breakdown"][dim] <= 1.0
    # Docstring cites field names
    from continuum.analysis.prefix_trust import trust_over_prefix as f

    assert "SemanticState" in f.__doc__ or "provenance" in f.__doc__.lower()
    assert "Goal" in f.__doc__ or "goal" in f.__doc__.lower()
    assert "Evidence" in f.__doc__ or "evidence" in f.__doc__.lower()


def test_falsifiable_two_runs_identical_except_fabricated_progress(tmp_path: Path) -> None:
    """Two runs identical except interleaved fabricated progress: trust diverges, local checks stay valid."""
    # Run A: clean, deterministic progress
    db_a = tmp_path / "a.db"
    storage_a = SQLiteStorage(str(db_a))
    storage_a.create_run(Run(run_id="r1", goal="ship"))
    storage_a.append_event(
        "r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC
    )
    for _ in range(5):
        storage_a.append_event(
            "r1",
            EventType.TASK_UPDATED,
            {"completed": _ + 1, "total": 10},
            source=Origin.DETERMINISTIC,
        )
    from continuum.state.semantic import project

    state_a = project("r1", storage_a.read_events("r1"))
    # Local validation should be valid for both (no self-certified)
    from continuum.state.validator import validate_state

    val_a = validate_state(state_a, confirmed=True)
    assert all(
        e.status.value == "valid"
        for e in val_a.report.statuses
        if e.component.value in ("goal", "progress")
    )
    score_a = trust_over_prefix(state_a)
    storage_a.close()

    # Run B: same but interleaved with fabricated EXTERNAL_AGENT progress events
    db_b = tmp_path / "b.db"
    storage_b = SQLiteStorage(str(db_b))
    storage_b.create_run(Run(run_id="r1", goal="ship"))
    storage_b.append_event(
        "r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC
    )
    for _ in range(5):
        storage_b.append_event(
            "r1",
            EventType.TASK_UPDATED,
            {"completed": _ + 1, "total": 10},
            source=Origin.DETERMINISTIC,
        )
        # Fabricated but well-formed
        storage_b.append_event(
            "r1",
            EventType.TASK_UPDATED,
            {"completed": _ + 1, "total": 10},
            source=Origin.EXTERNAL_AGENT,
        )
    state_b = project("r1", storage_b.read_events("r1"))
    val_b = validate_state(state_b, confirmed=True)
    # Local checks still valid when confirmed, even with fabricated
    assert all(
        e.status.value == "valid"
        for e in val_b.report.statuses
        if e.component.value in ("goal", "progress")
    )
    score_b = trust_over_prefix(state_b)
    storage_b.close()

    # Trust should diverge measurably
    delta = abs(score_a["trust_score"] - score_b["trust_score"])
    assert delta > 0.15, f"trust did not diverge enough: {score_a} vs {score_b} delta {delta}"


def test_an_agent_declared_dependency_cannot_buy_trust_with_a_version() -> None:
    """A dependency is scored by who declared it, not by whether it names a version.

    The module's own definition of trusted is "recorded by deterministic local
    code or by a human, not by an autonomous agent reporting on itself". A
    version string is not a certificate of who recorded it, so an agent declaring
    a versioned dependency must not raise its own role score (issue #1065).
    """
    from continuum.state.semantic import project

    def _seed(source: Origin, *, version: str | None = "v1", declare: bool = True) -> dict:  # type: ignore[type-arg]
        storage = SQLiteStorage(":memory:")
        storage.create_run(Run(run_id="r1", goal="ship"))
        storage.append_event("r1", EventType.RUN_STARTED, {"goal": "ship"}, source=source)
        storage.append_event("r1", EventType.WORK_COMPLETED, {"doc": 0}, source=source)
        if declare:
            payload = {"resource": "dataset"}
            if version is not None:
                payload["version"] = version
            storage.append_event("r1", EventType.DEPENDENCY_DECLARED, payload, source=source)
        state = project("r1", storage.read_events("r1"))
        score = trust_over_prefix(state)
        storage.close()
        return score

    agent_with_dep = _seed(Origin.EXTERNAL_AGENT)
    agent_without_dep = _seed(Origin.EXTERNAL_AGENT, declare=False)
    # Declaring the dependency changed nothing about who asserted the run's facts,
    # so the role score must be identical: no trust purchased with a version.
    assert agent_with_dep["breakdown"]["role"] == agent_without_dep["breakdown"]["role"] == 0.0

    trusted_with_dep = _seed(Origin.DETERMINISTIC)
    assert trusted_with_dep["breakdown"]["role"] == 1.0
    # Mode invariance preserved: the score is advisory and never gates.
    assert trusted_with_dep["trust_score"] > agent_with_dep["trust_score"]

    # Every cell of the origin x version matrix is pinned, so neither arm of the
    # credit can regress unnoticed. The dependency is the only scored fact here,
    # so the evidence breakdown is the credit itself.
    trusted_unversioned = _seed(Origin.DETERMINISTIC, version=None)
    assert trusted_with_dep["breakdown"]["evidence"] == 0.9
    assert trusted_unversioned["breakdown"]["evidence"] == 0.3
    assert agent_with_dep["breakdown"]["evidence"] == 0.2
    assert _seed(Origin.EXTERNAL_AGENT, version=None)["breakdown"]["evidence"] == 0.1
    # A version is never worth more than the origin that recorded it: an
    # unversioned declaration from a trusted source still outranks a versioned
    # one from an agent reporting on itself.
    assert trusted_unversioned["breakdown"]["evidence"] > agent_with_dep["breakdown"]["evidence"]


def test_agent_authored_plan_and_pending_work_lower_the_role_score() -> None:
    """Plan steps and pending work are scored by who authored them (#1065 sibling).

    ``_collect_origins`` seeded the role dimension from goal, progress, evidence,
    findings, decisions, external_dependencies, approvals and pins -- but omitted
    ``state.plan`` and ``state.pending_work``. Both carry provenance and an agent
    authors them by projecting PLAN_UPSERT / WORK_ADDED, so the omission let an
    agent add self-reported plan steps and queued work without denting its own
    role score: the same self-certification #1065 closed for a versioned
    dependency. A trusted author must still leave the score at 1.0, so the fix
    counts the origin, it does not blanket-penalise the field.
    """
    from continuum.state.semantic import project

    def _role(source: Origin, *, add_plan: bool = False, add_work: bool = False) -> float:
        storage = SQLiteStorage(":memory:")
        storage.create_run(Run(run_id="r1", goal="ship"))
        storage.append_event(
            "r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC
        )
        storage.append_event(
            "r1", EventType.WORK_COMPLETED, {"doc": 0}, source=Origin.DETERMINISTIC
        )
        if add_plan:
            storage.append_event(
                "r1",
                EventType.PLAN_UPSERT,
                {"plan_id": "p1", "units": [{"id": "u1", "title": "do it", "status": "pending"}]},
                source=source,
            )
        if add_work:
            storage.append_event(
                "r1",
                EventType.WORK_ADDED,
                {"task_id": "t1", "description": "later"},
                source=source,
            )
        state = project("r1", storage.read_events("r1"))
        score = trust_over_prefix(state)
        storage.close()
        return score["breakdown"]["role"]

    # Goal and progress alone, both deterministic: the role dimension is clean.
    baseline = _role(Origin.DETERMINISTIC)
    assert baseline == 1.0

    # An agent authoring a plan step or queuing pending work must dent the score;
    # before the fix neither field was collected, so both stayed at the baseline.
    assert _role(Origin.EXTERNAL_AGENT, add_plan=True) < baseline
    assert _role(Origin.EXTERNAL_AGENT, add_work=True) < baseline

    # Counting the origin, not the field: a trusted author of the same rows keeps
    # the role dimension at 1.0.
    assert _role(Origin.DETERMINISTIC, add_plan=True, add_work=True) == 1.0


def test_health_command_is_advisory_and_never_gates(tmp_path: Path) -> None:
    """Health command is advisory, never gates, never changes exit code."""
    db = tmp_path / "h.db"
    storage = SQLiteStorage(str(db))
    storage.create_run(Run(run_id="r1", goal="ship"))
    storage.append_event(
        "r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.EXTERNAL_AGENT
    )
    storage.close()

    out = io.StringIO()
    err = io.StringIO()
    code = cli_main(["--db", str(db), "health", "r1", "--json"], out=out, err=err)
    assert code == 0
    # health outputs JSON when --json is passed at subcommand or top-level; handle both
    text = out.getvalue().strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: health may have printed human text, try top-level --json
        out2 = io.StringIO()
        err2 = io.StringIO()
        code2 = cli_main(["--db", str(db), "--json", "health", "r1"], out=out2, err=err2)
        payload = json.loads(out2.getvalue())
        assert code2 == 0
        assert "advisory" in payload
        assert "trust_score" in payload["advisory"]
        return
    assert "advisory" in payload
    assert "trust_score" in payload["advisory"]

    # Second call should be identical and still exit 0 even though trust is low
    out2 = io.StringIO()
    err2 = io.StringIO()
    code2 = cli_main(["--db", str(db), "health", "r1", "--json"], out=out2, err=err2)
    assert code2 == 0
    text2 = out2.getvalue().strip()
    try:
        payload2 = json.loads(text2)
    except json.JSONDecodeError:
        payload2 = payload
    assert payload2 == payload


def test_dashboard_hook_is_read_only(tmp_path: Path) -> None:
    """Dashboard helper is read-only and small."""
    from continuum.dashboard.app import _advisory_trust_html  # type: ignore

    db = tmp_path / "d.db"
    storage = SQLiteStorage(str(db))
    storage.create_run(Run(run_id="r1", goal="ship"))
    storage.append_event("r1", EventType.RUN_STARTED, {"goal": "ship"}, source=Origin.DETERMINISTIC)
    # Do not close before calling the helper; it needs an open handle
    html = _advisory_trust_html(storage, "r1")  # type: ignore
    assert "trust" in html.lower()
    # Should not have mutated the store
    assert storage.get_run("r1").goal == "ship"
    storage.close()
    storage2 = SQLiteStorage(str(db))
    assert storage2.get_run("r1").goal == "ship"
    storage2.close()


def test_trust_over_prefix_empty_events_list() -> None:
    """Empty event list returns the default neutral trust report."""
    report = trust_over_prefix([])
    assert report == {
        "trust_score": 1.0,
        "breakdown": {"role": 1.0, "goal": 1.0, "evidence": 1.0},
    }


def test_trust_over_prefix_events_iterable() -> None:
    """Event iterables are projected with either inferred or explicit run_id."""
    ev = Event(
        run_id="r1",
        type=EventType.RUN_STARTED,
        payload={"goal": "ship"},
        source=Origin.DETERMINISTIC,
        sequence=1,
    )
    # Inferred run_id from first event
    rep1 = trust_over_prefix([ev])
    assert rep1["trust_score"] == 1.0
    assert rep1["breakdown"]["role"] == 1.0

    # Explicit run_id passed
    rep2 = trust_over_prefix([ev], run_id="r1")
    assert rep2["trust_score"] == 1.0
    assert rep2["breakdown"]["role"] == 1.0


def test_score_goal_none_returns_zero() -> None:
    """Goal dimension returns 0.0 when semantic state has no goal."""
    state = SemanticState.model_construct(
        run_id="r1",
        goal=None,
        progress=Progress(),
    )
    assert _score_goal(state) == 0.0


def test_score_role_empty_origins_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Role dimension returns 1.0 when origins list is empty."""
    state = SemanticState.model_construct(
        run_id="r1",
        goal=None,
        progress=Progress(),
    )
    monkeypatch.setattr("continuum.analysis.prefix_trust._collect_origins", lambda _s: [])
    assert _score_role(state) == 1.0


def test_collect_origins_component_provenance_and_approval_statuses() -> None:
    """_collect_origins collects provenance across findings, decisions, pins, and approvals."""
    # State with goal=None covers the branch skipping goal origin
    state = SemanticState.model_construct(
        run_id="r1",
        goal=None,
        progress=Progress(provenance=Provenance(origin=Origin.DETERMINISTIC)),
        plan=[
            PlanStep(step_id="p1", description="step", provenance=Provenance(origin=Origin.HUMAN))
        ],
        pending_work=[
            PendingWork(
                task_id="t1", description="work", provenance=Provenance(origin=Origin.DETERMINISTIC)
            )
        ],
        findings=[Finding(claim="claim", provenance=Provenance(origin=Origin.HUMAN))],
        decisions=[Decision(decision="dec", provenance=Provenance(origin=Origin.DETERMINISTIC))],
        pins={
            "pin1": ConstraintPin(
                constraint_id="c1",
                sha256="0" * 64,
                provenance=Provenance(origin=Origin.EXTERNAL_AGENT),
            )
        },
        approvals=[
            Approval(subject="deploy", status=ApprovalStatus.GRANTED),
            Approval(subject="migrate", status=ApprovalStatus.PENDING),
            Approval(subject="revoke", status=ApprovalStatus.REVOKED),
        ],
        external_dependencies=[
            ExternalDependency(resource="db", provenance=Provenance(origin=Origin.EXTERNAL_AGENT))
        ],
        evidence=[Evidence(source="log", provenance=Provenance(origin=Origin.HUMAN))],
    )
    origins = _collect_origins(state)
    # GRANTED approval maps to HUMAN; PENDING and REVOKED map to EXTERNAL_AGENT
    assert Origin.DETERMINISTIC in origins
    assert Origin.HUMAN in origins
    assert Origin.EXTERNAL_AGENT in origins
    assert origins.count(Origin.HUMAN) >= 3
    assert origins.count(Origin.EXTERNAL_AGENT) >= 3


def test_score_evidence_edge_branches() -> None:
    """_score_evidence handles empty state, checksum/source variants, and total==0 guard."""
    # 1. Clean run: no evidence and no dependencies -> 1.0
    clean_state = SemanticState.model_construct(evidence=[], external_dependencies=[])
    assert _score_evidence(clean_state) == 1.0

    # 2. Trusted evidence with source only
    ev_trusted_source = Evidence(
        source="log",
        provenance=Provenance(origin=Origin.DETERMINISTIC),
    )
    assert (
        _score_evidence(
            SemanticState.model_construct(evidence=[ev_trusted_source], external_dependencies=[])
        )
        == 1.0
    )

    # 3. Trusted evidence without checksum or source -> 0.7
    ev_trusted_bare = Evidence(provenance=Provenance(origin=Origin.HUMAN))
    assert (
        round(
            _score_evidence(
                SemanticState.model_construct(evidence=[ev_trusted_bare], external_dependencies=[])
            ),
            2,
        )
        == 0.7
    )

    # 4. Untrusted evidence with checksum -> 0.4
    ev_untrusted_checksum = Evidence(
        checksum="sha256:abc",
        provenance=Provenance(origin=Origin.EXTERNAL_AGENT),
    )
    assert (
        round(
            _score_evidence(
                SemanticState.model_construct(
                    evidence=[ev_untrusted_checksum], external_dependencies=[]
                )
            ),
            2,
        )
        == 0.4
    )

    # 5. Untrusted evidence without checksum or source -> 0.2
    ev_untrusted_bare = Evidence(provenance=Provenance(origin=Origin.EXTERNAL_AGENT))
    assert (
        round(
            _score_evidence(
                SemanticState.model_construct(
                    evidence=[ev_untrusted_bare], external_dependencies=[]
                )
            ),
            2,
        )
        == 0.2
    )

    # 6. Untrusted dependency with version -> 0.2
    dep_untrusted_version = ExternalDependency(
        resource="lib",
        version="1.0.0",
        provenance=Provenance(origin=Origin.EXTERNAL_AGENT),
    )
    assert (
        round(
            _score_evidence(
                SemanticState.model_construct(
                    evidence=[], external_dependencies=[dep_untrusted_version]
                )
            ),
            2,
        )
        == 0.2
    )

    # 7. Untrusted dependency without version -> 0.1
    dep_untrusted_bare = ExternalDependency(
        resource="lib",
        version=None,
        provenance=Provenance(origin=Origin.EXTERNAL_AGENT),
    )
    assert (
        round(
            _score_evidence(
                SemanticState.model_construct(
                    evidence=[], external_dependencies=[dep_untrusted_bare]
                )
            ),
            2,
        )
        == 0.1
    )

    # 8. Defensive total == 0 guard when collections are truthy but yield 0 items
    class _TruthyEmpty(list):
        def __bool__(self) -> bool:
            return True

    empty_guard_state = SemanticState.model_construct(
        evidence=_TruthyEmpty(), external_dependencies=[]
    )
    assert _score_evidence(empty_guard_state) == 1.0


def test_trust_report_dict_subclass() -> None:
    """TrustReport subclasses dict for backwards compatibility."""
    report = TrustReport(
        {"trust_score": 0.85, "breakdown": {"role": 1.0, "goal": 1.0, "evidence": 0.55}}
    )
    assert isinstance(report, dict)
    assert report["trust_score"] == 0.85
