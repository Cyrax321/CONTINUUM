"""Pluggable semantic state extraction.

An extractor turns a trajectory (the run's recorded events) plus an environment
into semantic state. The deterministic extractor is the default and the only
one required: it folds the event log and calls nothing external.

The optional LLM extractor exists for state that was never recorded structurally
— free-text reasoning a framework did not emit as events. It is constrained by
design:

* It runs only when explicitly enabled and given a callable; there is no
  provider SDK, no network default, no API key handling.
* It may only *add* components, never modify or delete what the deterministic
  fold produced. A model cannot overwrite a recorded fact.
* Everything it produces is tagged ``Origin.LLM`` and forced to
  ``REQUIRES_REVIEW``, so inferred state can never be silently trusted during
  recovery.

That asymmetry is the point: the deterministic layer is authoritative, the model
is an advisor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from continuum.events import Event
from continuum.models import (
    Decision,
    EnvironmentSnapshot,
    Finding,
    Origin,
    PendingWork,
    Provenance,
    SemanticState,
    StateStatus,
)
from continuum.state.semantic import ProjectionReport, project_incremental

__all__ = [
    "ExtractionContext",
    "StateExtractor",
    "DeterministicExtractor",
    "LLMExtractor",
    "CompositeExtractor",
    "LLMProposal",
]


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    """Everything an extractor is allowed to look at."""

    run_id: str
    trajectory: Sequence[Event]
    environment: EnvironmentSnapshot | None = None
    base: SemanticState | None = None


@runtime_checkable
class StateExtractor(Protocol):
    """Turn a trajectory into semantic state.

    Implementations must be side-effect free and must not require network
    access unless explicitly configured by the caller.
    """

    name: str

    def extract(self, context: ExtractionContext) -> SemanticState: ...


class DeterministicExtractor:
    """Folds recorded events into state. No model, no network, no clock."""

    name = "deterministic"

    def __init__(self) -> None:
        self.last_report: ProjectionReport | None = None

    def extract(self, context: ExtractionContext) -> SemanticState:
        state, report = project_incremental(
            context.run_id,
            context.trajectory,
            base=context.base,
        )
        self.last_report = report
        return state


@dataclass(frozen=True, slots=True)
class LLMProposal:
    """What a model is permitted to suggest."""

    decisions: Sequence[dict[str, Any]] = ()
    findings: Sequence[dict[str, Any]] = ()
    pending_work: Sequence[dict[str, Any]] = ()


#: A caller-supplied function. CONTINUUM never constructs one.
LLMCallable = Callable[[ExtractionContext, SemanticState], LLMProposal]


class LLMExtractor:
    """Optional enrichment layer over a base extractor.

    ``llm`` is supplied by the caller — CONTINUUM has no provider dependency.
    If it raises, extraction falls back to the deterministic result rather than
    failing the run: losing an optional enrichment must never cost a recovery.
    """

    name = "llm"

    def __init__(
        self,
        llm: LLMCallable,
        *,
        base: StateExtractor | None = None,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._base: StateExtractor = base or DeterministicExtractor()
        self.enabled = enabled
        self.last_error: Exception | None = None

    def extract(self, context: ExtractionContext) -> SemanticState:
        state = self._base.extract(context)
        if not self.enabled:
            return state

        try:
            proposal = self._llm(context, state)
        except Exception as exc:  # noqa: BLE001 - enrichment must never break recovery
            self.last_error = exc
            return state

        return self._merge(state, proposal)

    def _merge(self, state: SemanticState, proposal: LLMProposal) -> SemanticState:
        provenance = Provenance(origin=Origin.LLM, extractor=self.name)

        known_decisions = {d.decision_id for d in state.decisions}
        decisions = list(state.decisions)
        for raw in proposal.decisions:
            decision_id = str(raw.get("decision_id", ""))
            if not decision_id or decision_id in known_decisions:
                continue  # never overwrite a recorded fact
            # Also guards against a repeat later in this same proposal: the set is
            # seeded from the base state, so without this the second copy of an id
            # the LLM emitted twice would not be recognised as already handled.
            known_decisions.add(decision_id)
            decisions.append(
                Decision(
                    decision_id=decision_id,
                    decision=str(raw.get("decision", "")),
                    reason=str(raw.get("reason", "")),
                    evidence=[str(e) for e in raw.get("evidence", [])],
                    status=StateStatus.REQUIRES_REVIEW,
                    provenance=provenance,
                )
            )

        known_findings = {f.finding_id for f in state.findings}
        findings = list(state.findings)
        for raw in proposal.findings:
            finding_id = str(raw.get("finding_id", ""))
            if not finding_id or finding_id in known_findings:
                continue
            known_findings.add(finding_id)
            confidence = float(raw.get("confidence", 0.5))
            findings.append(
                Finding(
                    finding_id=finding_id,
                    claim=str(raw.get("claim", "")),
                    evidence=[str(e) for e in raw.get("evidence", [])],
                    confidence=min(max(confidence, 0.0), 1.0),
                    status=StateStatus.REQUIRES_REVIEW,
                    provenance=provenance,
                )
            )

        known_work = {w.task_id for w in state.pending_work}
        pending = list(state.pending_work)
        for raw in proposal.pending_work:
            task_id = str(raw.get("task_id", ""))
            if not task_id or task_id in known_work:
                continue
            known_work.add(task_id)
            pending.append(
                PendingWork(
                    task_id=task_id,
                    description=str(raw.get("description", "")),
                    status=StateStatus.REQUIRES_REVIEW,
                    provenance=provenance,
                )
            )

        return state.model_copy(
            update={
                "decisions": decisions,
                "findings": findings,
                "pending_work": pending,
            }
        )


class CompositeExtractor:
    """Chain extractors, feeding each result forward as the next one's base.

    Later extractors see an empty trajectory: the events were already folded by
    the first, and replaying them would double-apply progress counters.
    """

    name = "composite"

    def __init__(self, extractors: Iterable[StateExtractor]) -> None:
        self._extractors = tuple(extractors)
        if not self._extractors:
            raise ValueError("CompositeExtractor requires at least one extractor")

    def extract(self, context: ExtractionContext) -> SemanticState:
        first, *rest = self._extractors
        state = first.extract(context)
        for extractor in rest:
            state = extractor.extract(
                ExtractionContext(
                    run_id=context.run_id,
                    trajectory=(),
                    environment=context.environment,
                    base=state,
                )
            )
        return state
