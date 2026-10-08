"""Declarative risk policy schema and parser (issue #1422).

Maps real-time failure triggers to recovery actions with fail-closed validation.
Defaults follow the arXiv:2608.02464 table and operators may only make actions
more conservative. Attempts to downgrade severe triggers (such as
side_effect_duplicate or meltdown) below baseline safe thresholds are rejected.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from continuum.models import RecoveryMode

__all__ = [
    "BASELINE_RISK_POLICY",
    "DEFAULT_RISK_POLICY",
    "DEFAULT_RISK_POLICY_PATH",
    "KNOWN_TRIGGERS",
    "RISK_POLICY_SCHEMA",
    "RiskPolicy",
    "RiskPolicyError",
    "RiskPolicySchema",
    "evaluate_risk",
    "evaluate_risk_action",
    "is_more_conservative",
    "load_risk_policy",
]

DEFAULT_RISK_POLICY_PATH = Path(".continuum/risk-policy.json")

#: Default declarative risk policy mappings (issue #1422).
DEFAULT_RISK_POLICY: dict[str, str] = {
    "loop": RecoveryMode.REPLAN.value,
    "loop_persisting": RecoveryMode.ROLLBACK.value,
    "error_cascade": RecoveryMode.WAIT.value,
    "latency_anomaly": "annotate",
    "token_runaway": RecoveryMode.WAIT.value,
    "silent_abort": RecoveryMode.REPAIR_AND_RESUME.value,
    "meltdown": RecoveryMode.ROLLBACK.value,
    "side_effect_duplicate": RecoveryMode.ABORT.value,
    "governance_decay": RecoveryMode.REQUEST_HUMAN.value,
}

#: Baseline conservative thresholds. Operators may only make actions more
#: conservative than these safe baseline defaults.
BASELINE_RISK_POLICY: dict[str, str] = dict(DEFAULT_RISK_POLICY)

#: Canonical set of recognised trigger names under the declarative policy.
KNOWN_TRIGGERS: frozenset[str] = frozenset(DEFAULT_RISK_POLICY.keys())

#: Set of valid policy modes, including 'annotate' (watch-without-action).
_VALID_POLICY_MODES: frozenset[str] = frozenset({m.value for m in RecoveryMode} | {"annotate"})

#: Ascending caution severity ordering for conservative checks.
_SEVERITY_ORDER: dict[str, int] = {
    "annotate": -1,
    RecoveryMode.RESUME.value: 0,
    RecoveryMode.REPAIR_AND_RESUME.value: 1,
    RecoveryMode.REPLAN.value: 2,
    RecoveryMode.WAIT.value: 3,
    RecoveryMode.REQUEST_HUMAN.value: 4,
    RecoveryMode.ROLLBACK.value: 5,
    RecoveryMode.ABORT.value: 6,
}

#: Metadata keys permitted in risk policy JSON configurations.
_ALLOWED_META_KEYS: frozenset[str] = frozenset({"$schema", "token_runaway_threshold"})


class RiskPolicyError(ValueError):
    """Raised when a declarative risk policy exists but fails validation."""

    pass


def is_more_conservative(new_mode: str, old_mode: str) -> bool:
    """Whether new_mode is at least as severe (conservative) as old_mode."""
    if old_mode == "annotate":
        return True
    if new_mode == "annotate":
        return False
    new_sev = _SEVERITY_ORDER.get(new_mode)
    old_sev = _SEVERITY_ORDER.get(old_mode)
    if new_sev is not None and old_sev is not None:
        return new_sev >= old_sev
    try:
        from continuum.recovery.engine import SEVERITY

        new_m = RecoveryMode(new_mode)
        old_m = RecoveryMode(old_mode)
        return SEVERITY.get(new_m, 0) >= SEVERITY.get(old_m, 0)
    except Exception:
        return False


class RiskPolicySchema(BaseModel):
    """Strict JSON schema specification for .continuum/risk-policy.json."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    schema_url: str | None = Field(
        default=None,
        alias="$schema",
        description="Optional URI to the JSON Schema definition.",
    )
    loop: str = Field(
        default=RecoveryMode.REPLAN.value,
        description="Recovery mode for execution loop anomalies.",
    )
    loop_persisting: str = Field(
        default=RecoveryMode.ROLLBACK.value,
        description="Recovery mode for persistent loop anomalies.",
    )
    error_cascade: str = Field(
        default=RecoveryMode.WAIT.value,
        description="Recovery mode for cascading errors.",
    )
    latency_anomaly: str = Field(
        default="annotate",
        description="Recovery mode for latency anomalies (annotate only).",
    )
    token_runaway: str = Field(
        default=RecoveryMode.WAIT.value,
        description="Base recovery mode for token runaway anomalies.",
    )
    token_runaway_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Confidence score threshold for token runaway escalation to abort.",
    )
    silent_abort: str = Field(
        default=RecoveryMode.REPAIR_AND_RESUME.value,
        description="Recovery mode for silent abort anomalies.",
    )
    meltdown: str = Field(
        default=RecoveryMode.ROLLBACK.value,
        description="Recovery mode for unhandled meltdown anomalies.",
    )
    side_effect_duplicate: str = Field(
        default=RecoveryMode.ABORT.value,
        description="Recovery mode for duplicate side-effect execution.",
    )
    governance_decay: str = Field(
        default=RecoveryMode.REQUEST_HUMAN.value,
        description="Recovery mode for governance decay anomalies.",
    )


#: Generated JSON Schema dict for .continuum/risk-policy.json.
RISK_POLICY_SCHEMA: dict[str, Any] = RiskPolicySchema.model_json_schema()


