"""Domain validation rules in recovery assessment (issue #761).

Rules are the seam an integration uses to contribute staleness that built-in
validation cannot see: a decision void because the policy it cites was revoked,
not because any dependency moved. This suite covers the runner, the merge, the
engine wiring, and the conformance check a third-party rule author runs against
their own rule.

Every test here asserts a property of the trust boundary rather than an
implementation detail, because the boundary is the feature: a rule may add
caution and nothing else, and a rule that misbehaves becomes a diagnosable
finding rather than a silent gap.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import (
    Component,
    ComponentValidationEntry,
    EnvironmentSnapshot,
    Goal,
    RecoveryMode,
    RecoverySafety,
    Run,
    SemanticState,
    StateStatus,
)
from continuum.plugins import (
    Registry,
    RevokedApprovalRule,
    ValidationRule,
    check_validation_rule,
)
from continuum.recovery import RecoveryDecision, RecoveryEngine, verify_contract
from continuum.recovery.rules import (
    STATUS_CAUTION,
    active_rules,
    apply_rule_findings,
    merge_validation_entries,
    run_validation_rules,
)
from continuum.state.validator import StateValidator
from continuum.storage import SQLiteStorage

# --- fixtures --------------------------------------------------------------- #


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
    storage.append_event(
        "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
    )
    yield storage
    storage.close()


def env(dataset: str = "v3") -> EnvironmentSnapshot:  # type: ignore[name-defined]
    return capture("run_1", StaticProvider(dataset=dataset))


def seed(store: SQLiteStorage, *, dataset: str = "v3", decision: str = "policy P-42") -> None:
    """A clean run with one decision authorized by an approval, then a checkpoint."""
    store.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": dataset}
    )
    store.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "paper_1", "summary": "study", "source": "dataset"},
    )
    store.append_event(
        "run_1",
        EventType.FINDING_ADDED,
        {"finding_id": "finding_1", "claim": "X", "evidence": ["paper_1"]},
    )
    store.append_event(
        "run_1",
        EventType.DECISION_CREATED,
        {
            "decision_id": "decision_1",
            "decision": decision,
            "reason": "cites policy P-42",
            "evidence": ["finding_1"],
        },
    )
    store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": 0})
    CheckpointManager(store).checkpoint("run_1", environment=env(dataset))


def revoke(store: SQLiteStorage, subject: str = "decision_1") -> None:
    """Request an approval for ``subject`` and then revoke it."""
    store.append_event(
        "run_1", EventType.APPROVAL_REQUESTED, {"approval_id": "approval_1", "subject": subject}
    )
    store.append_event(
        "run_1", EventType.APPROVAL_REVOKED, {"approval_id": "approval_1", "subject": subject}
    )


# --- test rules ------------------------------------------------------------ #


class _LoweringRule:
    """A rule that tries to relax a finding. Merge must refuse."""

    name = "test:lowering"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return [
            ComponentValidationEntry(
                component=Component.DECISION,
                component_id="decision_1",
                status=StateStatus.VALID,
                detail="the rule says it is fine, actually",
                rule=self.name,
            )
        ]


class _CrashingRule:
    """A rule whose evaluate raises."""

    name = "test:crashing"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("the policy service is on fire")


class _NoNameRule:
    """A rule with no name attribute at all."""

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return []


class _NonStringNameRule:
    name = 7

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return []


class _MalformedReturnRule:
    name = "test:malformed_return"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return "not a list"


class _MalformedItemRule:
    name = "test:malformed_item"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return [
            ComponentValidationEntry(
                component=Component.GOAL, status=StateStatus.VALID, rule=self.name
            ),
            "not an entry",
        ]


class _ForgedNamespaceRule:
    """A rule that labels its finding with another rule's name."""

    name = "test:forged"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return [
            ComponentValidationEntry(
                component=Component.GOAL,
                status=StateStatus.REQUIRES_REVIEW,
                detail="someone else said this",
                rule="builtin:revoked_approval",
            )
        ]


class _Counter:
    """Shared mutable counter, so two evaluations of the same rule differ."""

    def __init__(self) -> None:
        self.calls = 0


