"""CONTINUUM: verifiable semantic recovery layer for long-running AI agents.

Agents that can lose their context without losing their work.

Phase 1 exposes the durable data model and the append-only event log. The
runtime (``Continuum``), storage engines, validation, action ledger and CLI
arrive in later phases; nothing here imports an LLM provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from continuum.actions import (
    ActionLedger,
    ActionOutcome,
    AssumeNotOccurredReconciler,
    ManualReconciler,
    ProbeReconciler,
    Reconciler,
    ReconciliationReport,
    Resolution,
    arguments_hash,
    idempotency_key,
    reconcile_pending,
    unresolved_actions,
)
from continuum.adapters import (
    AdapterRegistry,
    AgentAdapter,
    GenericAgentAdapter,
    get_adapter,
    list_adapters,
    recover,
    register_adapter,
)
from continuum.checkpoint import (
    CheckpointDecision,
    CheckpointError,
    CheckpointManager,
    CheckpointPolicy,
    CheckpointTrigger,
    ContextPressurePolicy,
    EventPolicy,
    HybridPolicy,
    IntervalPolicy,
    ManualPolicy,
    RecoveryContext,
    RestoredRun,
    SemanticPolicy,
    build_recovery_context,
    default_policy,
)
from continuum.environment import (
    CallableProvider,
    EnvironmentDiff,
    EnvironmentProvider,
    FileProvider,
    ResourceChange,
    ResourceDelta,
    StaticProvider,
    ValueProvider,
    capture_environment,
    diff_environments,
)
from continuum.events import (
    AppendOnlyViolation,
    Event,
    EventLog,
    EventType,
    IntegrityReport,
    IntegrityViolation,
)
from continuum.models import (
    Action,
    ActionStatus,
    Approval,
    ApprovalStatus,
    Component,
    ComponentValidationEntry,
    Decision,
    DiffEntry,
    DiffKind,
    EnvironmentSnapshot,
    EnvResource,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    Origin,
    PendingWork,
    PlanStep,
    PlanStepStatus,
    Progress,
    Provenance,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    Run,
    RunStatus,
    SemanticState,
    StateCheckpoint,
    StateDiff,
    StateStatus,
    StateValidationResult,
    UnknownSideEffect,
    utcnow,
)
from continuum.provenance_map import (
    CanonicalProvenance,
    ProvenanceView,
    canonical_origin,
    canonical_state_status,
    canonical_trust,
    summarize,
)
from continuum.recovery import (
    DependencyGraph,
    ImpactedSet,
    LedgerEntryKind,
    LedgerError,
    LedgerLockError,
    ReconcileReport,
    RecoveryDecision,
    RecoveryEngine,
    RecoveryLedger,
    RecoveryLedgerEntry,
    RepairKind,
    RepairPlan,
    RepairStep,
    build_contract,
    plan_repairs,
    render_contract,
    verify_contract,
)
from continuum.state import (
    CompositeExtractor,
    DeterministicExtractor,
    ExtractionContext,
    LLMExtractor,
    LLMProposal,
    ProjectionError,
    StateExtractor,
    VersionChain,
    VersionEntry,
    diff_states,
    project,
    project_incremental,
    render_diff,
    state_fingerprint,
)
from continuum.state.validator import StateValidator, ValidationOutcome, validate_state
from continuum.storage import (
    CheckpointNotFound,
    ConcurrentWriteError,
    CorruptedRecord,
    RunNotFound,
    SchemaVersionError,
    SQLiteStorage,
    Storage,
    StorageError,
    open_storage,
)

__version__ = "0.1.0"

# Framework-adapter names resolve lazily (PEP 562, issue #214): importing the
# package must not pay for openai/langgraph/langchain, because every entry
# point imports this package, including processes that never touch an
# adapter. Type-checkers see the real definitions here; runtime resolves them
# through __getattr__ below.
if TYPE_CHECKING:
    from continuum.adapters.langchain import LangChainAgentAdapter
    from continuum.adapters.langgraph import LangGraphAgentAdapter
    from continuum.adapters.openai import OpenAIAgentAdapter

_LAZY_TOP_LEVEL: dict[str, str] = {
    "LangChainAgentAdapter": "langchain",
    "LangGraphAgentAdapter": "langgraph",
    "OpenAIAgentAdapter": "openai",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_TOP_LEVEL.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f"continuum.adapters.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_TOP_LEVEL))


__all__ = [
    "__version__",
    # events
    "AppendOnlyViolation",
    "Event",
    "EventLog",
    "EventType",
    "IntegrityReport",
    "IntegrityViolation",
    # enums
    "ActionStatus",
    "ApprovalStatus",
    "Component",
    "DiffKind",
    "Origin",
    "PlanStepStatus",
    "RecoveryMode",
    "RecoverySafety",
    "RunStatus",
    "StateStatus",
    # state
    "Action",
    "Approval",
    "ComponentValidationEntry",
    "Decision",
    "DiffEntry",
    "EnvResource",
    "EnvironmentSnapshot",
    "Evidence",
    "ExternalDependency",
    "Finding",
    "Goal",
    "ModelSpecificState",
    "ModelState",
    "PendingWork",
    "PlanStep",
    "Progress",
    "Provenance",
    "Run",
    "RecoveryContract",
    "SemanticState",
    "StateCheckpoint",
    "StateDiff",
    "StateValidationResult",
    "UnknownSideEffect",
    "utcnow",
    # projection, extraction, versioning, diffing
    "CompositeExtractor",
    "DeterministicExtractor",
    "ExtractionContext",
    "LLMExtractor",
    "LLMProposal",
    "ProjectionError",
    "StateExtractor",
    "VersionChain",
    "VersionEntry",
    "diff_states",
    "project",
    "project_incremental",
    "render_diff",
    "state_fingerprint",
    # storage
    "CheckpointNotFound",
    "ConcurrentWriteError",
    "CorruptedRecord",
    "RunNotFound",
    "SQLiteStorage",
    "SchemaVersionError",
    "Storage",
    "StorageError",
    "open_storage",
    # checkpointing
    "CheckpointDecision",
    "CheckpointError",
    "CheckpointManager",
    "CheckpointPolicy",
    "CheckpointTrigger",
    "ContextPressurePolicy",
    "EventPolicy",
    "HybridPolicy",
    "IntervalPolicy",
    "ManualPolicy",
    "RecoveryContext",
    "RestoredRun",
    "SemanticPolicy",
    "build_recovery_context",
    "default_policy",
    # environment + validation
    "CallableProvider",
    "EnvironmentDiff",
    "EnvironmentProvider",
    "FileProvider",
    "ResourceChange",
    "ResourceDelta",
    "StateValidator",
    "StaticProvider",
    "ValidationOutcome",
    "ValueProvider",
    "capture_environment",
    "diff_environments",
    "validate_state",
    # action ledger
    "ActionLedger",
    "ActionOutcome",
    "AssumeNotOccurredReconciler",
    "ManualReconciler",
    "ProbeReconciler",
    "Reconciler",
    "ReconciliationReport",
    "Resolution",
    "arguments_hash",
    "idempotency_key",
    "reconcile_pending",
    "unresolved_actions",
    # recovery engine
    "DependencyGraph",
    "ImpactedSet",
    "LedgerEntryKind",
    "LedgerError",
    "LedgerLockError",
    "RecoveryDecision",
    "RecoveryEngine",
    "RecoveryLedger",
    "RecoveryLedgerEntry",
    "ReconcileReport",
    "RepairKind",
    "RepairPlan",
    "RepairStep",
    "build_contract",
    "plan_repairs",
    "render_contract",
    "verify_contract",
    # agent framework adapters
    "AdapterRegistry",
    "AgentAdapter",
    "GenericAgentAdapter",
    "LangChainAgentAdapter",
    "LangGraphAgentAdapter",
    "OpenAIAgentAdapter",
    "get_adapter",
    "list_adapters",
    "recover",
    "register_adapter",
    # provenance (canonical mapping)
    "CanonicalProvenance",
    "ProvenanceView",
    "canonical_origin",
    "canonical_state_status",
    "canonical_trust",
    "summarize",
]
