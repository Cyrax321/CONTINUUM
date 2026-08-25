"""Actionable recovery guidance: turn a decision into executable steps.

The contract names what is blocked (``reconcile_action:action_abc``,
``human_review:goal``) but not what to *do* about it, which left every
resuming agent or human translating statuses into commands by hand. This
module renders the translation once, in one place:

- Reconcile steps become either "run ``continuum reconcile``" when a probe is
  registered for that action type (#218), or the exact external check plus
  the exact ``continuum_reconcile_action`` call when not.
- Human-review steps become the concrete verification plus
  ``continuum confirm``.
- Dependency/evidence/finding steps point at ``validate --env`` re-pinning.

Guidance is derived from state that already exists (the plan, the uncertain
actions, the reconciler registry, the gate config). It adds no authority:
following it still lands as ordinary auditable events, and steps marked
``requires_human`` stay human-gated regardless of wording.
"""

from __future__ import annotations

from collections.abc import Iterable

from continuum.models import Component, RecoveryMode, StateStatus
from continuum.recovery.engine import RecoveryDecision
from continuum.recovery.planner import RepairKind

__all__ = ["human_steps_for", "self_report_guidance"]

_RECONCILE_HINT = (
    "call continuum_reconcile_action(run_id={run}, action_key={key}, occurred=true|false)"
)


def self_report_guidance(decision: RecoveryDecision) -> dict[str, str]:
    """Explain a ``request_human`` that is only about unverified self-reports.

    A run driven by an agent (over MCP or the sidecar) is self-certified, so its
    goal and progress validate as REQUIRES_REVIEW and the mode becomes
    ``request_human`` even when nothing is wrong. That is deliberate:
    self-reported progress decides what work a resumed session skips, so it must
    never read as verified. But the bare status looks like a hard stop, and the
    obvious next move -- calling ``continuum_confirm`` -- is refused by design,
    which strands the caller with no legal way forward.

    So when self-report is the *only* thing blocking, say what it means and what
    is still permitted. When anything else is also wrong (a conflicted
    dependency, an unresolved action) the caller has a real problem to fix and
    this note would only dilute it, so it is omitted.

    "Anything else" has to include the ledger, not just the validation report
    (issue #369). An uncertain action reaches ``request_human`` through
    ``decision.uncertain_actions``, never through ``report.statuses``, so scanning
    only the report found nothing but goal and progress and emitted this note
    beside a contract that said ``recovery_status: requires_human`` because a side
    effect's outcome was unknown. The agent was told "Nothing is wrong with this
    run" and "Work is not blocked" and pointed past the one thing this system
    exists to stop it walking past.

    Returns a mapping to splice into a payload, empty when not applicable, so a
    caller adds the key only when there is something to say.
    """
    if decision.mode is not RecoveryMode.REQUEST_HUMAN:
        return {}
    if decision.uncertain_actions:
        return {}
    blocking = [e for e in decision.validation.report.statuses if e.status is not StateStatus.VALID]
    only_self_report = bool(blocking) and all(
        e.component in (Component.GOAL, Component.PROGRESS)
        and e.status is StateStatus.REQUIRES_REVIEW
        for e in blocking
    )
    if not only_self_report:
        return {}
    return {
        "self_report_guidance": (
            "Nothing is wrong with this run. request_human here means only that its "
            "goal and progress were reported by an agent and nothing independent "
            "corroborates them. Work is not blocked: recording progress, "
            "checkpointing and the action tools all still function. Do not call "
            "continuum_confirm, which is refused over MCP because an agent must not "
            "vouch for its own claims; only a human running "
            f"'continuum confirm {decision.run_id}' clears it. Say that once and "
            "carry on. Before skipping units the counter calls complete, check the "
            "work actually happened rather than trusting the number."
        )
    }


def human_steps_for(
    decision: RecoveryDecision,
    *,
    run_id: str,
    probed_types: Iterable[str] = (),
    gate_configured: bool = False,
    reconcilers_path: str = ".continuum/reconcilers.json",
    gate_path: str = ".continuum/gate.json",
) -> list[str]:
    """Executable next steps for this decision, most urgent first."""
    if decision.mode is RecoveryMode.RESUME and not decision.plan.steps:
        return []

    probed = set(probed_types)
    uncertain_by_id = {a.action_id: a for a in decision.uncertain_actions}
    steps: list[str] = []

    for step in decision.plan.steps:
        if step.kind is RepairKind.RECONCILE_ACTION:
            action_id = step.target
            action = uncertain_by_id.get(action_id)
            action_type = action.action_type if action else "unknown"
            key_hint = ""
            if action is not None and action.external_id:
                key_hint = f" for {action.external_id}"
            if action_type in probed:
                steps.append(
                    f"a probe is registered for {action_type!r}: "
                    f"run `continuum reconcile {run_id}` to settle it automatically"
                )
            else:
                steps.append(
                    f"check whether {action_type!r}{key_hint} actually reached the outside "
                    f"world, then {_RECONCILE_HINT.format(run=run_id, key=action_id)}"
                    + (f"  (no probe registered in {reconcilers_path})")
                )
        elif step.kind is RepairKind.HUMAN_REVIEW:
            steps.append(f"verify {step.target} yourself, then run `continuum confirm {run_id}`")
        elif step.kind is RepairKind.REPAIR_LOG:
            # No automated surface can settle a refused event yet (that is the
            # repair/amend command, deliberately unbuilt), so name where to
            # look and who must decide rather than implying a command exists.
            steps.append(
                f"the log stops folding at {step.target}: run `continuum verify {run_id}` "
                f"for the offending event; settling it needs an operator decision, "
                f"no amend surface exists yet"
            )
        elif step.kind is RepairKind.REVALIDATE_DEPENDENCY:
            steps.append(
                f"re-pin dependency {step.target!r}: rerun with "
                f"`--env {step.target}=<current version>` after confirming its real version"
            )
        elif step.kind in (
            RepairKind.REDERIVE_EVIDENCE,
            RepairKind.REDERIVE_FINDING,
        ):
            steps.append(f"re-derive {step.target} from its source, then re-validate")
        elif step.kind is RepairKind.RENEW_APPROVAL:
            steps.append(f"obtain a fresh approval for {step.target}, then record it via MCP")
        elif step.kind is RepairKind.REVALIDATE_MODEL_STATE:
            steps.append("confirm which model resumes this work, then pass `--model <name>`")

    if not steps and decision.mode is not RecoveryMode.RESUME:
        steps.append("inspect the contract above; nothing further is automatable here")

    if gate_configured:
        steps.append(
            f"gate active ({gate_path}): route new side effects through "
            f"continuum_intercept_action before performing them"
        )
    return steps