class _NondeterministicRule:
    name = "test:nondeterministic"

    def __init__(self, counter: _Counter) -> None:
        self._counter = counter

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        self._counter.calls += 1
        status = StateStatus.VALID if self._counter.calls % 2 else StateStatus.STALE
        return [ComponentValidationEntry(component=Component.GOAL, status=status, rule=self.name)]


class _MutatingRule:
    name = "test:mutating"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        # Frozen pydantic models refuse attribute assignment, but a list field
        # is still appendable, which is the mutation this rule must not get away
        # with.
        state.decisions.append(
            type(state.decisions[0])(decision_id="injected", decision="forged")
            if state.decisions
            else None
        )
        return []


class _UnrelatedRule:
    """A second rule, so composition and ordering have something to compose."""

    name = "test:unrelated"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return [
            ComponentValidationEntry(
                component=Component.PROGRESS,
                status=StateStatus.REQUIRES_REVIEW,
                detail="progress is asserted by an agent",
                rule=self.name,
            )
        ]


def _entry(
    component: Component,
    component_id: str | None,
    status: StateStatus,
    *,
    rule: str | None = None,
    detail: str = "",
) -> ComponentValidationEntry:
    return ComponentValidationEntry(
        component=component,
        component_id=component_id,
        status=status,
        detail=detail,
        rule=rule,
    )


# --- merge: most cautious wins, order does not ----------------------------- #


def test_merge_takes_the_more_cautious_status() -> None:
    """A rule may raise a component's status, never lower it."""
    builtin = [
        _entry(Component.DECISION, "decision_1", StateStatus.VALID),
        _entry(Component.GOAL, None, StateStatus.VALID),
    ]
    findings = [_entry(Component.DECISION, "decision_1", StateStatus.INVALID, rule="test:r")]

    merged = merge_validation_entries(builtin, findings)

    assert merged[0].status is StateStatus.INVALID
    assert merged[0].rule == "test:r"
    assert merged[1].status is StateStatus.VALID


def test_merge_refuses_to_lower_a_finding() -> None:
    """A rule reporting VALID for a component built-in downgraded changes nothing."""
    builtin = [
        _entry(Component.DECISION, "decision_1", StateStatus.STALE),
        _entry(Component.EXTERNAL_DEPENDENCY, "dataset", StateStatus.CONFLICTED),
    ]
    findings = [
        _entry(Component.DECISION, "decision_1", StateStatus.VALID, rule="test:r"),
        _entry(Component.EXTERNAL_DEPENDENCY, "dataset", StateStatus.VALID, rule="test:r"),
    ]

    merged = merge_validation_entries(builtin, findings)

    assert [e.status for e in merged] == [StateStatus.STALE, StateStatus.CONFLICTED]
    # The incumbent wording survives: a rule that agrees adds no prose.
    assert all(e.rule is None for e in merged)


def test_merge_is_order_independent() -> None:
    """Two rules disagreeing about one component resolve the same way either way."""
    a = _entry(Component.DECISION, "decision_1", StateStatus.STALE, rule="test:a")
    b = _entry(Component.DECISION, "decision_1", StateStatus.INVALID, rule="test:b")
    builtin = [_entry(Component.DECISION, "decision_1", StateStatus.VALID)]

    first = merge_validation_entries(builtin, [a, b])
    second = merge_validation_entries(builtin, [b, a])

    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]
    assert first[0].status is StateStatus.INVALID


def test_merge_keeps_builtin_order_and_appends_new_components() -> None:
    """A component only a rule examined follows the built-in ones."""
    builtin = [
        _entry(Component.GOAL, None, StateStatus.VALID),
        _entry(Component.PROGRESS, None, StateStatus.VALID),
    ]
    findings = [_entry(Component.PIN, "pin_1", StateStatus.EXPIRED, rule="test:r")]

    merged = merge_validation_entries(builtin, findings)

    assert [e.component for e in merged] == [Component.GOAL, Component.PROGRESS, Component.PIN]


def test_merge_with_no_findings_returns_the_input_unchanged() -> None:
    builtin = [_entry(Component.GOAL, None, StateStatus.VALID)]
    assert merge_validation_entries(builtin, []) == builtin


