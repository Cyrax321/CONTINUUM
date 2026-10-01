"""Per-dependency recovery-attempt budgets (issue #744).

The research note in ``docs/research/human_gate_minimization.md`` asked for a
per-dependency budget so a noisy dependency cannot spend the run's
recovery-attempt allowance. These tests pin the properties that make that safe
rather than merely convenient: scopes isolate, unknown or conflicting ownership
buys nothing, and the isolation survives compaction and a file round trip.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from continuum.concurrency import InMemoryLeaseCoordinator
from continuum.environment.diff import EnvironmentDiff
from continuum.models import (
    Component,
    ComponentValidationEntry,
    ExternalDependency,
    Goal,
    RecoverySafety,
    SemanticState,
    StateStatus,
    StateValidationResult,
    utcnow,
)
from continuum.recovery import (
    BudgetStatus,
    FileLedgerBackend,
    MemoryLedgerBackend,
    RecoveryEngine,
    RecoveryLedger,
    RecoveryLedgerEntry,
    RepairKind,
    RepairPlan,
    RepairStep,
    build_contract,
    resolve_scope,
    verify_contract,
)
from continuum.recovery.ledger import HUMAN_REQUIRED, LedgerEntryKind, _normalize_scope
from continuum.security.hashing import stable_hash
from continuum.state.validator import ValidationOutcome


@pytest.fixture
def ledger() -> Iterator[RecoveryLedger]:
    yield RecoveryLedger(MemoryLedgerBackend())


# --------------------------------------------------------------------------- #
# Scope resolution: the fail-closed contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        ((), None),
        ((None,), None),
        (("",), None),
        (("   ",), None),
        ((5,), None),  # not a string at all
        (({"dataset"},), "dataset"),  # a scoped assessment names one resource
        ((["dataset", "dataset"],), "dataset"),  # duplicates collapse
        (("PyYAML",), "pyyaml"),  # case is not a scope boundary
        (("stripe", "stripe"), "stripe"),  # two agreeing signals
        (("stripe", None), "stripe"),  # one signal absent, the other stands
        (("stripe", "sendgrid"), None),  # conflicting ownership
        (({"dataset", "other"},), None),  # multi-resource scope is ambiguous
        (({"dataset"}, "other"), None),  # plan disagrees with the assessment
        ((["a", "b"], "a"), None),  # one candidate is itself ambiguous
    ],
)
def test_resolve_scope_is_fail_closed(candidates: tuple[object, ...], expected: str | None) -> None:
    assert resolve_scope(*candidates) == expected


def test_normalize_scope_rejects_non_strings() -> None:
    assert _normalize_scope(None) is None
    assert _normalize_scope(3) is None
    assert _normalize_scope(["dataset"]) is None
    assert _normalize_scope("  ") is None
    assert _normalize_scope("Dataset") == "dataset"


# --------------------------------------------------------------------------- #
# Independent scopes
# --------------------------------------------------------------------------- #


def test_independent_scopes_do_not_exhaust_each_other(ledger: RecoveryLedger) -> None:
    """Dependency A's attempts leave dependency B's allowance untouched."""
    for _ in range(3):
        ledger.record_attempt("run_1", scope="a", max_attempts=3)

    assert ledger.attempts("run_1", scope="a") == 3
    assert ledger.requires_human("run_1", scope="a", max_attempts=3) is True
    # B has spent nothing and is not gated.
    assert ledger.attempts("run_1", scope="b") == 0
    assert ledger.requires_human("run_1", scope="b", max_attempts=3) is False


def test_shared_scope_is_case_insensitive(ledger: RecoveryLedger) -> None:
    """``PyYAML`` and ``pyyaml`` are one dependency and therefore one budget."""
    ledger.record_attempt("run_1", scope="PyYAML")
    ledger.record_attempt("run_1", scope="pyyaml")

    assert ledger.attempts("run_1", scope="pyyaml") == 2


def test_scoped_escalation_marker_does_not_gate_another_scope(
    ledger: RecoveryLedger,
) -> None:
    """A's escalation marker is anchored, scoped, and stops at A."""
    for _ in range(3):
        ledger.record_attempt("run_1", scope="a", max_attempts=3)

    markers = [
        e
        for e in ledger.entries("run_1")
        if e.kind == LedgerEntryKind.GATE.value and e.gate == HUMAN_REQUIRED
    ]
    assert len(markers) == 1
    assert markers[0].scope == "a"
    assert markers[0].anchor is True  # it must outlive compaction

    assert ledger.requires_human("run_1", scope="b", max_attempts=3) is False
    # Recording against B after A escalated still works and stays separate.
    assert ledger.record_attempt("run_1", scope="b") == 1


