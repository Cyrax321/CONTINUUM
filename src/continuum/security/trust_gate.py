"""Trust gate for the secure planning loop (Extension 1).

Combines a planner-emitted :class:`PlanBranch` with a perception
:class:`ObservationProvenance`. A high-risk branch resolved by anything other
than a ``verified`` observation, or any ``environment_observed`` claim that is
``contested``, is routed to ``REQUIRES_REVIEW`` and must not execute. Low-risk
or ``verified`` branches proceed.

This is additive: it is a second trigger alongside the existing
``Origin.EXTERNAL_AGENT`` review path, not a replacement. See docs/PROBLEM.md.
"""

from __future__ import annotations

from continuum.events import Event, EventType
from continuum.models import Origin
from continuum.security.hashing import make_id
from continuum.security.provenance import ObservationProvenance, PlanBranch

__all__ = [
    "ReviewGate",
    "verify_observation",
    "resolve_branch",
    "record_observation",
]


def _default_dom_consistency(claim: str, dom_snapshot: str) -> bool:
    """Best-effort check: does the claim's text appear in the DOM/accessibility tree?

    A real deployment would parse the tree and compare the specific element the
    claim refers to. The default is deliberately simple so the gate is usable
    without a browser; inject a domain-specific check for production.
    """
    return claim.strip() != "" and claim.strip() in dom_snapshot


def _default_consensus(claim: str, screenshot_hash: str) -> bool:
    """Second-model consensus as a stub.

    Without a model runtime we assume an independent re-ask would agree. Inject
    a real second-model call (or a deterministic stand-in for tests) to make
    this meaningful; the toy task does exactly that.
    """
    return True


class ReviewGate:
    """The verdict of :func:`resolve_branch`."""

    __slots__ = ("requires_review", "branch", "observation", "event")

    def __init__(
        self,
        requires_review: bool,
        branch: PlanBranch,
        observation: ObservationProvenance,
        event: Event | None,
    ) -> None:
        self.requires_review = requires_review
        self.branch = branch
        self.observation = observation
        self.event = event

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"ReviewGate(requires_review={self.requires_review}, branch={self.branch.branch_id})"


def verify_observation(
    claim: str,
    screenshot_hash: str,
    dom_snapshot: str | None = None,
    *,
    storage: object | None = None,
    run_id: str | None = None,
    q_vlm_model: str = "unknown",
    dom_check: object | None = None,
    consensus_check: object | None = None,
) -> ObservationProvenance:
    """Score a perception claim's trust from independent checks.

    The trust tier follows the spec: both checks pass -> ``verified``; a single
    partial signal -> ``unverified``; two signals that disagree or both fail ->
    ``contested``. Checks are injectable so tests and real verifiers (DOM
    consistency, an independent second model) can be plugged in.
    """
    dom_fn = dom_check if callable(dom_check) else _default_dom_consistency
    consensus_fn = consensus_check if callable(consensus_check) else _default_consensus

    checks: list[bool] = []
    if dom_snapshot is not None:
        checks.append(bool(dom_fn(claim, dom_snapshot)))
    checks.append(bool(consensus_fn(claim, screenshot_hash)))

    # A single signal can never fully verify: only when two independent checks
    # both pass do we trust the claim. Two signals that disagree (or both fail)
    # are contested, the strongest manipulation signal.
    if len(checks) == 1:
        trust = "unverified"
    elif all(checks):
        trust = "verified"
    else:
        trust = "contested"

    verifier = "dom+consensus" if dom_snapshot is not None else "consensus_only"
    if trust == "verified" and dom_snapshot is not None:
        verifier = "dom+consensus"
    elif trust != "verified" and dom_snapshot is None:
        verifier = "consensus_only"

    obs = ObservationProvenance(
        observation_id=make_id("obs"),
        source="environment_observed",
        trust_level=trust,
        verifier=verifier,
        content_hash=screenshot_hash,
        q_vlm_model=q_vlm_model,
        raw_claim=claim,
    )

    if storage is not None and run_id is not None:
        record_observation(storage, run_id, obs)

    return obs


def record_observation(storage: object, run_id: str, obs: ObservationProvenance) -> Event:
    """Append a ``PERCEPTION_OBSERVED`` event carrying the provenance."""
    # storage is a continuum Storage; typed loosely to avoid an import cycle in
    # callers that only hold a duck-typed handle.
    append = storage.append_event  # type: ignore[attr-defined]
    return append(  # type: ignore[no-any-return]
        run_id,
        EventType.PERCEPTION_OBSERVED,
        obs.model_dump(),
        source=Origin.EXTERNAL_AGENT,
    )


def resolve_branch(
    branch: PlanBranch,
    obs: ObservationProvenance,
    *,
    storage: object | None = None,
    run_id: str | None = None,
) -> ReviewGate:
    """Decide whether a branch may proceed or must be reviewed.

    Returns a :class:`ReviewGate`. When ``requires_review`` is true the caller
    must not execute the branch; route it to a human (the existing
    ``request_human`` recovery path), exactly as the spec's
    ``route_to_request_human`` does.
    """
    requires_review = (branch.risk_tier == "high" and obs.trust_level != "verified") or (
        obs.source == "environment_observed" and obs.trust_level == "contested"
    )

    event: Event | None = None
    if storage is not None and run_id is not None:
        append = storage.append_event  # type: ignore[attr-defined]
        event = append(
            run_id,
            EventType.BRANCH_RESOLVED,
            {
                "branch": branch.model_dump(),
                "observation": obs.model_dump(),
                "requires_review": bool(requires_review),
            },
            source=Origin.DETERMINISTIC,
        )

    return ReviewGate(
        requires_review=bool(requires_review),
        branch=branch,
        observation=obs,
        event=event,
    )