def test_status_caution_orders_valid_below_everything_else() -> None:
    """VALID is the minimum, which is what makes a rule unable to launder a finding."""
    assert min(STATUS_CAUTION.values()) == STATUS_CAUTION[StateStatus.VALID]
    others = (s for s in STATUS_CAUTION if s is not StateStatus.VALID)
    assert all(STATUS_CAUTION[s] > STATUS_CAUTION[StateStatus.VALID] for s in others)


# --- runner: fail closed --------------------------------------------------- #


def _state() -> SemanticState:
    return SemanticState(run_id="run_1", goal=Goal(description="g"))


def test_run_stamps_findings_with_the_rule_name() -> None:
    findings = run_validation_rules([RevokedApprovalRule()], _state())
    for entry in findings:
        assert entry.rule == "builtin:revoked_approval"


def test_run_relabels_a_forged_namespace() -> None:
    """A rule may not report a finding in another rule's namespace."""
    findings = run_validation_rules([_ForgedNamespaceRule()], _state())

    assert len(findings) == 1
    assert findings[0].rule == "test:forged"


def test_run_reports_a_rule_with_no_name() -> None:
    findings = run_validation_rules([_NoNameRule(), _NonStringNameRule()], _state())

    assert len(findings) == 2
    assert all(e.component is Component.VALIDATION_RULE for e in findings)
    assert all(e.status is StateStatus.REQUIRES_REVIEW for e in findings)
    assert all(e.rule is None for e in findings)


def test_run_reports_a_crashing_rule() -> None:
    findings = run_validation_rules([_CrashingRule()], _state())

    assert len(findings) == 1
    entry = findings[0]
    assert entry.component is Component.VALIDATION_RULE
    assert entry.component_id == "test:crashing"
    assert entry.status is StateStatus.REQUIRES_REVIEW
    assert "RuntimeError" in entry.detail
    assert "the policy service is on fire" in entry.detail


def test_run_reports_a_malformed_return() -> None:
    findings = run_validation_rules([_MalformedReturnRule()], _state())

    assert findings[0].component_id == "test:malformed_return"
    assert "expected a list" in findings[0].detail


def test_run_reports_a_malformed_item_but_keeps_the_good_one() -> None:
    """One bad entry does not discard the rule's well-formed finding."""
    findings = run_validation_rules([_MalformedItemRule()], _state())

    assert len(findings) == 2
    good = next(e for e in findings if e.component is Component.GOAL)
    bad = next(e for e in findings if e.component is Component.VALIDATION_RULE)
    assert good.status is StateStatus.VALID
    assert "expected ComponentValidationEntry" in bad.detail


def test_run_reports_a_duplicate_rule_name() -> None:
    """Two rules sharing a name cannot both be namespaced by it."""
    findings = run_validation_rules([RevokedApprovalRule(), _DuplicateNameRule()], _state())

    dup = next(e for e in findings if e.component_id == "builtin:revoked_approval" and e.detail)
    assert dup.status is StateStatus.REQUIRES_REVIEW
    assert "registered more than once" in dup.detail


class _DuplicateNameRule:
    name = "builtin:revoked_approval"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return []


def test_active_rules_combines_the_collections_in_order() -> None:
    combined = active_rules([RevokedApprovalRule()], None, [_UnrelatedRule()])

    assert len(combined) == 2
    assert combined[0].name == "builtin:revoked_approval"
    assert combined[1].name == "test:unrelated"


# --- apply_rule_findings: the report and its safety ------------------------ #


def test_apply_rebuilds_safety_and_reason() -> None:
    """A rule finding withholds resume exactly like a built-in one of the same status."""
    outcome = StateValidator().validate(_state())
    assert outcome.safe

    findings = [
        _entry(Component.GOAL, None, StateStatus.REQUIRES_REVIEW, rule="test:r", detail="why")
    ]
    applied = apply_rule_findings(outcome, findings, strict_unknown=True)

    assert not applied.report.safe_to_resume
    assert applied.report.reason == "goal is requires_review"
    goal = next(e for e in applied.report.statuses if e.component is Component.GOAL)
    assert goal.rule == "test:r"
    assert goal.status is StateStatus.REQUIRES_REVIEW
    # The state the caller acts on is untouched; only the report moved.
    assert applied.state is outcome.state


