"""Running registered domain validation rules and merging what they report (#761).

Built-in validation asks one question: does the state still match the
environment it was checked against? Domains have staleness that question cannot
reach. A decision is invalid when the regulation it cites is revoked, a finding
is void when its lab retracted the assay, a plan step is moot when the ticket it
implements was closed as wontfix. None of that is an environment change, so
:mod:`continuum.state.validator` cannot see it, and the only way to contribute
it used to be patching the validator.

This module is the consumer for the :class:`~continuum.plugins.ValidationRule`
seam. It is deliberately narrow:

* Rules are *read-only observers*. They receive the projected state and the
  current environment, both immutable, and nothing else: no storage, no event
  writer, no handle on the repair plan. A rule that cannot reach those things
  cannot mutate storage, emit events, or rewrite the plan during assessment,
  which is the whole trust boundary. The seam is enforced by what it does not
  pass.
* Rules can only *raise* caution. Merge takes the most cautious status per
  component, so a rule may turn a VALID component INVALID but can never relax a
  finding built-in validation or another rule already made.
* A rule that misbehaves fails closed. Crashing, malformed, duplicate-named or
  unlabelled rules become an explicit ``validation_rule`` entry asking for
  review, never a silent gap in the report.

Assessment stays deterministic because rule order does not matter: max-merge is
commutative and associative, so the same rules over the same state produce the
same report in any order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from continuum.models import (
    Component,
    ComponentValidationEntry,
    EnvironmentSnapshot,
    SemanticState,
    StateStatus,
    StateValidationResult,
)
from continuum.state.validator import (
    ValidationOutcome,
    _is_blocking,
    blocking_reason,
)

__all__ = [
    "STATUS_CAUTION",
    "run_validation_rules",
    "merge_validation_entries",
    "apply_rule_findings",
]


#: Ascending caution for the statuses a component can carry. Only the ordering
#: of *different* statuses matters: merge takes the maximum, so a rule can
#: raise a component's status but never lower it. VALID is the minimum by
#: construction, which is the property that makes a rule unable to launder a
#: finding another signal produced.
#:
#: The middle of the ordering is a judgement call the docs state openly: there
#: is no fact of the matter about whether UNKNOWN is worse than STALE. Both
#: withhold resume, and the ordering only decides which word the report uses
#: when two signals disagree about one component.
STATUS_CAUTION: dict[StateStatus, int] = {
    StateStatus.VALID: 0,
    StateStatus.STALE: 1,
    StateStatus.EXPIRED: 2,
    StateStatus.CONFLICTED: 3,
    StateStatus.UNKNOWN: 4,
    StateStatus.REQUIRES_REVIEW: 5,
    StateStatus.INVALID: 6,
}


_EntryKey = tuple[str, str | None]


def _key(entry: ComponentValidationEntry) -> _EntryKey:
    """Identity of the component an entry describes.

    Two entries about the same component are one finding with competing
    statuses; two entries about different components are two findings, however
    similar their detail.
    """
    return entry.component.value, entry.component_id


def _more_cautionous(
    incumbent: ComponentValidationEntry, candidate: ComponentValidationEntry
) -> ComponentValidationEntry:
    """The entry with the more cautious status, breaking ties toward the incumbent.

    A tie keeps the entry that was already in the report, which is the built-in
    one when a rule merely agrees with it. That keeps built-in prose in front of
    a reader when a rule adds nothing, and keeps the report stable when two
    rules of equal caution disagree about wording.
    """
    if STATUS_CAUTION[candidate.status] > STATUS_CAUTION[incumbent.status]:
        return candidate
    return incumbent


def _fail_closed(name: str | None, detail: str) -> ComponentValidationEntry:
    """The entry a misbehaving rule becomes.

    REQUIRES_REVIEW rather than UNKNOWN: a rule that raised or returned garbage
    is a defect a person must look at, not a fact that is merely unverifiable.
    The planner maps ``validation_rule`` to a human step, so a broken rule
    escalates instead of vanishing.
    """
    return ComponentValidationEntry(
        component=Component.VALIDATION_RULE,
        component_id=name,
        status=StateStatus.REQUIRES_REVIEW,
        detail=detail,
        rule=name,
    )


def run_validation_rules(
    rules: Sequence[object],
    state: SemanticState,
    environment: EnvironmentSnapshot | None = None,
) -> list[ComponentValidationEntry]:
    """Execute ``rules`` and return their findings, fail-closed.

    Each rule is any object satisfying the :class:`~continuum.plugins.ValidationRule`
    protocol; it is duck-typed here rather than isinstance-checked so a rule
    written against the documented protocol works without importing the
    protocol itself. The engine rejects non-conforming rules before reaching
    here, and this layer still defends itself: nothing a rule does wrong is
    allowed to shorten the report.

    ``state`` and ``environment`` are immutable, so a rule cannot mutate the
    state it is given (it can only construct a new one, which this layer never
    reads back). That is the read-only half of the trust boundary; the other
    half is that no storage or event handle is ever in scope.
    """
    findings: list[ComponentValidationEntry] = []
    seen_names: set[str] = set()

    for rule in rules:
        name = getattr(rule, "name", None)
        if not isinstance(name, str) or not name.strip():
            findings.append(
                _fail_closed(
                    None,
                    f"rule of type {type(rule).__name__} has no non-empty string 'name' "
                    "attribute; a rule must be namespaced by a stable identifier",
                )
            )
            continue
        if name in seen_names:
            # Two rules sharing a name cannot both be namespaced by it, so the
            # collision is reported rather than silently deduplicated: picking
            # one would attribute the other's findings to the wrong rule, and
            # an auditor reading "rule X failed" has to know which X.
            findings.append(_fail_closed(name, f"rule name {name!r} is registered more than once"))
            continue
        seen_names.add(name)

        try:
            raw = rule.evaluate(state, environment)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - a broken rule must not break assessment
            findings.append(_fail_closed(name, f"evaluate() raised {type(exc).__name__}: {exc}"))
            continue

        if not isinstance(raw, list):
            findings.append(
                _fail_closed(
                    name,
                    f"evaluate() returned {type(raw).__name__}, expected a list of "
                    "ComponentValidationEntry",
                )
            )
            continue

        for position, entry in enumerate(raw):
            if not isinstance(entry, ComponentValidationEntry):
                findings.append(
                    _fail_closed(
                        name,
                        f"evaluate() item {position} is {type(entry).__name__}, expected "
                        "ComponentValidationEntry",
                    )
                )
                continue
            # Stamp the provenance the entry carries. A rule may set it itself;
            # it may not set another rule's name, which would let one rule
            # speak in another's namespace. Overwriting anything that is not
            # this rule's own name closes that without rejecting the entry.
            if entry.rule != name:
                entry = entry.model_copy(update={"rule": name})
            findings.append(entry)

    return findings


def merge_validation_entries(
    entries: Sequence[ComponentValidationEntry],
    findings: Sequence[ComponentValidationEntry],
) -> list[ComponentValidationEntry]:
    """Merge built-in ``entries`` with rule ``findings``, most cautious per component.

    The result keeps built-in order and appends components only rules examined,
    so a run with no rules yields a list equal to the input and the report does
    not move. Statuses are combined by maximum caution: a rule may raise a
    component's status, never lower one, and never lower what another rule
    raised. Order-independent, because either side may report several findings
    for one component and the maximum does not care which it sees first.
    """
    merged: dict[_EntryKey, ComponentValidationEntry] = {}
    order: list[_EntryKey] = []

    def place(entry: ComponentValidationEntry) -> None:
        key = _key(entry)
        incumbent = merged.get(key)
        if incumbent is None:
            merged[key] = entry
            order.append(key)
        else:
            merged[key] = _more_cautionous(incumbent, entry)

    for entry in entries:
        place(entry)
    for entry in findings:
        place(entry)

    return [merged[key] for key in order]


def apply_rule_findings(
    validation: ValidationOutcome,
    findings: Sequence[ComponentValidationEntry],
    *,
    strict_unknown: bool,
) -> ValidationOutcome:
    """Return ``validation`` with rule ``findings`` merged into its report.

    Rebuilds ``safe_to_resume`` and ``reason`` from the merged statuses, so a
    rule finding withholds resume exactly as a built-in finding of the same
    status would. Returns the input unchanged when the findings add nothing,
    which is how "no rules configured" keeps byte-identical default behaviour.
    """
    if not findings:
        return validation

    merged = merge_validation_entries(validation.report.statuses, findings)
    if [e.model_dump() for e in merged] == [e.model_dump() for e in validation.report.statuses]:
        return validation

    # The blocking predicate is this layer's own words, imported rather than
    # re-derived, so a rule-driven downgrade means the same thing as a built-in
    # one for resume and for the report's reason sentence.
    blocking = [e for e in merged if _is_blocking(e.status, strict_unknown=strict_unknown)]
    report = StateValidationResult(
        run_id=validation.report.run_id,
        checkpoint_version=validation.report.checkpoint_version,
        statuses=merged,
        safe_to_resume=not blocking,
        reason=blocking_reason(blocking),
        validated_at=validation.report.validated_at,
    )
    return ValidationOutcome(
        state=validation.state,
        report=report,
        environment_diff=validation.environment_diff,
    )


def active_rules(*collections: Iterable[object] | None) -> tuple[object, ...]:
    """Flatten the rule collections an assessment was offered into one sequence.

    Engine-level and per-call rules add together rather than the per-call set
    replacing the engine's: a caller asking "and also this one" on top of a
    configured set should not have to re-list the configured ones.
    """
    return tuple(rule for collection in collections if collection for rule in collection)
