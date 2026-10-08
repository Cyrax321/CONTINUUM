"""Recovery decisions, repair planning and contracts."""

from continuum.recovery.cleanup import cleanup_ephemeral_artifacts
from continuum.recovery.conformance import (
    ConformanceFailure,
    ConformanceResult,
    ContractFixture,
    run_conformance_suite,
)
from continuum.recovery.contract import (
    CONTRACT_DIGEST_FIELDS,
    CONTRACT_VERSIONS,
    CURRENT_CONTRACT_VERSION,
    SUPPORTED_CONTRACT_VERSIONS,
    build_contract,
    canonical_digest_input,
    contract_digest,
    render_contract,
    seal_contract,
    verify_contract,
    verify_contract_detailed,
)
from continuum.recovery.engine import SEVERITY, RecoveryDecision, RecoveryEngine
from continuum.recovery.gate import (
    EditPreconditionError,
    check_preconditions,
    summary_payload,
)
from continuum.recovery.impact import DependencyGraph, ImpactedSet
from continuum.recovery.ledger import (
    BudgetStatus,
    FileLedgerBackend,
    LedgerBackend,
    LedgerEntryKind,
    LedgerError,
    LedgerLockError,
    MemoryLedgerBackend,
    ReconcileReport,
    RecoveryLedger,
    RecoveryLedgerEntry,
    resolve_scope,
)
from continuum.recovery.limits import RecoveryTimeoutError, run_with_limits
from continuum.recovery.merge import MergePreconditionError, approve_merge
from continuum.recovery.planner import RepairKind, RepairPlan, RepairStep, plan_repairs
from continuum.recovery.preconditions import (
    DependedResult,
    DerivationResult,
    EditPoint,
    UncertainSlot,
    UnsettledAuthorization,
    derive,
)
from continuum.recovery.restore import RestorePreconditionError, approve_restore
from continuum.recovery.review_queue import ReviewItem, ReviewQueue
from continuum.recovery.rules import (
    STATUS_CAUTION,
    active_rules,
    apply_rule_findings,
    merge_validation_entries,
    run_validation_rules,
)

__all__ = [
    "ReviewItem",
    "ReviewQueue",
    "RecoveryTimeoutError",
    "CONTRACT_DIGEST_FIELDS",
    "CONTRACT_VERSIONS",
    "CURRENT_CONTRACT_VERSION",
    "ConformanceFailure",
    "ConformanceResult",
    "ContractFixture",
    "DependedResult",
    "DerivationResult",
    "EditPoint",
    "EditPreconditionError",
    "FileLedgerBackend",
    "MergePreconditionError",
    "RestorePreconditionError",
    "SUPPORTED_CONTRACT_VERSIONS",
    "approve_merge",
    "approve_restore",
    "canonical_digest_input",
    "check_preconditions",
    "cleanup_ephemeral_artifacts",
    "contract_digest",
    "run_conformance_suite",
    "run_with_limits",
    "SEVERITY",
    "DependencyGraph",
    "summary_payload",
    "ImpactedSet",
    "BudgetStatus",
    "LedgerBackend",
    "LedgerEntryKind",
    "LedgerError",
    "LedgerLockError",
    "MemoryLedgerBackend",
    "ReconcileReport",
    "RecoveryDecision",
    "RecoveryEngine",
    "RecoveryLedger",
    "RecoveryLedgerEntry",
    "RepairKind",
    "RepairPlan",
    "RepairStep",
    "UncertainSlot",
    "UnsettledAuthorization",
    "build_contract",
    "derive",
    "plan_repairs",
    "render_contract",
    "resolve_scope",
    "seal_contract",
    "verify_contract",
    "verify_contract_detailed",
    "STATUS_CAUTION",
    "active_rules",
    "apply_rule_findings",
    "merge_validation_entries",
    "run_validation_rules",
]
