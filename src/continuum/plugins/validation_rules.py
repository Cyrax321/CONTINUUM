"""A built-in domain validation rule, and the conformance check rule authors run (#761).

This is the reference implementation of the :class:`ValidationRule` seam and the
helper that keeps third-party rules honest. A rule is untrusted by default, not
because its author is untrusted but because a rule is the one place an
integration can quietly change what "safe to resume" means: it runs inside
assessment, and its findings are sealed into the contract an operator gates on.
Conformance is what lets a reviewer ask "did you run this?" and get an answer.

The trust boundary is what the seam does not hand over. ``evaluate`` receives
the projected state and the current environment, both immutable pydantic
models, and nothing else. No storage handle, no event writer, no repair plan, no
clock. A rule that cannot reach those cannot mutate storage, emit events, or
rewrite a repair plan during assessment; a rule that needs wall-clock time must
take it as an argument from its own caller, because a rule that reads the clock
is not deterministic for identical inputs and conformance will say so.

Registration is explicit. CONTINUUM never imports or loads a rule from a path,
a plugin directory or an entry point: an integration constructs the rule and
hands it to ``RecoveryEngine(..., validation_rules=[...])`` or registers it in a
:class:`~continuum.plugins.registry.Registry`. Nothing is discovered, because
automatically executing a third party's staleness logic during recovery is a
decision an operator must make on purpose.

What this is not: a policy engine. A rule reports what it can verify from state
it is given. It does not learn from prior runs, does not infer policy from
observed approvals, and does not get more confident over time. Building that
would mean trusting an inductive model with the word "unsafe", which is a
different product.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from continuum.models import (
    Approval,
    ApprovalStatus,
    Component,
    ComponentValidationEntry,
    Decision,
    EnvironmentSnapshot,
    Goal,
    PlanStep,
    PlanStepStatus,
    SemanticState,
    StateStatus,
)

__all__ = [
    "RevokedApprovalRule",
    "ConformanceReport",
    "check_validation_rule",
    "conformance_states",
]


class RevokedApprovalRule:
    """A decision is invalid when the approval that authorized it was revoked.

    The built-in validator already grades the approval itself: a revoked
    approval is an ``invalid`` approval. It does not know what that revocation
    *consequences*, because the link lives in the domain, not in the
    environment. This rule closes that gap: an approval whose subject names a
    decision, finding or plan unit pulls that component to ``invalid`` when the
    approval is revoked.

    Opt-in and off by default; ``RecoveryEngine`` runs no rules unless it is
    given them. Kept small on purpose: it is the worked example of the seam, not
    a policy product, and a rule author should be able to read it in one screen.

    Deterministic by construction: it reads only the state it is handed, ignores
    the environment, and touches no clock.
    """

    name = "builtin:revoked_approval"

    def evaluate(  # noqa: D102 - documented at the seam; satisfies ValidationRule
        self, state: SemanticState, environment: EnvironmentSnapshot | None = None
    ) -> list[ComponentValidationEntry]:
        revoked = [a for a in state.approvals if a.status is ApprovalStatus.REVOKED]
        if not revoked:
            return []

        # Index by subject once; an approval that names several components is
        # checked against each of them, and several approvals naming the same
        # subject report the earliest revocation.
        by_subject: dict[str, Approval] = {}
        for approval in revoked:
            subject = approval.subject.strip()
            if not subject:
                continue
            existing = by_subject.get(subject)
            if existing is None or _approval_earlier(approval, existing):
                by_subject[subject] = approval

        if not by_subject:
            return []

        findings: list[ComponentValidationEntry] = []
        for decision in state.decisions:
            authorization = by_subject.get(decision.decision_id)
            if authorization is not None and decision.status is StateStatus.VALID:
                findings.append(
                    ComponentValidationEntry(
                        component=Component.DECISION,
                        component_id=decision.decision_id,
                        status=StateStatus.INVALID,
                        detail=f"authorizing approval {authorization.approval_id} was revoked",
                        rule=self.name,
                    )
                )
        for finding_ in state.findings:
            authorization = by_subject.get(finding_.finding_id)
            if authorization is not None and finding_.status is StateStatus.VALID:
                findings.append(
                    ComponentValidationEntry(
                        component=Component.FINDING,
                        component_id=finding_.finding_id,
                        status=StateStatus.INVALID,
                        detail=f"authorizing approval {authorization.approval_id} was revoked",
                        rule=self.name,
                    )
                )
        for step in state.plan:
            authorization = by_subject.get(step.step_id)
            # A plan step carries its own lifecycle status, not a StateStatus:
            # only work still outstanding can be blocked by a lost authorization.
            outstanding = step.status is not PlanStepStatus.COMPLETED
            if authorization is not None and outstanding:
                findings.append(
                    ComponentValidationEntry(
                        component=Component.PLAN,
                        component_id=step.step_id,
                        status=StateStatus.INVALID,
                        detail=f"authorizing approval {authorization.approval_id} was revoked",
                        rule=self.name,
                    )
                )
        return findings


def _approval_earlier(a: Approval, b: Approval) -> bool:
    """Whether revocation ``a`` is the earlier of two approvals naming a subject."""
    if a.granted_at is None:
        return False
    if b.granted_at is None:
        return True
    return a.granted_at < b.granted_at


# --------------------------------------------------------------------------- #
# Conformance
# --------------------------------------------------------------------------- #


def conformance_states() -> list[SemanticState]:
    """The states a rule is exercised against by default.

    One empty state (a rule must tolerate a run with nothing in it), one rich
    enough that a domain rule has something to bite on. A rule may behave
    differently on states it authored itself, so a caller with a representative
    state should pass it to :func:`check_validation_rule` as well.
    """
    return [
        SemanticState(run_id="conformance_empty", goal=Goal(description="g")),
        _rich_state(),
    ]


def _rich_state() -> SemanticState:
    """A state with decisions, plan units and one revoked authorization."""
    return SemanticState(
        run_id="conformance_rich",
        goal=Goal(description="ship the report"),
        decisions=[
            Decision(decision_id="decision_authorised", decision="adopt policy P-42"),
            Decision(decision_id="decision_free", decision="adopt policy P-7"),
        ],
        plan=[PlanStep(step_id="step_authorised", description="file the report")],
        approvals=[
            Approval(
                approval_id="approval_1",
                subject="decision_authorised",
                status=ApprovalStatus.REVOKED,
            ),
            Approval(
                approval_id="approval_2",
                subject="step_authorised",
                status=ApprovalStatus.GRANTED,
            ),
        ],
    )


@dataclass(frozen=True)
class ConformanceReport:
    """What :func:`check_validation_rule` found. ``passed`` is False on any failure."""

    rule_name: str | None
    passed: bool
    failures: tuple[str, ...] = ()
    #: One entry per state exercised, so a caller can see which input broke.
    findings: tuple[tuple[SemanticState, list[ComponentValidationEntry]], ...] = field(
        default_factory=tuple
    )


def check_validation_rule(
    rule: object,
    *,
    states: Sequence[SemanticState] | None = None,
    environment: EnvironmentSnapshot | None = None,
) -> ConformanceReport:
    """Check that ``rule`` satisfies the :class:`ValidationRule` contract.

    Run this in your test suite, not in production: it evaluates the rule
    several times against each state, which is wasted work at assessment time
    and is how it can detect nondeterminism at all. ``assert report.passed`` in
    a pytest test, and read ``failures`` when it is not.

    What is checked:

    * **Interface** - a non-empty string ``name``, and an ``evaluate`` returning
      a list of :class:`ComponentValidationEntry`.
    * **Determinism** - two calls with equal inputs return equal findings.
    * **Read-only** - the state and environment handed in are unchanged after
      evaluation. A rule must observe, not edit.
    * **Namespacing** - every entry carries this rule's name, or none at all;
      a rule may not stamp another rule's name on a finding.
    * **Degenerate inputs** - a state with no decisions, findings or approvals
      is handled without raising.

    What is *not* checked, because no checker can: that the rule's verdict is
    *true*. Determinism guarantees the same answer twice, not that the answer is
    right. Review the logic, and review what it reads: a rule that consults a
    live service is a rule whose verdict changes between the assessment and the
    resume.
    """
    states = list(states) if states is not None else conformance_states()
    failures: list[str] = []
    name = getattr(rule, "name", None)
    if not isinstance(name, str) or not name.strip():
        return ConformanceReport(
            rule_name=None,
            passed=False,
            failures=(
                f"rule of type {type(rule).__name__} has no non-empty string 'name' "
                "attribute; the name namespaces every finding the rule reports",
            ),
        )
    if not callable(getattr(rule, "evaluate", None)):
        failures.append(f"rule {name!r} has no callable 'evaluate' method")
        return ConformanceReport(rule_name=name, passed=False, failures=tuple(failures))

    observed: list[tuple[SemanticState, list[ComponentValidationEntry]]] = []

    for state in states:
        before = state.model_dump_json()
        env_before = environment.model_dump_json() if environment is not None else None
        try:
            first = _evaluate_all(rule, state, environment)
        except Exception as exc:  # noqa: BLE001 - the point is to report it
            failures.append(
                f"state {state.run_id!r}: evaluate() raised {type(exc).__name__}: {exc}"
            )
            continue
        observed.append((state, first))

        # Determinism needs a second call with byte-identical inputs; a rule
        # that reads a clock or a counter returns different findings here.
        try:
            second = _evaluate_all(rule, state, environment)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"state {state.run_id!r}: second evaluate() raised {type(exc).__name__}: {exc}"
            )
            continue
        if [e.model_dump() for e in first] != [e.model_dump() for e in second]:
            failures.append(
                f"state {state.run_id!r}: evaluate() is not deterministic for identical "
                "inputs; a rule must not read wall-clock time, counters or external state"
            )

        # Read-only: compare the serialized inputs, not object identity, because
        # a rule that returns an internal alias of a state list would pass an
        # identity check while having mutated the state it was given.
        if state.model_dump_json() != before:
            failures.append(
                f"state {state.run_id!r}: evaluate() mutated the state it was given; "
                "a rule observes state, it does not edit it"
            )
        if environment is not None and env_before != environment.model_dump_json():
            failures.append(
                f"state {state.run_id!r}: evaluate() mutated the environment it was "
                "given; a rule observes the environment, it does not edit it"
            )

        for position, entry in enumerate(first):
            if entry.rule is not None and entry.rule != name:
                failures.append(
                    f"state {state.run_id!r}: entry {position} is labelled "
                    f"{entry.rule!r}, which is not this rule's name {name!r}; a rule may "
                    "not report findings in another rule's namespace"
                )

    return ConformanceReport(
        rule_name=name,
        passed=not failures,
        failures=tuple(failures),
        findings=tuple(observed),
    )


def _evaluate_all(
    rule: object, state: SemanticState, environment: EnvironmentSnapshot | None
) -> list[ComponentValidationEntry]:
    raw = rule.evaluate(state, environment)  # type: ignore[attr-defined]
    if not isinstance(raw, list):
        raise TypeError(f"evaluate() returned {type(raw).__name__}, expected a list")
    for position, entry in enumerate(raw):
        if not isinstance(entry, ComponentValidationEntry):
            raise TypeError(
                f"evaluate() item {position} is {type(entry).__name__}, expected "
                "ComponentValidationEntry"
            )
    return raw
