"""The recovery contract.

A contract is the machine-readable answer to "what am I allowed to do now?".
It names what was verified, what was invalidated, what must happen before
normal work resumes, and (critically) the *single* next permitted action.

One action, not a set. If a contract listed everything currently allowed, an
agent could pick the convenient one and skip reconciling the side effect it was
supposed to resolve first. Naming exactly one step makes the gate enforceable
and the ordering meaningful.

Contracts are deterministic: the same state, environment and ledger always
produce a byte-identical contract. That is what makes them auditable, diffable
and safe to compare in tests. They are sealed with an integrity hash for the
same reason checkpoints are: a contract that could be edited between issue and
enforcement would gate nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from continuum.models import (
    CONTRACT_VERSION,
    Component,
    RecoveryContract,
    RecoverySafety,
    StateStatus,
    StateValidationResult,
    utcnow,
)
from continuum.recovery.planner import RepairPlan
from continuum.security.hashing import hash_content, to_json
from continuum.state.validator import ValidationOutcome

__all__ = [
    "CONTRACT_DIGEST_FIELDS",
    "CONTRACT_VERSIONS",
    "CURRENT_CONTRACT_VERSION",
    "ContractVersionSpec",
    "ContractVerification",
    "SUPPORTED_CONTRACT_VERSIONS",
    "build_contract",
    "canonical_digest_input",
    "contract_digest",
    "render_contract",
    "seal_contract",
    "verify_contract",
    "verify_contract_detailed",
]


# --------------------------------------------------------------------------- #
# Compatibility versions (issue #764)
# --------------------------------------------------------------------------- #
# The contract's integrity hash covers a *published* field set, not "whatever
# the model happens to define today". That distinction is what makes a contract
# sealed by one build verifiable by another, including one that does not yet
# know a field the producer added.
#
# Each version pins the digest input exactly: which fields are covered, and
# which are deliberately excluded. A verifier that recognizes the version
# recomputes the digest over exactly that set. One that does not recognizes
# the version as unknown and fails closed (see ``verify_contract_detailed``)
# rather than hashing whatever it happens to have and silently disagreeing.
#
# Adding an optional, non-hash-covered field is *not* a version bump: an old
# verifier ignores it and the digest is unchanged. A version bumps only when a
# field's coverage changes -- it newly enters or leaves the digest, or its
# canonical serialization changes shape.


@dataclass(frozen=True)
class ContractVersionSpec:
    """The published digest rules for one contract compatibility version.

    ``covered`` and ``excluded`` partition the contract's fields. ``excluded``
    is the explicit, auditable record of what the hash *does not* protect, so
    a reader never has to infer it from the absence of a name.
    """

    version: int
    covered: frozenset[str]
    excluded: frozenset[str]
    #: Why this version exists, and what a verifier must know about it.
    note: str = ""

    @property
    def digest_fields(self) -> frozenset[str]:
        """The covered field names, sorted-collapsed for a stable digest input."""
        return self.covered


#: Fields no version ever covers. These are bookkeeping, not terms: the hash
#: itself, and ``created_at``, which is wall-clock metadata an identical
#: re-assessment would otherwise change (see ``canonical_digest_input``).
_ALWAYS_EXCLUDED: frozenset[str] = frozenset({"integrity_hash", "created_at", "contract_version"})

#: Fields every version covers. These are the contract's terms, and they have
#: been covered since the first sealed contract.
_BASE_COVERED: frozenset[str] = frozenset(
    {
        "run_id",
        "checkpoint_version",
        "recovery_status",
        "verified",
        "invalidated",
        "required_actions",
        "next_allowed_action",
        "post_checkpoint_observations",
        "liveness",
        "triggering_risks",
    }
)

#: Version 0: the contract as it existed before Phase 1's explanatory fields.
#: ``evidence`` and ``reason`` did not exist, so the digest covers nothing
#: under them. A v0 contract has no ``contract_version`` key on the wire at
#: all; it loads as v0 via the field default, and verifies against these rules.
_V0_COVERED: frozenset[str] = _BASE_COVERED

#: Version 1: the current contract. ``evidence`` and ``reason`` entered the
#: digest (Phase 1), so a contract sealed under either version can be told
#: apart by the field set its hash covers rather than by guessing.
_V1_COVERED: frozenset[str] = _BASE_COVERED | frozenset({"evidence", "reason"})

CONTRACT_VERSIONS: tuple[ContractVersionSpec, ...] = (
    ContractVersionSpec(
        version=0,
        covered=_V0_COVERED,
        excluded=_ALWAYS_EXCLUDED | frozenset({"evidence", "reason"}),
        note=(
            "The pre-Phase-1 contract: no evidence or reason fields, so the "
            "digest covers nothing under them. This is the version a legacy "
            "payload carries implicitly."
        ),
    ),
    ContractVersionSpec(
        version=1,
        covered=_V1_COVERED,
        excluded=_ALWAYS_EXCLUDED,
        note=(
            "The current contract: evidence and reason are hash-covered. "
            "Liveness carries one wall-clock reading (last_append_age) that is "
            "stripped from the digest input because two assessments of an "
            "unchanged run would otherwise seal different hashes."
        ),
    ),
)


def _spec_for_version(version: int) -> ContractVersionSpec:
    """The published spec for ``version``.

    Raises ``KeyError`` for an unknown version: this is a programming error by
    a caller that had a version string it never validated, and a silent
    fallback here would hash an invented field set.
    """
    for spec in CONTRACT_VERSIONS:
        if spec.version == version:
            return spec
    raise KeyError(
        f"unknown contract version {version}; supported: {sorted(SUPPORTED_CONTRACT_VERSIONS)}"
    )


#: The newest version this build can seal and verify.
CURRENT_CONTRACT_VERSION: int = CONTRACT_VERSION

#: All versions this build recognizes, whether or not it still seals them.
SUPPORTED_CONTRACT_VERSIONS: frozenset[int] = frozenset(spec.version for spec in CONTRACT_VERSIONS)

#: The digest fields of the version this build seals, for callers that want the
#: current rules without naming a version.
CONTRACT_DIGEST_FIELDS: frozenset[str] = _spec_for_version(CURRENT_CONTRACT_VERSION).covered


def _identifier(component: Component, component_id: str | None) -> str:
    return f"{component.value}:{component_id}" if component_id else component.value


def _liveness_digest_value(liveness: Any) -> Any:
    """Strip the one wall-clock reading ``liveness`` carries.

    ``last_append_age`` is seconds since the last append at assessment time, so
    two assessments of an unchanged run would seal different hashes without
    this. The verdict fields (``breached``, ``threshold_seconds``, ``phase``,
    ``breaches``) stay covered.
    """
    if isinstance(liveness, dict):
        return {k: v for k, v in liveness.items() if k != "last_append_age"}
    return liveness


def canonical_digest_input(contract: RecoveryContract, version: int) -> str:
    """The canonical bytes the integrity hash covers, for one version.

    This is the published digest input an external verifier reproduces: canonical
    JSON (sorted keys, no insignificant whitespace, ASCII) over exactly the
    version's covered fields. It never depends on model field order or on the
    set of fields a later build happens to add.

    The digest is ``sha256`` over these bytes as UTF-8. Hashing the *string*
    rather than re-parsing it is deliberate: an independent verifier must
    reproduce the digest from the published bytes alone, and re-canonicalizing
    an already-canonical string would double-encode it.
    """
    spec = _spec_for_version(version)
    dumped = contract.model_dump(mode="json")
    payload: dict[str, Any] = {
        name: value for name, value in dumped.items() if name in spec.covered
    }
    if "liveness" in payload:
        payload["liveness"] = _liveness_digest_value(payload["liveness"])
    return to_json(payload)


def contract_digest(contract: RecoveryContract, version: int) -> str:
    """The integrity digest of ``contract`` under one version's published rules.

    ``sha256`` over the canonical digest input bytes. This is the single place
    the digest is computed, so sealing and verifying cannot drift apart.
    """
    return hash_content(canonical_digest_input(contract, version).encode("utf-8"))


@dataclass(frozen=True)
class ContractVerification:
    """The outcome of verifying a contract, with the reason a verifier needs.

    ``verify_contract`` keeps its boolean contract; this is the surface a
    diagnostic caller (CLI, dashboard, conformance suite) wants, because
    "does not verify" alone cannot tell a sealed-before-evidence legacy
    contract from a tampered one.
    """

    verified: bool
    #: The version whose digest rules were applied, when known.
    version: int | None = None
    #: The versions actually tried, in order, for an audit trail.
    tried_versions: tuple[int, ...] = field(default_factory=tuple)
    #: Human-readable, actionable reason for the outcome.
    reason: str = ""


def _verify_against_version(contract: RecoveryContract, version: int) -> str | None:
    """The digest of ``contract`` under ``version``'s rules, or None.

    Returns None rather than raising when the contract cannot be hashed under
    these rules: the caller treats that as "this version does not apply", which
    is a normal compatibility outcome, not an error.
    """
    digest = contract_digest(contract, version)
    return digest if contract.integrity_hash == digest else None


def verify_contract_detailed(contract: RecoveryContract) -> ContractVerification:
    """Verify a contract and report which version's rules accepted it.

    Compatibility policy, applied in order:

    * An unknown ``contract_version`` fails closed. A verifier that does not
      know a version cannot know what its hash covers, so it must not guess;
      it reports the unsupported version and the ones it does know.
    * Otherwise the contract's own version is tried first, then the older
      versions whose digest rules still accept it. That fallback is the
      documented path for a legacy contract that predates a covered field:
      its stored hash was computed without it, so the current-version digest
      cannot match, but the legacy one does.
    * A contract with no integrity hash never verifies, for any version.
    """
    if contract.integrity_hash is None:
        return ContractVerification(
            verified=False,
            version=contract.contract_version,
            reason="no integrity hash: the contract was never sealed",
        )

    declared = contract.contract_version
    if declared not in SUPPORTED_CONTRACT_VERSIONS:
        return ContractVerification(
            verified=False,
            version=declared,
            reason=(
                f"unsupported contract version {declared}: this build verifies "
                f"{sorted(SUPPORTED_CONTRACT_VERSIONS)}, and will not guess what "
                "a newer version's hash covers"
            ),
        )

    # The declared version first: it is what the producer says it sealed under.
    # Then older versions in descending order, which is the only direction a
    # compatibility path can run -- a newer version's digest covers fields the
    # stored hash cannot, so it can never match a contract sealed beneath it.
    order = [declared, *(v for v in sorted(SUPPORTED_CONTRACT_VERSIONS) if v < declared)]
    for version in order:
        if _verify_against_version(contract, version) is not None:
            return ContractVerification(
                verified=True,
                version=version,
                tried_versions=tuple(order),
                reason=(
                    "verified against the contract's declared version"
                    if version == declared
                    else f"verified against legacy version {version} (declared {declared})"
                ),
            )
    return ContractVerification(
        verified=False,
        version=declared,
        tried_versions=tuple(order),
        reason=(
            f"integrity hash does not match any supported digest (tried versions "
            f"{order}); the contract's terms changed after sealing, or it was "
            "sealed by a build whose field coverage this one does not recognize"
        ),
    )


def seal_contract(contract: RecoveryContract) -> RecoveryContract:
    """Attach an integrity hash covering the contract's published terms."""
    return contract.model_copy(
        update={
            "integrity_hash": contract_digest(contract, CURRENT_CONTRACT_VERSION),
            "contract_version": CURRENT_CONTRACT_VERSION,
        }
    )


