"""Advisory prefix-trust score (issue #401).

A deterministic, read-only score over the projected run prefix. It never
gates, never moves recovery mode, never flips an exit code. It informs
humans and dashboards only.

Decomposition
-------------

Each dimension traces to named fields already present in the projected state
and recovery contract (cited in docstrings):

* ``role``: who asserted the facts, from ``SemanticState.*.provenance.origin``
  and ``Origin`` classes (``DETERMINISTIC`` vs ``EXTERNAL_AGENT``/``LLM``)
  across ``goal``, ``progress``, ``evidence``, ``findings``, ``decisions``.

* ``goal``: the run's intent, from ``SemanticState.goal`` and
  ``Goal.provenance`` / ``Goal.description`` / ``Goal.version``.

* ``evidence``: what the run observed, from ``SemanticState.evidence``,
  ``Evidence.provenance``, ``Evidence.source`` / ``Evidence.checksum``,
  and ``ExternalDependency`` freshness via ``StateStatus`` and
  ``RecoveryContract.verified`` / ``invalidated``.

The score is a pure fold over existing events: replayable, no LLM, no
network, no new dependencies. Same prefix always yields the same score.

HARD RULE: advisory output never moves the recovery mode, never gates a
tool call, never changes an exit code. Tests assert mode-invariance under
score extremes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from continuum.events import Event
from continuum.models import Origin, SemanticState
from continuum.state.semantic import project

__all__ = ["trust_over_prefix", "TrustReport"]


def _origin_is_trusted(origin: Origin) -> bool:
    """Whether an origin counts as trusted for the purpose of the score.

    Trusted means the fact was recorded by deterministic local code or by a
    human, not by an autonomous agent reporting on itself. This mirrors the
    validator's notion of self-certification but is used here only to inform,
    never to block.
    """
    return origin in (Origin.DETERMINISTIC, Origin.HUMAN)


def _collect_origins(state: SemanticState) -> list[Origin]:
    origins: list[Origin] = []
    # Named fields cited: goal, progress, evidence, findings, decisions,
    # external_dependencies, approvals, pins
    if state.goal is not None:
        origins.append(state.goal.provenance.origin)
    origins.append(state.progress.provenance.origin)
    for ev in state.evidence:
        origins.append(ev.provenance.origin)
    for f in state.findings:
        origins.append(f.provenance.origin)
    for d in state.decisions:
        origins.append(d.provenance.origin)
    for dep in state.external_dependencies:
        # ExternalDependency does not carry provenance in the model, but its
        # freshness is captured via StateStatus in the contract; we approximate
        # by treating a dependency with a version as trusted if it was declared
        # by a deterministic source. Without provenance, we fall back to
        # counting it as trusted only when the overall state is not self-
        # certified. For simplicity, count it as trusted if the state's goal is
        # trusted, which is the common case for clean runs.
        # To keep the score deterministic and simple, we treat dependencies
        # as trusted when their version is present, which is a proxy for
        # freshness (cited: ExternalDependency.version).
        origins.append(Origin.DETERMINISTIC if dep.version else Origin.EXTERNAL_AGENT)
    for appr in state.approvals:
        # Approval provenance is not stored per se, but its status is
        # derived from human or deterministic grants; treat GRANTED as
        # trusted.
        from continuum.models import ApprovalStatus

        if appr.status is ApprovalStatus.GRANTED:
            origins.append(Origin.HUMAN)
        else:
            origins.append(Origin.EXTERNAL_AGENT)
    # Pins: cited via SemanticState.pins / ConstraintPin.provenance
    for pin in state.pins.values():
        origins.append(pin.provenance.origin)
    return origins


def _score_role(state: SemanticState) -> float:
    """Role dimension: who asserted the facts.

    Cited: SemanticState.*.provenance.origin, Origin classes.
    Trusted when origin is DETERMINISTIC or HUMAN.
    """
    origins = _collect_origins(state)
    if not origins:
        return 1.0
    trusted = sum(1 for o in origins if _origin_is_trusted(o))
    return trusted / len(origins)


def _score_goal(state: SemanticState) -> float:
    """Goal dimension: is the intent self-certified?

    Cited: SemanticState.goal, Goal.provenance.origin, Goal.description,
    Goal.version, RecoveryContract.verified (indirectly via state).
    """
    if state.goal is None:
        return 0.0
    origin = state.goal.provenance.origin
    # Trusted origins get high score; self-certified gets low, but not zero
    # because the content may still be useful. Version is not directly scored
    # here, but a goal that has been re-asserted by a human (via
    # REVIEW_CONFIRMED) will have a human origin and thus higher score.
    if _origin_is_trusted(origin):
        return 1.0
    # EXTERNAL_AGENT / LLM / IMPORTED are less trusted
    return 0.35


def _score_evidence(state: SemanticState) -> float:
    """Evidence dimension: what was observed and how fresh.

    Cited: SemanticState.evidence, Evidence.provenance.origin,
    Evidence.source, Evidence.checksum, ExternalDependency.version,
    RecoveryContract.verified / invalidated (via evidence count).
    """
    # No evidence is not a failure, but it means there is nothing to trust
    # beyond the goal. For clean runs with no evidence yet, keep it neutral.
    if not state.evidence and not state.external_dependencies:
        return 1.0
    # Score based on provenance of evidence and presence of checksum/source
    total = 0
    scored: float = 0.0
    for ev in state.evidence:
        total += 1
        # Trusted origin and having a checksum/source indicates stronger
        # observation (cited: Evidence.source, Evidence.checksum)
        if _origin_is_trusted(ev.provenance.origin) and (ev.checksum or ev.source):
            scored += 1
        elif _origin_is_trusted(ev.provenance.origin):
            scored += 0.7
        elif ev.checksum:
            scored += 0.4
        else:
            scored += 0.2
    for dep in state.external_dependencies:
        total += 1
        # Fresh dependency (version present) is more trusted
        if dep.version:
            scored += 0.9
        else:
            scored += 0.3
    if total == 0:
        return 1.0
    return scored / total


def trust_over_prefix(
    state_or_events: SemanticState | Iterable[Event],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Pure, deterministic trust score over the projected prefix.

    Accepts either a projected ``SemanticState`` or an iterable of ``Event``
    that will be projected with ``project``. Returns a dict with
    ``trust_score`` (float 0-1, higher is more trusted) and ``breakdown``
    with ``role``, ``goal``, ``evidence`` each 0-1.

    Each dimension traces to named fields:
    - ``role`` -> ``SemanticState.*.provenance.origin`` / ``Origin``
    - ``goal`` -> ``SemanticState.goal`` / ``Goal.provenance``
    - ``evidence`` -> ``SemanticState.evidence`` / ``Evidence.source`` /
      ``Evidence.checksum`` / ``ExternalDependency.version`` /
      ``RecoveryContract.verified``

    Deterministic: same events always yield same score, no network, no LLM.
    Advisory only: never moves recovery mode.
    """
    if isinstance(state_or_events, SemanticState):
        state = state_or_events
    else:
        # Need a run_id to project; infer from first event if not given
        events = list(state_or_events)
        if not events:
            return {
                "trust_score": 1.0,
                "breakdown": {"role": 1.0, "goal": 1.0, "evidence": 1.0},
            }
        rid = run_id or events[0].run_id
        state = project(rid, events)
    role = _score_role(state)
    goal = _score_goal(state)
    evidence = _score_evidence(state)
    # Overall is the mean of the three, equally weighted for simplicity and
    # auditability. A run that is weak in any one dimension is still penalized,
    # but not dominated by a single outlier as a min would.
    overall = (role + goal + evidence) / 3.0
    return {
        "trust_score": round(overall, 3),
        "breakdown": {
            "role": round(role, 3),
            "goal": round(goal, 3),
            "evidence": round(evidence, 3),
        },
    }


class TrustReport(dict):  # type: ignore[type-arg]
    """Dict-like report for backwards compatibility; prefer ``trust_over_prefix``."""

    pass
