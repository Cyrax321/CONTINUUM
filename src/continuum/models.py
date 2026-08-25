"""CONTINUUM data models.

Phase 1 defines the *shape* of durable task state: enums, the semantic state
tree, ledger records, environment snapshots, validation reports and recovery
contracts. No storage or recovery logic lives here — these are pure data
structures (mostly immutable) so they can be serialized, versioned, hashed and
diffed without side effects.

Conventions
-----------
* All times are timezone-aware UTC.
* All IDs are stable strings (``run_..``, ``action_..``, ``finding_..``).
* Enums are ``str`` subclasses so they serialize to readable JSON.
* State-bearing models are frozen: mutations must produce a new version via
  ``model_copy`` — the versioning phase builds on this property.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from continuum.security.hashing import make_id, stable_hash

__all__ = [
    "RunStatus",
    "StateStatus",
    "ActionStatus",
    "RecoveryMode",
    "RecoverySafety",
    "Component",
    "DiffKind",
    "ApprovalStatus",
    "PlanStepStatus",
    "utcnow",
    "Goal",
    "PlanStep",
    "Progress",
    "Decision",
    "Evidence",
    "Finding",
    "PendingWork",
    "Approval",
    "ExternalDependency",
    "ModelSpecificState",
    "ModelState",
    "Run",
    "SemanticState",
    "Action",
    "EnvResource",
    "EnvironmentSnapshot",
    "ComponentValidationEntry",
    "StateValidationResult",
    "RecoveryContract",
    "StateCheckpoint",
    "DiffEntry",
    "StateDiff",
    "UnknownSideEffect",
]

Frozen = ConfigDict(frozen=True, extra="forbid")


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    CRASHED = "crashed"
    ABORTED = "aborted"
    FAILED = "failed"


class StateStatus(StrEnum):
    VALID = "valid"
    STALE = "stale"
    CONFLICTED = "conflicted"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    REQUIRES_REVIEW = "requires_review"
    EXPIRED = "expired"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    COMPENSATED = "compensated"
    REQUIRES_REVIEW = "requires_review"
    EXPIRED = "expired"


class RecoveryMode(StrEnum):
    RESUME = "resume"
    REPAIR_AND_RESUME = "repair_and_resume"
    ROLLBACK = "rollback"
    WAIT = "wait"
    REQUEST_HUMAN = "request_human"
    REPLAN = "replan"
    ABORT = "abort"


class RecoverySafety(StrEnum):
    SAFE_TO_RESUME = "safe_to_resume"
    REQUIRES_REPAIR = "requires_repair"
    REQUIRES_REVALIDATION = "requires_revalidation"
    REQUIRES_HUMAN = "requires_human"
    BLOCKED = "blocked"
    UNSAFE = "unsafe"


class Component(StrEnum):
    GOAL = "goal"
    PROGRESS = "progress"
    PLAN = "plan"
    DECISION = "decision"
    FINDING = "finding"
    EVIDENCE = "evidence"
    PENDING_WORK = "pending_work"
    EXTERNAL_DEPENDENCY = "external_dependency"
    ACTION = "action"
    MODEL = "model"
    APPROVAL = "approval"
    ENVIRONMENT = "environment"


class DiffKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    INVALIDATED = "invalidated"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    REVOKED = "revoked"
    EXPIRED = "expired"
    REQUIRES_REVIEW = "requires_review"


class PlanStepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    COMPLETED = "completed"


class Origin(StrEnum):
    """Who asserted a fact — decides how much it can be trusted.

    This describes the *writer*, not the derivation. Folding a fabricated event
    is still a faithful fold, so "the projection is reproducible" says nothing
    about whether the underlying claim is true. Provenance has to be captured
    where the claim enters the system and carried forward from there.
    """

    DETERMINISTIC = "deterministic"
    """Recorded by trusted local code: the CLI, or an adapter called in-process.

    Not a claim that the fact is *correct* — only that it was not asserted by an
    autonomous agent reporting on itself.
    """

    HUMAN = "human"
    """Asserted by a person."""

    LLM = "llm"
    """Inferred by a model. Never authoritative; always requires review."""

    EXTERNAL_AGENT = "external_agent"
    """Asserted by an autonomous agent over a remote interface such as MCP.

    An agent reporting its own progress is marking its own homework. The claim
    may well be true, but nothing has verified it, so it cannot on its own
    establish that a run is safe to resume.
    """

    IMPORTED = "imported"
    """Loaded from a foreign checkpoint whose event history is unavailable."""

    @property
    def self_certified(self) -> bool:
        """Whether this origin is an unverified self-report.

        Such state is usable — it is often correct — but it cannot be the
        grounds for declaring a run verified.
        """
        return self in (Origin.LLM, Origin.EXTERNAL_AGENT, Origin.IMPORTED)


class Provenance(BaseModel):
    """Trace from a state component back to the event that produced it."""

    model_config = Frozen

    origin: Origin = Origin.DETERMINISTIC
    source_sequence: int | None = None
    source_event_id: str | None = None
    extractor: str | None = None

    @property
    def reproducible(self) -> bool:
        """True when the component can be re-derived from the event log alone."""
        return self.origin is Origin.DETERMINISTIC and self.source_sequence is not None


# --------------------------------------------------------------------------- #
# Goal / plan / progress
# --------------------------------------------------------------------------- #


class Goal(BaseModel):
    model_config = Frozen

    description: str
    version: int = 1
    constraints: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("version")
    @classmethod
    def _version_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("goal version must be >= 1")
        return v


class PlanStep(BaseModel):
    model_config = Frozen

    step_id: str = Field(default_factory=lambda: make_id("step"))
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class Progress(BaseModel):
    model_config = Frozen

    total: int | None = None
    completed: int = 0
    pending: int = 0
    failed: int = 0
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("total", "completed", "pending", "failed")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("progress counters must be non-negative")
        return v

    @model_validator(mode="after")
    def _bounded(self) -> Progress:
        if self.total is not None and self.completed + self.pending + self.failed > self.total:
            raise ValueError("completed + pending + failed exceeds total")
        return self


# --------------------------------------------------------------------------- #
# Semantic state components
# --------------------------------------------------------------------------- #


class Decision(BaseModel):
    """A durable decision the agent made, with its evidence trail."""

    model_config = Frozen

    decision_id: str = Field(default_factory=lambda: make_id("decision"))
    decision: str
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    status: StateStatus = StateStatus.VALID
    created_at: datetime = Field(default_factory=utcnow)
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)


class Evidence(BaseModel):
    model_config = Frozen

    evidence_id: str = Field(default_factory=lambda: make_id("evidence"))
    summary: str = ""
    source: str | None = None
    checksum: str | None = None
    status: StateStatus = StateStatus.VALID
    added_at: datetime = Field(default_factory=utcnow)
    provenance: Provenance = Field(default_factory=Provenance)


class Finding(BaseModel):
    model_config = Frozen

    finding_id: str = Field(default_factory=lambda: make_id("finding"))
    claim: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    status: StateStatus = StateStatus.VALID
    created_at: datetime = Field(default_factory=utcnow)
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("confidence")
    @classmethod
    def _confidence_unit(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        return v


class PendingWork(BaseModel):
    model_config = Frozen

    task_id: str = Field(default_factory=lambda: make_id("task"))
    description: str
    prerequisite: list[str] = Field(default_factory=list)
    status: StateStatus = StateStatus.VALID
    created_at: datetime = Field(default_factory=utcnow)
    provenance: Provenance = Field(default_factory=Provenance)


class Approval(BaseModel):
    """A human approval with a lifetime; approvals can expire."""

    model_config = Frozen

    approval_id: str = Field(default_factory=lambda: make_id("approval"))
    subject: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    granted_at: datetime | None = None
    granted_by: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None


class ExternalDependency(BaseModel):
    model_config = Frozen

    resource: str
    kind: str = "resource"
    version: str | None = None
    checksum: str | None = None
    status: StateStatus = StateStatus.VALID
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    last_verified_at: datetime | None = None
    provenance: Provenance = Field(default_factory=Provenance)


class ModelSpecificState(BaseModel):
    """An assumption tied to a specific model; switching models must revalidate."""

    model_config = Frozen

    item_id: str = Field(default_factory=lambda: make_id("model_state"))
    description: str
    required_validation: str = "Must be revalidated after model change."


class ModelState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    model: str | None = None
    provider: str | None = None
    fingerprint: str | None = None
    model_specific_state: list[ModelSpecificState] = Field(default_factory=list)


class SemanticState(BaseModel):
    """The compact, durable representation of task state.

    This is what survives crashes and context loss — NOT the transcript.

    A state is a *projection* of an event prefix. ``source_sequence`` records
    how far into the log the projection consumed, which makes the state
    reproducible: folding the same prefix again must yield an equal state.

    A state whose log stopped folding partway (issue #383) is marked
    ``status=INVALID`` and names the break in the ``unprojectable_*`` fields.
    Such a state reports what was known through its last good event; it must
    never be read as a complete picture of the run.
    """

    model_config = Frozen

    run_id: str
    goal: Goal
    progress: Progress = Field(default_factory=Progress)
    plan: list[PlanStep] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    pending_work: list[PendingWork] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    external_dependencies: list[ExternalDependency] = Field(default_factory=list)
    model: ModelState | None = None
    version: int = 0
    source_sequence: int = 0
    """Highest event sequence folded into this state (0 = nothing consumed)."""
    status: StateStatus = StateStatus.VALID
    """VALID for a complete fold. INVALID marks a degraded projection that
    stopped at ``unprojectable_at_sequence``."""
    unprojectable_at_sequence: int | None = None
    """Sequence of the earliest event the fold refused, or None when the whole
    log folded."""
    unprojectable_event_type: str | None = None
    """Type of the refused event, as ``unprojectable_reason`` alone may not name it."""
    unprojectable_reason: str | None = None
    """Single-line statement of the constraint the refused event violated."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("version", "source_sequence")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    def next_version(self, **overrides: Any) -> SemanticState:
        """Produce the next versioned state from this one (immutable update)."""
        return self.model_copy(
            update={
                "version": self.version + 1,
                "updated_at": utcnow(),
                **overrides,
            }
        )

    # -- lookups used by validation and recovery -------------------------- #

    def decision(self, decision_id: str) -> Decision | None:
        return next((d for d in self.decisions if d.decision_id == decision_id), None)

    def finding(self, finding_id: str) -> Finding | None:
        return next((f for f in self.findings if f.finding_id == finding_id), None)

    def dependency(self, resource: str) -> ExternalDependency | None:
        return next((d for d in self.external_dependencies if d.resource == resource), None)

    def evidence_ids(self) -> frozenset[str]:
        return frozenset(e.evidence_id for e in self.evidence)

    def valid_decisions(self) -> tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.status is StateStatus.VALID)

    def open_work(self) -> tuple[PendingWork, ...]:
        return tuple(w for w in self.pending_work if w.status is not StateStatus.INVALID)

    def dangling_evidence(self) -> frozenset[str]:
        """Support cited by decisions or findings that the state cannot produce.

        A decision may cite either raw evidence or a finding derived from it —
        both are legitimate provenance. Only references matching neither are
        dangling. Treating a cited finding as missing evidence would raise a
        false alarm on every well-formed reasoning chain, and false alarms are
        how real ones get ignored.
        """
        known = self.evidence_ids() | frozenset(f.finding_id for f in self.findings)
        cited: set[str] = set()
        for decision in self.decisions:
            cited.update(decision.evidence)
        for finding in self.findings:
            cited.update(finding.evidence)
        return frozenset(cited - known)