def verify_contract(contract: RecoveryContract) -> bool:
    """Whether a contract still matches the terms it was sealed with.

    Boolean wrapper over ``verify_contract_detailed`` for callers that only
    need the verdict. Use the detailed form for diagnostics.
    """
    return verify_contract_detailed(contract).verified


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
    liveness: dict[str, object] | None = None,
    triggering_risks: list[str] | None = None,
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

    # A repair step is only "the next allowed action" under a mode that
    # permits repair. A risk-driven ROLLBACK or ABORT can coexist with a
    # non-empty plan, and advertising the plan's first step there would hand
    # any caller gating on permits() a green light on a run the engine has
    # declared must not proceed (issue #1058). required_actions still lists
    # the work for an auditor; nothing is permitted until the mode changes.
    if safety in (RecoverySafety.BLOCKED, RecoverySafety.UNSAFE):
        next_action = None
    else:
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
        contract_version=CURRENT_CONTRACT_VERSION,
        recovery_status=safety,
        verified=sorted(verified),
        invalidated=sorted(invalidated),
        required_actions=[step.action_name for step in plan.steps],
        next_allowed_action=next_action,
        evidence=evidence,
        reason=reason,
        post_checkpoint_observations=post_checkpoint_observations or [],
        liveness=liveness,
        triggering_risks=triggering_risks or [],
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
