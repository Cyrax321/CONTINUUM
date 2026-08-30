"""Informed retry: what previous attempts did to this run (#265).

AgentRewind (arXiv:2608.14380) shows that a resumed agent which knows why the
previous attempt failed, an *informed retry*, outperforms a blind one across
models and harnesses. The recovery contract names what is blocked and the
single next permitted action; neither it nor the agent-authored reasoning
summary (#235) carries an engine's account of prior attempts. A fresh session
could therefore repeat, verbatim, the mistake that caused the rollback.

This module derives that account as a **pure projection** of facts already in
the hash chain:

- ``RECOVERY_STARTED``  repair plans recorded by ``resume --repair`` (#19)
- ``RECOVERY_COMPLETED`` / ``RECOVERY_BLOCKED``  how attempts ended
- ``ACTION_RECONCILED`` settlements of uncertain side effects (#45), whose
  ``status`` field encodes the verdict (COMPLETED means the effect was found,
  FAILED means absence was confirmed)
- ``ACTION_COMPENSATED`` compensating repairs recorded by the ledger

plus the current decision's own failure signals (non-VALID components,
uncertain actions). Nothing new is written: every input is tamper-evident
through the existing event chain, identical logs yield byte-identical blocks,
and a run with no recovery history yields ``None`` so today's output is
unchanged byte for byte.

The block is informational by contract: presence never gates, absence never
blocks, and size is capped with deterministic eviction when inputs are large.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from continuum.events import EventType
from continuum.models import Origin, StateStatus
from continuum.provenance_map import derived_provenance_for_events

if TYPE_CHECKING:
    from collections.abc import Sequence

    from continuum.models import Action, StateValidationResult
    from continuum.recovery.planner import RepairPlan
    from continuum.storage.base import Storage

__all__ = [
    "INFORMED_RETRY_CAP_BYTES",
    "ATTEMPT_LESSON_FIELD_CAP",
    "ATTEMPT_LESSON_TOTAL_CAP",
    "build_informed_retry",
    "render_informed_retry",
    "build_attempt_lesson",
    "render_attempt_lesson",
    "record_attempt_lesson",
]

#: Serialized blocks larger than this are shrunk deterministically rather
#: than allowed to grow with run history (same spirit as the #235 cap).
INFORMED_RETRY_CAP_BYTES = 4096

#: Per-field cap for AttemptLesson (issue #313) - 512 chars per field, 2KB total.
ATTEMPT_LESSON_FIELD_CAP = 512
ATTEMPT_LESSON_TOTAL_CAP = 2048

_MAX_STEPS = 8
_MAX_SETTLED = 5
_MAX_FAILURES = 8
_MAX_AVOID = 6


def build_informed_retry(
    storage: Storage,
    run_id: str,
    *,
    validation_report: StateValidationResult,
    uncertain_actions: Sequence[Action] = (),
    plan: RepairPlan | None = None,
) -> dict[str, Any] | None:
    """Derive the informed-retry block for ``run_id``, or ``None`` if clean.

    A run qualifies when its log holds any recovery-path event or any action
    settlement, or when the current assessment itself reports failures or
    uncertainty. Otherwise the return is ``None`` and callers must not change
    their output at all.
    """
    events = storage.read_events(run_id)
    starts = [e for e in events if e.type is EventType.RECOVERY_STARTED]
    completions = [e for e in events if e.type is EventType.RECOVERY_COMPLETED]
    blocked = [e for e in events if e.type is EventType.RECOVERY_BLOCKED]
    settled = [e for e in events if e.type is EventType.ACTION_RECONCILED]
    compensated = [e for e in events if e.type is EventType.ACTION_COMPENSATED]

    failures = [e for e in validation_report.statuses if e.status is not StateStatus.VALID]

    if not (
        starts or completions or blocked or settled or compensated or failures or uncertain_actions
    ):
        return None

    last_start = starts[-1].payload if starts else None

    def _step_name(step: Any) -> str:
        # Stored plans carry {kind, target}; the contract's action_name is
        # reconstructed the same way everywhere else it is displayed.
        named = step.get("action_name")
        if named:
            return str(named)
        kind = step.get("kind")
        target = step.get("target")
        if kind and target:
            return f"{kind}:{target}"
        return str(kind or "")

    last_steps = [_step_name(step) for step in ((last_start or {}).get("plan") or [])]
    last_steps = [s for s in last_steps if s][-_MAX_STEPS:]

    settled_entries = [
        {
            "action_id": str(e.payload.get("action_id", "")),
            "action_type": str(e.payload.get("action_type", "")),
            # The settlement's status IS the verdict: COMPLETED means the
            # effect was confirmed to exist, FAILED that absence was confirmed.
            "occurred": str(e.payload.get("status")) == "completed",
        }
        for e in settled[-_MAX_SETTLED:]
    ]

    current_failures = [
        {
            "component": entry.component.value,
            "id": entry.component_id or "",
            "status": entry.status.value,
            "detail": entry.detail or "",
        }
        for entry in failures[-_MAX_FAILURES:]
    ]

    avoid = _avoid_rules(failures, uncertain_actions)

    derived_origin = derived_provenance_for_events(events)
    block: dict[str, Any] = {
        "attempts": len(starts),
        "completed_recoveries": len(completions),
        "blocked_recoveries": len(blocked),
        "compensations": len(compensated),
        "last_attempt_mode": (last_start or {}).get("mode"),
        "last_attempt_steps": last_steps,
        "settled_effects": settled_entries,
        "current_failures": current_failures,
        "avoid": avoid,
        "derived_origin": derived_origin.value,
    }
    return _fit(block)


def _avoid_rules(failures: Sequence[Any], uncertain: Sequence[Action]) -> list[str]:
    """Deterministic one-line rules derived from failure kinds.

    These are generic on purpose: they restate what the validator and ledger
    already concluded, they never speculate about causes the signals do not
    contain.
    """
    rules: list[str] = []
    for entry in failures:
        name = (
            f"{entry.component.value}:{entry.component_id}"
            if entry.component_id
            else entry.component.value
        )
        component = entry.component.value
        if component == "external_dependency":
            rules.append(
                f"{name}: changed under the run; re-pin with --env before trusting cached results"
            )
        elif component in ("evidence", "finding"):
            rules.append(f"{name}: stale; re-derive from its source before citing it again")
        elif component == "goal":
            rules.append(f"{name}: no longer valid as recorded; restate it before continuing work")
        elif component == "model":
            rules.append(f"{name}: model assumptions unverified; confirm which model resumes")
        elif component == "approval":
            rules.append(f"{name}: approval does not carry over; obtain a fresh one")
        else:
            detail = f" ({entry.detail})" if entry.detail else ""
            rules.append(f"{name}: needs repair{detail}")
    for action in uncertain:
        rules.append(
            f"{action.action_type} ({action.action_id[:14]}): outcome unknown; "
            "reconcile before any retry"
        )
    return sorted(rules)[:_MAX_AVOID]


def _fit(block: dict[str, Any]) -> dict[str, Any]:
    """Shrink the block below the cap with deterministic eviction."""
    candidate = dict(block)
    while len(json.dumps(candidate, sort_keys=True).encode()) > INFORMED_RETRY_CAP_BYTES:
        if len(candidate["avoid"]) > _MAX_AVOID - 2 and _MAX_AVOID - 2 >= 0:
            candidate["avoid"] = candidate["avoid"][: max(_MAX_AVOID - 2, 0)]
        elif len(candidate["current_failures"]) > 1:
            candidate["current_failures"] = candidate["current_failures"][:-1]
        elif len(candidate["settled_effects"]) > 1:
            candidate["settled_effects"] = candidate["settled_effects"][:-1]
        elif len(candidate["last_attempt_steps"]) > 1:
            candidate["last_attempt_steps"] = candidate["last_attempt_steps"][:-1]
        elif any(f.get("detail") for f in candidate["current_failures"]):
            candidate["current_failures"] = [
                {**f, "detail": ""} for f in candidate["current_failures"]
            ]
        else:
            # Nothing left to trim without emptying the block; hard-stop with
            # whatever fits least badly. Unreachable for realistic inputs.
            break
    return candidate


def _derived_label(block: dict[str, Any]) -> str | None:
    raw = block.get("derived_origin")
    if raw is None:
        return "unverified (derived from unverified sources)"
    try:
        origin = Origin(raw)
    except ValueError:
        return "unverified (derived from unverified sources)"
    if origin.self_certified:
        return f"unverified (derived from {origin.value})"
    return f"derived from {origin.value}"


def _truncate(text: str, cap: int = ATTEMPT_LESSON_FIELD_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[:cap]


def _lesson_total_bytes(lesson: Any) -> int:
    import json

    return len(json.dumps(lesson.model_dump(mode="json"), sort_keys=True).encode())


def build_attempt_lesson(
    decision: Any,
    *,
    uncertain_actions: Any | None = None,
    ledger_entries: Any | None = None,
    now: Any | None = None,
) -> Any:
    from continuum.security.hashing import stable_hash

    run_id = getattr(decision, "run_id", "unknown")
    rationale = list(getattr(decision, "rationale", []) or [])
    scars = (
        uncertain_actions
        if uncertain_actions is not None
        else getattr(decision, "uncertain_actions", [])
    )
    scars = list(scars or [])
    validation = getattr(decision, "validation", None)
    report = getattr(validation, "report", None) if validation else None
    statuses = list(getattr(report, "statuses", []) or []) if report else []
    source_evidence = [
        str(getattr(entry, "detail", "") or "")
        for entry in statuses
        if getattr(entry, "detail", "")
    ]
    source_evidence = [s for s in source_evidence if s]
    falsified_raw = str(rationale[0]) if rationale else "attempt did not achieve its goal"
    falsified = _truncate(falsified_raw.strip(), ATTEMPT_LESSON_FIELD_CAP)
    env_delta = ""
    for entry in statuses:
        component = getattr(getattr(entry, "component", None), "value", "")
        if component == "external_dependency":
            env_delta = _truncate(str(getattr(entry, "detail", "") or ""), ATTEMPT_LESSON_FIELD_CAP)
            break
    if not env_delta:
        for entry in statuses:
            detail = str(getattr(entry, "detail", "") or "")
            if detail:
                env_delta = _truncate(detail, ATTEMPT_LESSON_FIELD_CAP)
                break
    scar_ids = [str(getattr(action, "action_id", "") or "") for action in scars]
    scar_ids = [sid for sid in scar_ids if sid]
    from continuum.recovery.summary import _avoid_rules

    avoid_rules = _avoid_rules(statuses, scars)
    next_avoid = _truncate(str(avoid_rules[0]) if avoid_rules else "", ATTEMPT_LESSON_FIELD_CAP)
    source_evidence = [_truncate(str(s), ATTEMPT_LESSON_FIELD_CAP) for s in source_evidence[:8]]
    ledger_len = len(list(ledger_entries or []))
    attempt_id_raw = stable_hash(
        {
            "run_id": str(run_id),
            "rationale": sorted(rationale),
            "scars": sorted(scar_ids),
            "ledger_len": ledger_len,
        }
    )
    attempt_id = _truncate(attempt_id_raw[:16], ATTEMPT_LESSON_FIELD_CAP)
    if not attempt_id:
        attempt_id = "attempt_1"
    created = (
        now if now is not None else __import__("continuum.models", fromlist=["utcnow"]).utcnow()
    )
    lesson = __import__("continuum.models", fromlist=["AttemptLesson"]).AttemptLesson(
        attempt_id=attempt_id,
        falsified=falsified,
        env_delta=env_delta,
        scar_action_ids=scar_ids[:16],
        next_avoid=next_avoid,
        source_evidence=source_evidence,
        created_at=created,
    )
    while _lesson_total_bytes(lesson) > 2048:
        fields = ["falsified", "env_delta", "next_avoid"]
        largest = max(fields, key=lambda name: len(getattr(lesson, name)))
        current = getattr(lesson, largest)
        if not current:
            if lesson.source_evidence:
                trimmed = list(lesson.source_evidence)
                trimmed = (
                    trimmed[:-1]
                    if len(trimmed) > 1
                    else [trimmed[0][: max(len(trimmed[0]) // 2, 0)]]
                )
                lesson = lesson.model_copy(update={"source_evidence": trimmed})
                continue
            break
        truncated = current[: max(len(current) // 2, 0)]
        lesson = lesson.model_copy(update={largest: truncated})
        if _lesson_total_bytes(lesson) <= 2048:
            break
        if all(len(getattr(lesson, name)) == 0 for name in fields) and not lesson.source_evidence:
            break
    return lesson


def render_attempt_lesson(lesson: Any) -> list[str]:
    if isinstance(lesson, dict):
        attempt_id = str(lesson.get("attempt_id", ""))
        falsified = str(lesson.get("falsified", ""))
        env_delta = str(lesson.get("env_delta", ""))
        scars = list(lesson.get("scar_action_ids", []) or [])
        next_avoid = str(lesson.get("next_avoid", ""))
        evidence = list(lesson.get("source_evidence", []) or [])
    else:
        attempt_id = str(getattr(lesson, "attempt_id", ""))
        falsified = str(getattr(lesson, "falsified", ""))
        env_delta = str(getattr(lesson, "env_delta", ""))
        scars = list(getattr(lesson, "scar_action_ids", []) or [])
        next_avoid = str(getattr(lesson, "next_avoid", ""))
        evidence = list(getattr(lesson, "source_evidence", []) or [])
    lines = []
    if attempt_id:
        lines.append(f"attempt {attempt_id}: {falsified}" if falsified else f"attempt {attempt_id}")
    elif falsified:
        lines.append(f"falsified: {falsified}")
    if env_delta:
        lines.append(f"env delta: {env_delta}")
    if scars:
        lines.append(f"scar actions: {', '.join(scars[:5])}")
    if next_avoid:
        lines.append(f"next avoid: {next_avoid}")
    for ev in evidence[:3]:
        lines.append(f"evidence: {ev}")
    return lines


def record_attempt_lesson(
    storage: Any,
    run_id: str,
    decision: Any,
    *,
    uncertain_actions: Any | None = None,
    ledger_entries: Any | None = None,
) -> Any:
    lesson = build_attempt_lesson(
        decision, uncertain_actions=uncertain_actions, ledger_entries=ledger_entries
    )
    storage.append_event(
        run_id,
        __import__("continuum.events", fromlist=["EventType"]).EventType.ATTEMPT_LESSON,
        lesson.model_dump(mode="json"),
        source=__import__("continuum.models", fromlist=["Origin"]).Origin.DETERMINISTIC,
    )
    return lesson


def render_informed_retry(block: dict[str, Any]) -> list[str]:
    """Human/agent-readable lines for briefing and resume output."""
    lines: list[str] = []
    label = _derived_label(block)
    if label:
        lines.append(f"provenance: {label}")
    attempts = block.get("attempts", 0)
    if attempts:
        lines.append(f"previous attempt(s): {attempts}")
        mode = block.get("last_attempt_mode")
        steps = block.get("last_attempt_steps") or []
        if mode:
            lines.append(f"  last plan mode: {mode}")
        for step in steps:
            lines.append(f"  planned then: {step}")
    for key, label in (
        ("completed_recoveries", "recoveries completed"),
        ("blocked_recoveries", "recoveries blocked"),
        ("compensations", "compensations applied"),
    ):
        if block.get(key):
            lines.append(f"{label}: {block[key]}")
    for s in block.get("settled_effects", []):
        verdict = "effect confirmed present" if s.get("occurred") else "absence confirmed"
        lines.append(f"settled {s.get('action_type')}: {verdict}")
    for f in block.get("current_failures", []):
        name = f"{f['component']}:{f['id']}" if f.get("id") else f["component"]
        detail = f" ({f['detail']})" if f.get("detail") else ""
        lines.append(f"still failing: {name}{detail}")
    for rule in block.get("avoid", []):
        lines.append(f"avoid repeating: {rule}")
    return lines