# --------------------------------------------------------------------------- #
# Action ledger
# --------------------------------------------------------------------------- #


class Action(BaseModel):
    """A record of an external side effect, for idempotent reconciliation."""

    model_config = Frozen

    action_id: str = Field(default_factory=lambda: make_id("action"))
    run_id: str
    action_type: str
    dep_scope: str | None = None
    arguments: Mapping[str, Any] = Field(default_factory=dict)
    arguments_hash: str | None = None
    status: ActionStatus = ActionStatus.PLANNED
    external_id: str | None = None
    result: Mapping[str, Any] | None = None
    result_hash: str | None = None
    side_effect_uncertain: bool = False
    compensated_by: list[str] = Field(default_factory=list)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class UnknownSideEffect(RuntimeError):
    """Raised when CONTINUUM cannot determine whether an external side effect occurred.

    The caller must reconcile (do not blindly retry).

    ``action_key`` and ``action_id`` carry the identity of the action needing
    reconciliation, when the raiser knows it. Telling a caller to reconcile
    without telling it *what* to reconcile is not actionable, and a recovering
    session is by definition the one least able to reconstruct that identity for
    itself (issue #367). Both are optional: a cross-run refusal is raised about
    another run's record, which this ledger has no standing to settle.
    """

    def __init__(
        self,
        message: str,
        *,
        action_key: str | None = None,
        action_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.action_key = action_key
        self.action_id = action_id


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


class EnvResource(BaseModel):
    model_config = Frozen

    name: str
    kind: str = "resource"
    version: str | None = None
    checksum: str | None = None
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class EnvironmentSnapshot(BaseModel):
    model_config = Frozen

    env_id: str = Field(default_factory=lambda: make_id("env"))
    run_id: str
    captured_at: datetime = Field(default_factory=utcnow)
    resources: Mapping[str, EnvResource] = Field(default_factory=dict)
    integrity_hash: str | None = None


# --------------------------------------------------------------------------- #
# Validation + recovery
# --------------------------------------------------------------------------- #


class ComponentValidationEntry(BaseModel):
    model_config = Frozen

    component: Component
    component_id: str | None = None
    status: StateStatus
    detail: str = ""


class StateValidationResult(BaseModel):
    model_config = Frozen

    run_id: str
    checkpoint_version: int = 0
    statuses: list[ComponentValidationEntry] = Field(default_factory=list)
    safe_to_resume: bool = False
    recovery_mode: RecoveryMode | None = None
    reason: str = ""
    validated_at: datetime = Field(default_factory=utcnow)


class RecoveryContract(BaseModel):
    """The machine-readable answer to "what am I allowed to do now, and why?".

    ``evidence`` and ``reason`` are additive, backward-compatible fields added
    in Phase 1 so a contract can explain *why* CONTINUUM reached its decision
    and *what* evidence drove it. They are threaded from the existing
    validation report and recovery rationale; nothing here is invented. Both
    default to empty so contracts serialized before they existed still load.
    """

    model_config = Frozen

    run_id: str
    checkpoint_version: int = 0
    recovery_status: RecoverySafety
    verified: list[str] = Field(default_factory=list)
    invalidated: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)
    next_allowed_action: str | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    #: File observations recorded by host hooks (#210) after the latest
    #: checkpoint, disk-checked at assess time (#208). Informational only:
    #: never affects the recovery decision. Newest first; a trailing row with
    #: ``truncated`` marks omitted older rows when the cap bites.
    post_checkpoint_observations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    integrity_hash: str | None = None


