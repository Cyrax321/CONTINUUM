"""Compatibility shim for cadence contracts (issue #302).

Canonical implementation lives in ``continuum.recovery.health``. This module
re-exports the public symbols so existing imports from ``continuum.liveness``
continue to work.
"""

from continuum.recovery.health import (  # noqa: F401
    DEFAULT_LIVENESS_PATH,
    DEFAULT_MAX_SILENCE_SECONDS,
    DEFAULT_PHASE_SCOPES,
    CadenceContract,
    LivenessResult,
    advisory_for_storage,
    advisory_text,
    evaluate,
    load_cadence_contract,
    silence_seconds,
)

__all__ = [
    "CadenceContract",
    "LivenessResult",
    "DEFAULT_LIVENESS_PATH",
    "DEFAULT_MAX_SILENCE_SECONDS",
    "DEFAULT_PHASE_SCOPES",
    "evaluate",
    "load_cadence_contract",
    "silence_seconds",
    "advisory_for_storage",
    "advisory_text",
]
