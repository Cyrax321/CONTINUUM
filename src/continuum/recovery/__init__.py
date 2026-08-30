"""Recovery decisions, repair planning and contracts."""

from continuum.recovery.cleanup import cleanup_ephemeral_artifacts
from continuum.recovery.contract import (
    build_contract,
    render_contract,
    seal_contract,
    verify_contract,
)
from continuum.recovery.engine import SEVERITY, RecoveryDecision, RecoveryEngine
from continuum.recovery.gate import (
    EditPreconditionError,
    check_preconditions,
    summary_payload,
)
from continuum.recovery.impact import DependencyGraph, ImpactedSet
from continuum.recovery.ledger import (
    FileLedgerBackend,
    LedgerBackend,
    LedgerEntryKind,
    LedgerError,
    LedgerLockError,
    MemoryLedgerBackend,
    ReconcileReport,
    RecoveryLedger,
    RecoveryLedgerEntry,
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

__all__ = [
    "RecoveryTimeoutError",
    "DependedResult",
    "DerivationResult",
    "EditPoint",
    "EditPreconditionError",
    "FileLedgerBackend",
    "MergePreconditionError",
    "RestorePreconditionError",
    "approve_merge",
    "approve_restore",
    "check_preconditions",
    "cleanup_ephemeral_artifacts",
    "run_with_limits",
    "SEVERITY",
    "DependencyGraph",
    "summary_payload",
    "ImpactedSet",
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
    "seal_contract",
    "verify_contract",
]
