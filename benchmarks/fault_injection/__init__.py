"""Fault-injection chaos suite for #397.

Public API for the fault-injection benchmark. The suite injects
schema-valid semantic faults into real runs and measures whether the
verification layer detects them.
"""

from .emitter import emit_fault_injection_report
from .faults import FAULT_CLASSES, FaultClass
from .runner import run_fault_injection_suite

__all__ = [
    "FAULT_CLASSES",
    "FaultClass",
    "emit_fault_injection_report",
    "run_fault_injection_suite",
]