def test_apply_returns_the_input_when_findings_change_nothing() -> None:
    """A rule that reports VALID for everything leaves the report byte-identical."""
    outcome = StateValidator().validate(_state())
    findings = [_entry(Component.GOAL, None, StateStatus.VALID, rule="test:r")]

    assert apply_rule_findings(outcome, findings, strict_unknown=True) is outcome


def test_apply_honours_lenient_unknown() -> None:
    """A rule reporting UNKNOWN does not withhold resume when the caller tolerated it."""
    outcome = StateValidator().validate(_state())
    findings = [_entry(Component.PIN, "pin_1", StateStatus.UNKNOWN, rule="test:r")]

    strict = apply_rule_findings(outcome, findings, strict_unknown=True)
    lenient = apply_rule_findings(outcome, findings, strict_unknown=False)

    assert not strict.report.safe_to_resume
    assert lenient.report.safe_to_resume


# --- engine: default behaviour, escalation, containment -------------------- #


def test_an_engine_with_no_rules_behaves_as_before(store: SQLiteStorage) -> None:
    """The default path is byte-identical: no rules, no change."""
    seed(store)

    baseline = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    with_rules = RecoveryEngine(store, validation_rules=[RevokedApprovalRule()]).assess(
        "run_1", current_environment=env("v3")
    )

    # A rule that finds nothing must not perturb the report or the contract.
    assert baseline.mode is RecoveryMode.RESUME
    assert with_rules.mode is RecoveryMode.RESUME
    assert [e.model_dump() for e in baseline.validation.report.statuses] == [
        e.model_dump() for e in with_rules.validation.report.statuses
    ]
    assert baseline.contract.integrity_hash == with_rules.contract.integrity_hash


def test_a_rule_escalates_a_decision_to_invalid(store: SQLiteStorage) -> None:
    """A rule's finding withholds resume and names the repair the finding implies."""
    seed(store)

    decision = RecoveryEngine(store, validation_rules=[_RaisingRule()]).assess(
        "run_1", current_environment=env("v3")
    )
    baseline = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))

    assert baseline.mode is RecoveryMode.RESUME
    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert decision.contract.recovery_status is RecoverySafety.REQUIRES_REPAIR
    entry = next(
        e
        for e in decision.validation.report.statuses
        if e.component is Component.DECISION and e.component_id == "decision_1"
    )
    assert entry.status is StateStatus.INVALID
    assert entry.rule == "test:raising"
    # A decision repair is a review, not a human-only step.
    assert any(
        step.target == "decision_1" and step.requires_human is False for step in decision.plan.steps
    )
    assert verify_contract(decision.contract)


def test_the_builtin_rule_invalidates_what_a_revoked_approval_authorized(
    store: SQLiteStorage,
) -> None:
    """A revoked authorization pulls the decision it authorized to invalid."""
    seed(store)
    revoke(store)

    without = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))
    with_rule = RecoveryEngine(store, validation_rules=[RevokedApprovalRule()]).assess(
        "run_1", current_environment=env("v3")
    )

    # Built-in validation already grades the approval itself; what it cannot do
    # is follow the revocation to the decision the approval authorized.
    approval = next(
        e
        for e in without.validation.report.statuses
        if e.component is Component.APPROVAL and e.component_id == "approval_1"
    )
    assert approval.status is StateStatus.INVALID
    assert not any(
        e.component is Component.DECISION and e.component_id == "decision_1"
        for e in without.validation.report.statuses
    )
    entry = next(
        e
        for e in with_rule.validation.report.statuses
        if e.component is Component.DECISION and e.component_id == "decision_1"
    )
    assert entry.status is StateStatus.INVALID
    assert entry.rule == "builtin:revoked_approval"
    assert "approval_1 was revoked" in entry.detail
    # The most cautious proposal wins: the approval's own renewal needs a
    # person, so the decision repair does not lower the verdict.
    assert with_rule.mode is RecoveryMode.REQUEST_HUMAN