class RiskPolicy(dict):  # type: ignore[type-arg]
    """Validated risk policy mapping supporting dictionary access and thresholds."""

    token_runaway_threshold: float

    def __init__(
        self,
        mapping: Mapping[str, str] | None = None,
        *,
        token_runaway_threshold: float = 0.8,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.token_runaway_threshold = float(token_runaway_threshold)
        if mapping:
            for k, v in mapping.items():
                if k in KNOWN_TRIGGERS:
                    self[k.strip().lower()] = v
        for k, v in kwargs.items():
            if k == "token_runaway_threshold":
                self.token_runaway_threshold = float(v)
            elif k in KNOWN_TRIGGERS:
                self[k.strip().lower()] = v
        # Ensure all baseline triggers are present with conservative defaults
        for dk, dv in DEFAULT_RISK_POLICY.items():
            if dk not in self:
                self[dk] = dv

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Return the strict JSON Schema for risk-policy.json."""
        return RiskPolicySchema.model_json_schema()

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary copy of the policy mapping and threshold."""
        res: dict[str, Any] = dict(self)
        res["token_runaway_threshold"] = self.token_runaway_threshold
        return res


def load_risk_policy(path: Path | str | None = None) -> RiskPolicy:
    """Load risk policy from path or default location, with fail-closed validation.

    Safe defaults: if configuration file is absent, conservative defaults load
    automatically.

    Fail-closed validation: rejects invalid JSON, non-object roots, unknown triggers,
    invalid modes, and attempts to downgrade severe triggers below baseline
    safe thresholds.
    """
    target = Path(path) if path is not None else DEFAULT_RISK_POLICY_PATH
    if not target.exists():
        return RiskPolicy(DEFAULT_RISK_POLICY)

    location = str(target.resolve())
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RiskPolicyError(f"invalid risk policy {location}: {exc}") from exc
    except Exception as exc:
        raise RiskPolicyError(f"invalid risk policy {location}: {exc}") from exc

    if not isinstance(data, Mapping):
        raise RiskPolicyError(f"risk policy {location} must be a JSON object")

    result: dict[str, str] = {}
    threshold = 0.8

    for k, v in data.items():
        if not isinstance(k, str) or not k.strip():
            raise RiskPolicyError("risk policy keys must be non-empty strings")
        clean_key = k.strip()

        # Handle metadata keys
        if clean_key == "$schema":
            continue
        if clean_key == "token_runaway_threshold":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise RiskPolicyError(
                    f"token_runaway_threshold must be a number in [0.0, 1.0], got {v!r}"
                )
            if not 0.0 <= float(v) <= 1.0:
                raise RiskPolicyError(
                    f"token_runaway_threshold must be within [0.0, 1.0], got {v!r}"
                )
            threshold = float(v)
            continue

        clean_lower = clean_key.lower()
        # Fail-closed: reject unknown triggers
        if clean_lower not in KNOWN_TRIGGERS:
            raise RiskPolicyError(
                f"unknown trigger {k!r} in risk policy {location}; "
                f"known triggers are {sorted(KNOWN_TRIGGERS)}"
            )

        # Fail-closed: reject invalid modes
        if not isinstance(v, str) or v not in _VALID_POLICY_MODES:
            raise RiskPolicyError(
                f"risk policy[{k!r}] must be one of {sorted(_VALID_POLICY_MODES)}, got {v!r}"
            )

        # Fail-closed: reject downgrades below baseline safe thresholds
        baseline_mode = BASELINE_RISK_POLICY.get(clean_lower)
        if baseline_mode is not None and not is_more_conservative(v, baseline_mode):
            raise RiskPolicyError(
                f"risk policy[{k!r}] downgrades {baseline_mode!r} to {v!r}; "
                f"operators may only make actions more conservative"
            )

        result[clean_lower] = v

    for dk, dv in DEFAULT_RISK_POLICY.items():
        if dk not in result:
            result[dk] = dv

    return RiskPolicy(result, token_runaway_threshold=threshold)


def evaluate_risk_action(
    trigger: str,
    score: float = 0.0,
    policy: RiskPolicy | dict[str, Any] | None = None,
) -> RecoveryMode | None:
    """Evaluate a trigger and confidence score against policy into a RecoveryMode.

    Returns None for annotate-only signals (such as latency_anomaly), unknown triggers,
    or blank trigger inputs.
    """
    if not isinstance(trigger, str) or not trigger.strip():
        return None

    key = trigger.strip().lower()
    pol = policy if policy is not None else DEFAULT_RISK_POLICY
    mode_val = pol.get(key)
    if mode_val is None or mode_val == "annotate":
        return None

    # Threshold-based escalation for token_runaway
    if key == "token_runaway":
        threshold = (
            getattr(pol, "token_runaway_threshold", None)
            if hasattr(pol, "token_runaway_threshold")
            else None
        )
        if threshold is None and isinstance(pol, Mapping):
            threshold = pol.get("token_runaway_threshold", 0.8)
        try:
            thresh_f = float(threshold) if threshold is not None else 0.8
        except (ValueError, TypeError):
            thresh_f = 0.8

        if score >= thresh_f:
            return RecoveryMode.ABORT

    try:
        return RecoveryMode(mode_val)
    except (ValueError, KeyError):
        return None


def evaluate_risk(
    trigger: str,
    policy: dict[str, str] | RiskPolicy | None = None,
) -> str | None:
    """Return the mode string for a trigger, or None for annotate/no-action."""
    if not isinstance(trigger, str) or not trigger.strip():
        return None
    pol = policy if policy is not None else DEFAULT_RISK_POLICY
    mode = pol.get(trigger.strip().lower())
    if mode is None or mode == "annotate":
        return None
    return mode
