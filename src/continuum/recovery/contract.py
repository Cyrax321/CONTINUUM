"""The recovery contract.

A contract is the machine-readable answer to "what am I allowed to do now?".
It names what was verified, what was invalidated, what must happen before
normal work resumes, and — critically — the *single* next permitted action.

One action, not a set. If a contract listed everything currently allowed, an
agent could pick the convenient one and skip reconciling the side effect it was
supposed to resolve first. Naming exactly one step makes the gate enforceable
and the ordering meaningful.

Contracts are deterministic: the same state, environment and ledger always
produce a byte-identical contract. That is what makes them auditable, diffable
and safe to compare in tests. They are sealed with an integrity hash for the
same reason checkpoints are — a contract that could be edited between issue and
enforcement would gate nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from continuum.models import (
    Component,
    RecoveryContract,
    RecoverySafety,
    StateStatus,
    StateValidationResult,
    utcnow,
)
from continuum.recovery.planner import RepairPlan
from continuum.security.hashing import stable_hash
from continuum.state.validator import ValidationOutcome

__all__ = ["build_contract", "seal_contract", "verify_contract"]


def _identifier(component: Component, component_id: str | None) -> str:
    return f"{component.value}:{component_id}" if component_id else component.value


def seal_contract(contract: RecoveryContract) -> RecoveryContract:
    """Attach an integrity hash covering the contract's terms."""
    payload = contract.model_dump(mode="json", exclude={"integrity_hash", "created_at"})
    return contract.model_copy(update={"integrity_hash": stable_hash(payload)})


def verify_contract(contract: RecoveryContract) -> bool:
    """Whether a contract still matches the terms it was sealed with.

    Two digests are accepted so contracts sealed *before* ``evidence``/``reason``
    existed still verify: their stored hash was computed over the terms without
    those fields, so we also try the legacy payload that excludes them.
    """
    if contract.integrity_hash is None:
        return False
    payload = contract.model_dump(mode="json", exclude={"integrity_hash", "created_at"})
    if contract.integrity_hash == stable_hash(payload):
        return True
    legacy = contract.model_dump(
        mode="json", exclude={"integrity_hash", "created_at", "evidence", "reason"}
    )
    return contract.integrity_hash == stable_hash(legacy)