def test_rule_findings_are_namespaced_in_the_contract_and_the_render(
    store: SQLiteStorage,
) -> None:
    """Both the sealed contract and the text rendering attribute a rule's finding."""
    seed(store)
    revoke(store)

    decision = RecoveryEngine(store, validation_rules=[RevokedApprovalRule()]).assess(
        "run_1", current_environment=env("v3")
    )

    # The JSON surface: the contract's invalidated and evidence lists.
    payload = decision.contract.model_dump(mode="json")
    assert any(
        "decision:decision_1 [rule:builtin:revoked_approval]" in line
        for line in payload["invalidated"]
    )
    assert any("rule:builtin:revoked_approval" in line for line in payload["evidence"])
    # The text surface.
    assert "[rule:builtin:revoked_approval]" in decision.render()
    # And the finding is covered by the seal, not decoration beside it.
    assert verify_contract(decision.contract)


def test_a_contract_with_no_rule_findings_seals_as_it_always_did(
    store: SQLiteStorage,
) -> None:
    """Rule namespacing adds no contract key, so a clean run is unchanged."""
    seed(store)

    decision = RecoveryEngine(store).assess("run_1", current_environment=env("v3"))

    assert verify_contract(decision.contract)
    assert not any("[rule:" in line for line in decision.contract.evidence)
    assert not any("[rule:" in line for line in decision.contract.invalidated)


def test_a_rule_cannot_lower_a_builtin_finding_through_the_engine(
    store: SQLiteStorage,
) -> None:
    """A changed dependency makes the decision stale; a rule cannot talk it clean."""
    seed(store)

    decision = RecoveryEngine(store, validation_rules=[_LoweringRule()]).assess(
        "run_1", current_environment=env("v4")
    )

    entry = next(
        e
        for e in decision.validation.report.statuses
        if e.component is Component.DECISION and e.component_id == "decision_1"
    )
    assert entry.status is StateStatus.STALE
    assert entry.rule is None
    assert decision.mode is not RecoveryMode.RESUME


def test_a_crashing_rule_escalates_to_human_instead_of_being_ignored(
    store: SQLiteStorage,
) -> None:
    """A broken rule becomes a diagnosable finding, not a silent gap."""
    seed(store)

    before_events = len(store.read_events("run_1"))
    before_version = store.latest_version("run_1")
    decision = RecoveryEngine(store, validation_rules=[_CrashingRule()]).assess(
        "run_1", current_environment=env("v3")
    )

    entry = next(
        e for e in decision.validation.report.statuses if e.component is Component.VALIDATION_RULE
    )
    assert entry.status is StateStatus.REQUIRES_REVIEW
    assert entry.component_id == "test:crashing"
    assert decision.mode is RecoveryMode.REQUEST_HUMAN
    assert any(
        step.requires_human and step.target == "test:crashing" for step in decision.plan.steps
    )

    # The trust boundary: assessment with a hostile rule changed nothing.
    assert len(store.read_events("run_1")) == before_events
    assert store.latest_version("run_1") == before_version


def test_rules_do_not_receive_a_storage_handle(store: SQLiteStorage) -> None:
    """The seam passes state and environment only, so a rule cannot write."""
    captured: dict[str, object] = {}

    class _InspectionRule:
        name = "test:inspection"

        def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
            captured["has_storage"] = hasattr(state, "storage") or hasattr(
                environment or object(), "storage"
            )
            captured["state_type"] = type(state).__name__
            return []

    seed(store)
    RecoveryEngine(store, validation_rules=[_InspectionRule()]).assess(
        "run_1", current_environment=env("v3")
    )

    assert captured["state_type"] == "SemanticState"
    assert captured["has_storage"] is False


def test_a_registry_supplies_the_rules(store: SQLiteStorage) -> None:
    """The registry-backed spelling runs every registered ValidationRule."""
    seed(store)
    registry = Registry()
    registry.register("raising", _RaisingRule())
    # A service that is not a rule is ignored, not misread as one.
    registry.register("not_a_rule", object())

    decision = RecoveryEngine(store, registry=registry).assess(
        "run_1", current_environment=env("v3")
    )

    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert any(e.rule == "test:raising" for e in decision.validation.report.statuses)