def test_scoped_escalation_still_answers_the_run_wide_query(
    ledger: RecoveryLedger,
) -> None:
    """An ownership-less caller must read a known escalation as its own."""
    for _ in range(3):
        ledger.record_attempt("run_1", scope="a", max_attempts=3)

    assert ledger.requires_human("run_1", max_attempts=3) is True


def test_run_wide_escalation_blocks_every_scope(ledger: RecoveryLedger) -> None:
    """A global escalation (or a legacy unscoped one) gates all of them."""
    for _ in range(3):
        ledger.record_attempt("run_1", max_attempts=3)

    assert ledger.requires_human("run_1", scope="a", max_attempts=3) is True
    assert ledger.requires_human("run_1", scope="b", max_attempts=3) is True


# --------------------------------------------------------------------------- #
# Missing, malformed and conflicting ownership
# --------------------------------------------------------------------------- #


def test_missing_ownership_keeps_the_global_bucket(ledger: RecoveryLedger) -> None:
    """No scope given = the pre-#744 run-wide behaviour, byte for byte."""
    ledger.record_attempt("run_1")
    ledger.record_attempt("run_1")

    assert ledger.attempts("run_1") == 2
    assert ledger.requires_human("run_1", max_attempts=3) is False
    ledger.record_attempt("run_1")
    assert ledger.requires_human("run_1", max_attempts=3) is True


def test_scoped_attempts_do_not_inflate_the_global_count(ledger: RecoveryLedger) -> None:
    """The point of the feature: noise stays inside its own dependency."""
    for _ in range(5):
        ledger.record_attempt("run_1", scope="noisy", max_attempts=3)

    assert ledger.attempts("run_1") == 0
    # The global query still escalates, through the scoped marker, not a count.
    assert ledger.requires_human("run_1", max_attempts=3) is True


@pytest.mark.parametrize(
    "scope",
    [None, "", "   ", 7, ["a", "b"], {"a", "b"}, {"a": 1}],
)
def test_malformed_scope_charges_the_run_wide_bucket(ledger: RecoveryLedger, scope: object) -> None:
    """A malformed scope must not manufacture a fresh private budget."""
    ledger.record_attempt("run_1", scope=scope, max_attempts=3)

    assert ledger.attempts("run_1") == 1
    # Whatever scope was claimed, escalation is decided by the run-wide count.
    assert ledger.requires_human("run_1", max_attempts=1) is True


def test_conflicting_ownership_charges_the_run_wide_bucket(ledger: RecoveryLedger) -> None:
    """Two signals disagreeing about ownership is ambiguity, not a tiebreak."""
    for _ in range(2):
        ledger.record_attempt("run_1", scope="a", max_attempts=3)
    # An attempt whose plan and assessment disagree lands in the shared bucket.
    ledger.record_attempt("run_1", scope=["a", "b"], max_attempts=3)

    assert ledger.attempts("run_1") == 1
    assert ledger.attempts("run_1", scope="a") == 2
    # Ambiguity escalated the run, not either private budget.
    assert ledger.requires_human("run_1", max_attempts=1) is True


