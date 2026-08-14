"""Perception and planning provenance for the secure planning loop.

These models are additive. They record what a perception model claimed about
the environment and how a planner decomposed its intent, so a manipulated
branch can be audited and gated instead of firing silently. See docs/PROBLEM.md
(Extension 1).

Both models are frozen: a recorded observation or plan branch is an
immutable fact in the audit trail, not something later code may rewrite.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ObservationSource = Literal["environment_observed", "user_instructed", "deterministic"]
TrustLevel = Literal["verified", "unverified", "contested"]
RiskTier = Literal["low", "medium", "high"]


class ObservationProvenance(BaseModel):
    """What a perception model claimed about the environment, and how much it can be trusted.

    ``raw_claim`` is stored exactly as returned and is never mutated, so the
    audit trail shows the precise text that drove a branch decision.
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str
    source: ObservationSource
    trust_level: TrustLevel
    verifier: str | None  # e.g. "dom_consistency", "multi_modal_consensus", "dom+consensus", None
    content_hash: str  # sha256 of the screenshot/DOM slice that produced this claim
    q_vlm_model: str
    raw_claim: str


class PlanBranch(BaseModel):
    """One branch of a plan, tagged with the risk it carries if executed.

    The planner emits branches in the abstract (intent and risk) without
    resolving them against live perception; only the harness combines a branch
    with an :class:`ObservationProvenance` via the trust gate.
    """

    model_config = ConfigDict(frozen=True)

    branch_id: str
    risk_tier: RiskTier
    action_intent: str  # e.g. "delete_file", "submit_payment", "scroll_view", "read_text"
    depends_on_observation: bool
    resolved_by: str | None = None  # observation_id, filled in at resolution time