def build_contract(
    *,
    run_id: str,
    checkpoint_version: int,
    safety: RecoverySafety,
    validation: ValidationOutcome,
    plan: RepairPlan,
    reason: str | None = None,
    evidence: list[str] | None = None,
    scope: Iterable[str] | None = None,
    post_checkpoint_observations: list[dict[str, Any]] | None = None,
    admissibility: Any | None = None,
) -> RecoveryContract:
    """Assemble a sealed, deterministic contract.

    ``verified`` and ``invalidated`` are sorted so two runs over equivalent
    state produce identical contracts regardless of dictionary iteration order.

    ``reason`` and ``evidence`` are threaded from information the engine and
    validator already produced; they are never invented. ``reason`` defaults to
    the validation report's reason when the caller supplies none, and
    ``evidence`` defaults to the validator's per-component details (the existing
    provenance/validation evidence), so a contract is always self-explaining.

    When ``scope`` names specific dependency resources, the contract records that
    the recovery was localized to them, so an auditor can see at a glance that
    clean parts of the state were intentionally preserved.
    """
    verified: list[str] = []
    invalidated: list[str] = []

    # A degraded fold (issue #383) changes what "verified" can claim: those
    # components were checked against the last-good prefix only, so an
    # unqualified list would assert an assurance the run cannot support, and a
    # machine keying on verified/invalidated would read the contract as clean
    # over a log that stops folding. Qualify every entry and record the break.
    state = validation.state
    projection_broken = (
        state.status is StateStatus.INVALID and state.unprojectable_at_sequence is not None
    )
    for entry in validation.report.statuses:
        name = _identifier(entry.component, entry.component_id)
        if entry.status is StateStatus.VALID:
            if projection_broken:
                name = f"{name} (through sequence {state.source_sequence})"
            verified.append(name)
        else:
            invalidated.append(f"{name} ({entry.status.value})")
    if admissibility is not None and not admissibility.admissible:
        for d in admissibility.details:
            invalidated.append(
                f"action:{d['action_id']} at position {d['chain_position']} ({d['reason']})"
            )
    if projection_broken:
        invalidated.append(
            f"projection (invalid: log stops folding at sequence {state.unprojectable_at_sequence})"
        )

    next_action = plan.first.action_name if plan.first else None

    if reason is None:
        reason = validation.report.reason
    if evidence is None:
        evidence = _validation_evidence(validation.report)
    if admissibility is not None and not admissibility.admissible:
        for d in admissibility.details:
            evidence.append(
                f"blocking commitment: action {d['action_id']} at position {d['chain_position']} type {d['action_type']} consumed {d['consumed_inputs']} reason {d['reason']}"
            )
        evidence = sorted(set(evidence))
    if projection_broken:
        # The validation details describe the prefix and cannot name the break;
        # without this the contract's evidence would read as a complete audit.
        evidence = [
            *evidence,
            f"projection stopped at sequence {state.unprojectable_at_sequence} "
            f"({state.unprojectable_event_type}): {state.unprojectable_reason}",
        ]
    if scope is not None:
        named = sorted(set(scope))
        if named:
            evidence = [
                *evidence,
                f"localized recovery scoped to: {', '.join(named)}",
            ]

    contract = RecoveryContract(
        run_id=run_id,
        checkpoint_version=checkpoint_version,
        recovery_status=safety,
        verified=sorted(verified),
        invalidated=sorted(invalidated),
        required_actions=[step.action_name for step in plan.steps],
        next_allowed_action=next_action,
        evidence=evidence,
        reason=reason,
        post_checkpoint_observations=post_checkpoint_observations or [],
        created_at=utcnow(),
    )
    return seal_contract(contract)


def _validation_evidence(report: StateValidationResult) -> list[str]:
    """Existing validation evidence, as human-readable strings.

    These are exactly the per-component details the validator already produced;
    nothing here is fabricated. Sorted so the contract stays deterministic.
    """
    return sorted(
        f"{e.component.value}{f':{e.component_id}' if e.component_id else ''}: {e.detail}"
        for e in report.statuses
        if e.detail
    )


def render_contract(contract: RecoveryContract) -> str:
    """Human-readable rendering of a contract."""
    lines = [
        f"run_id:            {contract.run_id}",
        f"checkpoint:        v{contract.checkpoint_version}",
        f"recovery_status:   {contract.recovery_status.value}",
    ]
    if contract.verified:
        lines.append(f"verified:          {', '.join(contract.verified)}")
    if contract.invalidated:
        lines.append(f"invalidated:       {', '.join(contract.invalidated)}")
    if contract.required_actions:
        lines.append("required_actions:")
        lines += [f"  - {a}" for a in contract.required_actions]
    # "continue" is only honest when resuming is actually permitted. A
    # requires_human contract with no next step must not render permission
    # into prose the gate never issued (issue #385 review).
    fallback = (
        "continue"
        if contract.recovery_status is RecoverySafety.SAFE_TO_RESUME
        else "none (settle required_actions first)"
    )
    lines.append(f"next_allowed:      {contract.next_allowed_action or fallback}")
    if contract.reason:
        lines.append(f"reason:            {contract.reason}")
    if contract.evidence:
        lines.append("evidence:")
        lines += [f"  - {e}" for e in contract.evidence]
    if contract.post_checkpoint_observations:
        lines.append("files changed since last checkpoint:")
        for entry in contract.post_checkpoint_observations:
            if entry.get("truncated"):
                lines.append(f"  ... {entry['omitted']} earlier observation(s) omitted")
            else:
                lines.append(
                    f"  [{entry['status']}] {entry['path']} ({entry['tool']}, seq {entry['sequence']})"
                )
    return "\n".join(lines)