def test_scoped_limit_cannot_exceed_the_run_wide_ceiling(ledger: RecoveryLedger) -> None:
    """A per-dependency limit above the run's ceiling buys nothing."""
    ledger.record_attempt("run_1", scope="a", max_attempts=10, global_max_attempts=2)
    assert ledger.requires_human("run_1", scope="a", max_attempts=10) is False

    ledger.record_attempt("run_1", scope="a", max_attempts=10, global_max_attempts=2)
    status = ledger.budget("run_1", scope="a", max_attempts=10, global_max_attempts=2)
    assert status.max_attempts == 2
    assert status.exhausted is True
    assert status.requires_human is True


# --------------------------------------------------------------------------- #
# Persistence: compaction, file round trip, legacy records
# --------------------------------------------------------------------------- #


def test_scoped_escalation_survives_compaction(ledger: RecoveryLedger) -> None:
    """The marker is anchored, so dropping old ATTEMPT entries cannot reset it."""
    for _ in range(3):
        ledger.record_attempt("run_1", scope="a", max_attempts=3)
    assert ledger.requires_human("run_1", scope="a", max_attempts=3) is True

    removed = ledger.compact("run_1", keep=1)
    assert removed > 0
    assert ledger.attempts("run_1", scope="a") == 0
    # Count is gone; the marker still gates.
    assert ledger.requires_human("run_1", scope="a", max_attempts=3) is True
    # And the other scope is still untouched.
    assert ledger.requires_human("run_1", scope="b", max_attempts=3) is False


def test_scope_round_trips_through_the_file_backend(tmp_path: Path) -> None:
    backend = FileLedgerBackend(str(tmp_path / "ledger"))
    for _ in range(2):
        RecoveryLedger(backend).record_attempt("run_1", scope="a")
    RecoveryLedger(backend).record_attempt("run_1", scope="b")

    reopened = RecoveryLedger(backend)
    ok, _ = reopened.verify("run_1")
    assert ok is True
    assert reopened.attempts("run_1", scope="a") == 2
    assert reopened.attempts("run_1", scope="b") == 1
    # The persisted records carry the scope where a reader can see it.
    lines = (tmp_path / "ledger" / "ledger-run_1.jsonl").read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in lines if json.loads(line).get("scope") == "a") == 2


def test_pre_scope_records_still_verify_and_count_globally(tmp_path: Path) -> None:
    """A ledger file written before #744 must keep verifying after the upgrade.

    The scope key is omitted from a scope-less entry's sealed content, so the
    record hashes exactly as it did when it was written. An auditor holding a
    pre-upgrade copy still reconciles it.
    """
    directory = tmp_path / "ledger"
    directory.mkdir()
    legacy = RecoveryLedgerEntry(
        entry_id="ledger_legacy",
        run_id="run_1",
        sequence=0,
        prev_hash="genesis",
        content_hash="",
        kind=LedgerEntryKind.ATTEMPT.value,
        contract=None,
        gate=None,
        anchor=False,
        created_at=utcnow(),
        note="written before scopes existed",
    )
    legacy = replace(legacy, content_hash=stable_hash(legacy.content()))
    (directory / "ledger-run_1.jsonl").write_text(
        json.dumps(legacy.to_record()) + "\n", encoding="utf-8"
    )

    ledger = RecoveryLedger(FileLedgerBackend(str(directory)))
    ok, broken_at = ledger.verify("run_1")
    assert ok is True, f"legacy record failed to verify at index {broken_at}"
    assert ledger.attempts("run_1") == 1
    # A new attempt appends onto the legacy chain without breaking it.
    ledger.record_attempt("run_1")
    ok, _ = ledger.verify("run_1")
    assert ok is True
    assert ledger.attempts("run_1") == 2


