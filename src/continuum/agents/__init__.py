"""Instruction targets: one source, rendered into every IDE's filename.

Coding IDEs with no hook surface still read instructions, and each reads a
different file. This package renders all of them from
:data:`continuum.agents.generator.DEFAULT_SOURCE` so the guidance cannot drift
between copies, refuses to overwrite a file a human wrote, and can prove a
committed copy is still current.

:mod:`continuum.agents.doctor` sits on top of it and answers the wider
question: not just whether the instruction files are right, but whether
CONTINUUM works at all in the IDE you are sitting in.
"""

from __future__ import annotations

from .generator import (
    DEFAULT_SOURCE,
    REGENERATE_COMMAND,
    CheckResult,
    InstallResult,
    check,
    install,
    remove,
    resolve_source,
)
from .render import GENERATED_MARKER, Fingerprint, fingerprint_of, render, source_digest
from .targets import TARGET_IDS, TARGETS, InstructionTarget, target_for

__all__ = [
    "DEFAULT_SOURCE",
    "GENERATED_MARKER",
    "REGENERATE_COMMAND",
    "TARGET_IDS",
    "TARGETS",
    "CheckResult",
    "Fingerprint",
    "InstructionTarget",
    "InstallResult",
    "check",
    "fingerprint_of",
    "install",
    "remove",
    "render",
    "resolve_source",
    "source_digest",
    "target_for",
]