def test_per_call_rules_add_to_the_engines_rules(store: SQLiteStorage) -> None:
    """A caller's rules extend rather than replace the engine's configured set."""
    seed(store)
    revoke(store)
    engine = RecoveryEngine(store, validation_rules=[RevokedApprovalRule()])

    decision = engine.assess(
        "run_1", current_environment=env("v3"), validation_rules=[_UnrelatedRule()]
    )

    statuses = decision.validation.report.statuses
    assert any(e.rule == "builtin:revoked_approval" for e in statuses)
    assert any(
        e.component is Component.PROGRESS and e.status is StateStatus.REQUIRES_REVIEW
        for e in statuses
    )


def test_two_rules_compose_by_severity_regardless_of_order(store: SQLiteStorage) -> None:
    """The most cautious of two rules' findings wins, in either registration order."""
    seed(store)

    def run_in(order: tuple[str, str]) -> RecoveryDecision:  # type: ignore[type-arg]
        rules = {
            "raising": _RaisingRule(),
            "lowering": _LoweringRule(),
        }
        engine = RecoveryEngine(store, validation_rules=[rules[name] for name in order])
        return engine.assess("run_1", current_environment=env("v3"))

    first = run_in(("raising", "lowering"))
    second = run_in(("lowering", "raising"))

    for decision in (first, second):
        entry = next(
            e
            for e in decision.validation.report.statuses
            if e.component is Component.DECISION and e.component_id == "decision_1"
        )
        assert entry.status is StateStatus.INVALID
        assert entry.rule == "test:raising"
    assert [e.model_dump() for e in first.validation.report.statuses] == [
        e.model_dump() for e in second.validation.report.statuses
    ]


class _RaisingRule:
    name = "test:raising"

    def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
        return [
            ComponentValidationEntry(
                component=Component.DECISION,
                component_id="decision_1",
                status=StateStatus.INVALID,
                detail="revoked by the rule",
                rule=self.name,
            )
        ]


# --- conformance ----------------------------------------------------------- #


def test_the_builtin_rule_conforms() -> None:
    """The shipped example satisfies the contract the seam documents."""
    report = check_validation_rule(RevokedApprovalRule())
    assert report.passed, report.failures
    assert report.rule_name == "builtin:revoked_approval"


def test_conformance_detects_a_nondeterministic_rule() -> None:
    counter = _Counter()
    report = check_validation_rule(_NondeterministicRule(counter))

    assert not report.passed
    assert any("not deterministic" in failure for failure in report.failures)


def test_conformance_detects_a_mutating_rule() -> None:
    report = check_validation_rule(_MutatingRule())

    assert not report.passed
    assert any("mutated the state" in failure for failure in report.failures)


def test_conformance_detects_a_rule_with_no_name() -> None:
    report = check_validation_rule(_NoNameRule())

    assert not report.passed
    assert report.rule_name is None
    assert any("'name'" in failure for failure in report.failures)


def test_conformance_detects_a_forged_namespace() -> None:
    report = check_validation_rule(_ForgedNamespaceRule())

    assert not report.passed
    assert any("not this rule's name" in failure for failure in report.failures)


def test_conformance_reports_a_state_the_rule_raises_on() -> None:
    """A rule may be well-behaved on one state and broken on another."""

    class _RaisesOnRichRule:
        name = "test:raises_on_rich"

        def evaluate(self, state, environment=None):  # type: ignore[no-untyped-def]
            if state.decisions:
                raise RuntimeError("cannot cope with decisions")
            return []

    report = check_validation_rule(_RaisesOnRichRule())

    assert not report.passed
    assert any("conformance_rich" in failure for failure in report.failures)


def test_conformance_accepts_a_callers_own_state() -> None:
    """An integration exercises its representative state, not just the defaults."""
    state = SemanticState(run_id="mine", goal=Goal(description="g"))

    report = check_validation_rule(RevokedApprovalRule(), states=[state])

    assert report.passed, report.failures
    assert len(report.findings) == 1


def test_the_builtin_rule_satisfies_the_protocol() -> None:
    assert isinstance(RevokedApprovalRule(), ValidationRule)