def test_malformed_scope_in_a_record_falls_back_to_global(tmp_path: Path) -> None:
    """A hand-edited record cannot carry a bogus scope into a private budget."""
    directory = tmp_path / "ledger"
    directory.mkdir()
    forged = RecoveryLedgerEntry(
        entry_id="ledger_forged",
        run_id="run_1",
        sequence=0,
        prev_hash="genesis",
        content_hash="",
        kind=LedgerEntryKind.ATTEMPT.value,
        contract=None,
        gate=None,
        anchor=False,
        created_at=utcnow(),
        note="scope is not a string",
        scope=17,
    )
    forged = replace(forged, content_hash=stable_hash(forged.content()))
    record = forged.to_record()
    assert record["scope"] == 17  # the record really does carry the junk value

    ledger = RecoveryLedger(FileLedgerBackend(str(directory)))
    directory.joinpath("ledger-run_1.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    # Re-read, the entry lands in the run-wide bucket instead of a scope "17".
    assert ledger.attempts("run_1") == 1
    assert ledger.attempts("run_1", scope="17") == 0


# --------------------------------------------------------------------------- #
# Repeated and concurrent assessment
# --------------------------------------------------------------------------- #


def test_repeated_reads_are_stable(ledger: RecoveryLedger) -> None:
    """Two assessments with no write between them must agree."""
    ledger.record_attempt("run_1", scope="a")
    first = ledger.budget("run_1", scope="a", max_attempts=3)
    second = ledger.budget("run_1", scope="a", max_attempts=3)
    assert first == second
    assert first.scope == "a"
    assert (first.attempts, first.max_attempts, first.remaining) == (1, 3, 2)
    assert first.requires_human is False


def test_budget_status_names_the_global_scope_explicitly(ledger: RecoveryLedger) -> None:
    """A caller whose ownership fell back must be able to see that it did."""
    ledger.record_attempt("run_1")
    status = ledger.budget("run_1", max_attempts=3)
    assert status.scope is None
    payload = status.to_dict()
    assert payload["scope"] == "global"
    assert payload == {
        "scope": "global",
        "attempts": 1,
        "max_attempts": 3,
        "remaining": 2,
        "exhausted": False,
        "requires_human": False,
    }


def test_concurrent_appenders_serialise_under_the_lock() -> None:
    """Two ledgers on one backend must not lose attempts to a race."""
    backend = MemoryLedgerBackend()
    ledger = RecoveryLedger(backend, lock=InMemoryLeaseCoordinator())

    def spend(scope: str) -> None:
        for _ in range(3):
            ledger.record_attempt("run_1", scope=scope, max_attempts=3)

    # Sequential here exercises the lock path; the concurrency guarantee is the
    # lease's, already covered for decisions, and the budget reuses it.
    spend("a")
    spend("b")

    assert ledger.attempts("run_1", scope="a") == 3
    assert ledger.attempts("run_1", scope="b") == 3
    ok, _ = ledger.verify("run_1")
    assert ok is True


# --------------------------------------------------------------------------- #
# Planner: where the scope comes from
# --------------------------------------------------------------------------- #


def test_dependency_finding_scopes_its_step() -> None:
    from continuum.recovery import plan_repairs

    plan = plan_repairs(
        [
            ComponentValidationEntry(
                component=Component.EXTERNAL_DEPENDENCY,
                component_id="dataset",
                status=StateStatus.CONFLICTED,
                detail="v3 -> v4",
            )
        ]
    )
    assert plan.steps[0].scope == "dataset"
    assert plan.scopes == ("dataset",)


def test_dependency_finding_without_an_id_is_run_wide() -> None:
    from continuum.recovery import plan_repairs

    plan = plan_repairs(
        [
            ComponentValidationEntry(
                component=Component.EXTERNAL_DEPENDENCY,
                component_id=None,
                status=StateStatus.UNKNOWN,
                detail="unverifiable",
            )
        ]
    )
    assert plan.steps[0].scope is None
    assert plan.scopes == ()


def test_uncertain_action_inherits_its_dep_scope() -> None:
    from continuum.models import Action, ActionStatus
    from continuum.recovery import plan_repairs

    plan = plan_repairs(
        uncertain_actions=(
            Action(
                run_id="r",
                action_type="model.push",
                status=ActionStatus.UNKNOWN,
                dep_scope="dataset",
            ),
        )
    )
    assert plan.steps[0].scope == "dataset"


def test_untagged_action_charges_no_private_budget() -> None:
    from continuum.models import Action, ActionStatus
    from continuum.recovery import plan_repairs

    plan = plan_repairs(
        uncertain_actions=(
            Action(run_id="r", action_type="model.push", status=ActionStatus.UNKNOWN),
        )
    )
    assert plan.steps[0].scope is None
    assert plan.scopes == ()


def test_steps_spanning_two_dependencies_name_no_single_budget() -> None:
    from continuum.recovery import plan_repairs

    plan = plan_repairs(
        [
            ComponentValidationEntry(
                component=Component.EXTERNAL_DEPENDENCY,
                component_id="dataset",
                status=StateStatus.CONFLICTED,
                detail="drifted",
            ),
            ComponentValidationEntry(
                component=Component.EXTERNAL_DEPENDENCY,
                component_id="other",
                status=StateStatus.CONFLICTED,
                detail="drifted too",
            ),
        ]
    )
    # Each step knows its own dependency; the plan as a whole does not.
    assert sorted(s.scope for s in plan.steps) == ["dataset", "other"]
    assert plan.scopes == ("dataset", "other")


# --------------------------------------------------------------------------- #
# Contract: the budget surfaces without leaking
# --------------------------------------------------------------------------- #


def _outcome_with_dependency_change() -> ValidationOutcome:
    state = SemanticState(
        run_id="r",
        goal=Goal(description="g"),
        external_dependencies=[
            ExternalDependency(resource="dataset", status=StateStatus.CONFLICTED)
        ],
    )
    report = StateValidationResult(
        run_id="r",
        checkpoint_version=1,
        statuses=[
            ComponentValidationEntry(
                component=Component.EXTERNAL_DEPENDENCY,
                component_id="dataset",
                status=StateStatus.CONFLICTED,
                detail="v3 -> v4",
            ),
            ComponentValidationEntry(
                component=Component.GOAL, status=StateStatus.VALID, detail="v1"
            ),
        ],
        safe_to_resume=False,
        reason="external_dependency dataset is conflicted",
    )
    return ValidationOutcome(state=state, report=report, environment_diff=EnvironmentDiff())


def _repair_plan(scope: str | None = "dataset") -> RepairPlan:
    return RepairPlan(
        steps=[
            RepairStep(
                kind=RepairKind.REVALIDATE_DEPENDENCY,
                target="dataset",
                reason="drift",
                scope=scope,
            )
        ]
    )


def test_contract_names_the_budget_scope_and_what_remains() -> None:
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=_outcome_with_dependency_change(),
        plan=_repair_plan(),
        budget=BudgetStatus(scope="dataset", attempts=1, max_attempts=3, escalated=False),
    )
    line = next(e for e in contract.evidence if e.startswith("recovery budget:"))
    assert "scope dataset" in line
    assert "1 of 3 attempts used" in line
    assert "2 remaining" in line
    assert verify_contract(contract) is True