# --------------------------------------------------------------------------- #
# Checkpoints + diffs
# --------------------------------------------------------------------------- #


class Run(BaseModel):
    """A single long-running task, and the anchor for everything durable."""

    model_config = Frozen

    run_id: str = Field(default_factory=lambda: make_id("run"))
    goal: str
    status: RunStatus = RunStatus.STARTED
    parent_run_id: str | None = None
    """Set on child runs in a multi-agent hierarchy (issue #243). Children
    aggregate into the parent's resume contract; siblings share nothing."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    def touch(self, **overrides: Any) -> Run:
        return self.model_copy(update={"updated_at": utcnow(), **overrides})


#: Fields that describe how an event log was *read*, not what a run's state
#: is (issue #383). They live on SemanticState so a degraded fold can carry
#: its diagnosis, but they are outside every durable identity of a state:
#: excluded from the version fingerprint and from checkpoint integrity hashes
#: (both predate #383; hashing them would brand every existing record as
#: tampered), and omitted from persisted bodies so readers built before #383,
#: whose SemanticState forbids extra inputs, can still load newer databases.
PROJECTION_BOOKKEEPING: set[str] = {
    "status",
    "unprojectable_at_sequence",
    "unprojectable_event_type",
    "unprojectable_reason",
}


class StateCheckpoint(BaseModel):
    model_config = Frozen

    checkpoint_id: str = Field(default_factory=lambda: make_id("checkpoint"))
    run_id: str
    version: int = 0
    trigger: str = "manual"
    reason: str = ""
    state: SemanticState
    environment: EnvironmentSnapshot | None = None
    created_at: datetime = Field(default_factory=utcnow)
    integrity_hash: str | None = None

    def content(self) -> dict[str, Any]:
        """The sealed portion of the checkpoint (everything but the hash).

        Projection bookkeeping is excluded beside the hash itself: it was added
        after these checkpoints existed (#383), it is default in anything that
        can be persisted (every capture path refuses a degraded fold), and
        hashing it would report every checkpoint written by earlier builds as
        tampered, which is exactly the alarm this hash exists to mean.
        """
        return self.model_dump(
            mode="json", exclude={"integrity_hash": True, "state": PROJECTION_BOOKKEEPING}
        )

    def canonical_json(self) -> str:
        """The serialised form written to storage.

        Omits projection bookkeeping for the same reasons ``content`` does,
        plus one more: readers built before #383 validate ``SemanticState``
        with ``extra="forbid"`` and would refuse a body carrying fields they
        have never heard of. Omitting them costs nothing, since they are
        always default in anything persistable.
        """
        return self.model_dump_json(exclude={"state": PROJECTION_BOOKKEEPING})

    def digest(self) -> str:
        return stable_hash(self.content())

    def sealed(self) -> StateCheckpoint:
        """Return a copy carrying its integrity hash.

        A checkpoint is the thing recovery trusts, so it is sealed the same way
        events are: the hash is over the content, and a mismatch on read means
        the record was changed outside CONTINUUM.
        """
        return self.model_copy(update={"integrity_hash": self.digest()})

    def verify(self) -> bool:
        return self.integrity_hash is not None and self.integrity_hash == self.digest()


class DiffEntry(BaseModel):
    model_config = Frozen

    kind: DiffKind
    component: Component
    component_id: str | None = None
    detail: str = ""
    before: Any = None
    after: Any = None


class StateDiff(BaseModel):
    model_config = Frozen

    run_id: str
    from_version: int
    to_version: int
    entries: list[DiffEntry] = Field(default_factory=list)
