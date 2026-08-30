"""Fault corpus definitions for #397.

Each fault class is schema-valid but semantically corrupt. The injector
produces a real run, then mutates it through the fault's injection
function. The runner then asks the recovery engine to assess the run and
checks that the contract names the expected detection module.

Fault classes are deterministic: the same run + fault always produces the
same corrupted state, so the suite is replayable and diffable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultClass:
    """One injectable semantic fault."""

    name: str
    description: str
    # Module that must appear in the contract's invalidated/notes when caught.
    # For example, "continuum.state.validator" or "continuum.recovery.engine".
    expected_detection_module: str
    # Whether this fault should make unsafe resume impossible (target 0 unsafe-resume rate).
    should_block_resume: bool = True


# The fault corpus. Each entry is testable today and has a known detection
# module. Budget-bypass and dropped-pin classes are scaffolded but marked as
# blocked until their feature issues land; they are not yet included in the
# default corpus that CI runs.

FAULT_CLASSES: list[FaultClass] = [
    FaultClass(
        name="fabricated_progress",
        description="Forge high progress via MCP surface without evidence. The run claims 9/10 tasks complete but only 2 evidence items exist. Should be caught by the state validator (progress vs evidence) or recovery engine.",
        expected_detection_module="continuum.state.validator",
        should_block_resume=True,
    ),
    FaultClass(
        name="drifted_path_argument",
        description="Drift a file path argument in an action ledger entry from 'out/INV-001.pdf' to 'out/INV-001-drifted.pdf'. Should be caught by the action ledger's idempotency or environment diff.",
        expected_detection_module="continuum.actions.ledger",
        should_block_resume=True,
    ),
    FaultClass(
        name="tampered_history",
        description="Tamper with event history payload and reseal to look valid. Modify an evidence summary and recompute the chain hash naively. Should be caught by event chain verification (integrity) or recovery ledger.",
        expected_detection_module="continuum.storage.base",
        should_block_resume=True,
    ),
    FaultClass(
        name="dropped_constraint",
        description="Delete constraint pins during reconstruction. Declare a constraint pin, then remove it from the store before assessment. Should be caught by constraint pinning verification.",
        expected_detection_module="continuum.state.semantic",
        should_block_resume=True,
    ),
    FaultClass(
        name="laundered_lesson",
        description="Derive a lesson purely from external-agent events without deterministic evidence. Should be caught by the provenance check or recovery contract.",
        expected_detection_module="continuum.recovery.contract",
        should_block_resume=True,
    ),
    FaultClass(
        name="fresh_key_reissuance",
        description="Loop fresh idempotency keys for one authorization to bypass retry budget. Before #413 each fresh key opened a new bucket and the Nth claim passed; after, N fresh keys for same invoice exhaust one bucket and Nth is refused with budget exhausted naming authorization_id (issue #415, epic #390).",
        expected_detection_module="continuum.budgets",
        should_block_resume=False,
    ),
    FaultClass(
        name="unsafe_edit",
        description="Checkpoint mid-run after ActionLedger.claim, then restore and merge that skips past the unsettled claim. Before #389 the same restore/merge would have passed and silently dropped the outside-world uncertainty; after, the shared gate (continuum.recovery.gate) refuses naming the action id and suggesting reconcile or carry-forward. Covers fork, restore and merge (issue #410, epic #389).",
        expected_detection_module="continuum.recovery.gate",
        should_block_resume=True,
    ),
]

# Quick lookup
FAULT_BY_NAME: dict[str, FaultClass] = {f.name: f for f in FAULT_CLASSES}

# CI corpus: classes testable today without feature gates.
# Scaffold originally had three reliably detectable classes; after #390
# the fresh-key reissuance class is also testable (authorization-bound
# budgets at 4a4d76e). After #391 (pinning) the dropped-constraint class
# is testable via hash-tagged markers and grace escalation. Laundered
# lesson remains scaffolded until its feature lands.
CI_FAULTS: list[FaultClass] = [
    f
    for f in FAULT_CLASSES
    if f.name
    in {
        "fabricated_progress",
        "drifted_path_argument",
        "tampered_history",
        "fresh_key_reissuance",
        "dropped_constraint",
        "unsafe_edit",
    }
]