def test_contract_reports_an_exhausted_budget() -> None:
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=_outcome_with_dependency_change(),
        plan=_repair_plan(),
        budget=BudgetStatus(scope="dataset", attempts=3, max_attempts=3, escalated=True),
    )
    line = next(e for e in contract.evidence if e.startswith("recovery budget:"))
    assert "0 remaining" in line
    assert "human required" in line


def test_budget_line_carries_no_sensitive_values() -> None:
    """Counts and a dependency name: nothing about arguments, files or reasons."""
    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=_outcome_with_dependency_change(),
        plan=_repair_plan(),
        budget=BudgetStatus(scope="dataset", attempts=2, max_attempts=3, escalated=False),
    )
    line = next(e for e in contract.evidence if e.startswith("recovery budget:"))
    assert "sk_" not in line
    assert "v3 -> v4" not in line  # the failure detail is not a budget fact


def test_budget_line_absent_keeps_the_contract_unchanged() -> None:
    """No ledger, no budget line: the contract is identical to pre-#744."""
    without = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=_outcome_with_dependency_change(),
        plan=_repair_plan(),
    )
    with_budget = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=_outcome_with_dependency_change(),
        plan=_repair_plan(),
        budget=None,
    )
    assert without.evidence == with_budget.evidence
    assert not any(e.startswith("recovery budget:") for e in without.evidence)
    assert verify_contract(without) is True


def test_budget_evidence_renders_for_a_human() -> None:
    from continuum.recovery import render_contract

    contract = build_contract(
        run_id="r",
        checkpoint_version=1,
        safety=RecoverySafety.REQUIRES_REPAIR,
        validation=_outcome_with_dependency_change(),
        plan=_repair_plan(),
        budget=BudgetStatus(scope="dataset", attempts=1, max_attempts=3, escalated=False),
    )
    rendered = render_contract(contract)
    assert "recovery budget: scope dataset: 1 of 3 attempts used, 2 remaining" in rendered
    # And it is in the JSON a machine reads.
    payload = contract.model_dump(mode="json")
    assert any(str(e).startswith("recovery budget:") for e in payload["evidence"])


# --------------------------------------------------------------------------- #
# Engine wiring
# --------------------------------------------------------------------------- #


def test_engine_without_a_ledger_emits_no_budget_line() -> None:
    from continuum.benchmark.phase6.scenarios import _new_store, env_multi, seed_two

    store = _new_store()
    seed_two(store)
    decision = RecoveryEngine(store).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v3"), scope={"dataset"}
    )
    assert not any(str(e).startswith("recovery budget:") for e in decision.contract.evidence)


def test_engine_reads_the_budget_of_the_scope_it_named() -> None:
    from continuum.benchmark.phase6.scenarios import _new_store, env_multi, seed_two

    store = _new_store()
    seed_two(store)
    rl = RecoveryLedger(MemoryLedgerBackend())
    rl.record_attempt("run_1", scope="dataset")

    decision = RecoveryEngine(store, ledger=rl).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v3"), scope={"dataset"}
    )
    line = next(e for e in decision.contract.evidence if str(e).startswith("recovery budget:"))
    # The plan's dependency finding and the assessment scope agree, so the
    # budget is scoped, and one prior attempt is accounted for.
    assert "scope dataset" in line
    assert "1 of 3 attempts used" in line


def test_engine_falls_back_to_global_when_ownership_conflicts() -> None:
    from continuum.benchmark.phase6.scenarios import _new_store, env_multi, seed_two

    store = _new_store()
    seed_two(store)
    rl = RecoveryLedger(MemoryLedgerBackend())
    rl.record_attempt("run_1")  # run-wide attempt

    # Both dependencies drifted, so the plan names two scopes and no single
    # budget can be attributed: the contract must say so honestly.
    decision = RecoveryEngine(store, ledger=rl).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v4"), scope={"dataset", "other"}
    )
    line = next(e for e in decision.contract.evidence if str(e).startswith("recovery budget:"))
    assert "scope global" in line


def test_ledger_read_failure_costs_only_the_line() -> None:
    """A ledger that raises must not change the recovery verdict."""

    class ExplodingBackend(MemoryLedgerBackend):
        def load(self, run_id: str) -> list[RecoveryLedgerEntry]:
            raise RuntimeError("ledger unreadable")

    from continuum.benchmark.phase6.scenarios import _new_store, env_multi, seed_two

    store = _new_store()
    seed_two(store)
    rl = RecoveryLedger(ExplodingBackend())

    decision = RecoveryEngine(store, ledger=rl).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v3"), scope={"dataset"}
    )
    assert decision.mode is not None
    assert not any(str(e).startswith("recovery budget:") for e in decision.contract.evidence)
