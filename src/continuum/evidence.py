"""Built-in evidence probes: settle uncertainty from observation, not memory (issue #268).

An action left STARTED by a crash between intercept and complete is the
ledger's most expensive state: resume blocks until a human reconciles it or a
registered probe settles it from reality. Registering a probe per action type
used to mean writing a command by hand (``reconcilers.json``); the registry now
also accepts two built-in probe types that need no external script:

``otel_span``
    A tool-call span recorded by :mod:`continuum.otel` (or by ``observe``, or by
    any hook) already lands in the event log as a ``TOOL_COMPLETED`` /
    ``TOOL_FAILED`` observation. This probe finds one that matches the claim's
    action type and identity tokens *after the claim sequence*, and treats it as
    evidence. The settlement cites the span and event ids, so the chain from
    "settled" back to "what the trace said" is auditable.

``artifact_check``
    For path-scoped actions: did the claimed write actually land? Existence plus
    content digest of a file, reusing the environment snapshot primitives. A
    missing file is evidence of absence, which is what a retry needs.

Both are :class:`~continuum.actions.reconciliation.Reconciler` instances, so
they compose with the existing strategies and dispatch through the same
``reconcile_pending`` / ``settle_run`` paths.

Safety posture, unchanged from the command probes:

- A definitive verdict settles; anything the probe cannot decide stays pending
  (or, in strict mode, escalates to ``REQUIRES_REVIEW``).
- A span that ended in error is neither evidence of occurrence nor clean
  evidence of absence: it returns ``None`` and keeps the human queue intact,
  because turning a failed span into "occurred=False" would license a retry
  against a write that may have landed partially.
- :func:`detect_discrepancies` cross-checks the ledger against the filesystem
  after probing and escalates contradictions to review rather than picking a
  side. Settling and contradicting are separate passes on purpose.
- eBPF collectors (Tetragon, AgentSight) stay out of process. We consume their
  standard JSON through a small adapter that speaks the probe envelope; no BPF
  program ships with CONTINUUM. See ``docs/guides/evidence-reconciliation.md``.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from continuum.actions.reconciliation import Reconciler, Resolution
from continuum.environment.file_snapshot import file_digest
from continuum.events import Event, EventType
from continuum.models import Action, ActionStatus
from continuum.storage.base import Storage

__all__ = [
    "OtelSpanReconciler",
    "ArtifactCheckReconciler",
    "Discrepancy",
    "detect_discrepancies",
    "build_probe",
    "resolve_artifact_path",
    "ebpf_collector_available",
    "OBSERVATION_TOOL_KEYS",
]

#: Attribute keys on a recorded tool observation that identify *which* write it
#: was, in the order the OTel bridge and the hooks extract them. Used to match
#: an observation to the action that claimed it.
OBSERVATION_TOOL_KEYS = ("tool",)

#: Default identity tokens: action-argument keys that must agree with the
#: observation for the span to be about *this* claim rather than a sibling.
_DEFAULT_IDENTITY_KEYS = ("path",)


def _claim_sequence(events: Iterable[Event], action_id: str) -> int | None:
    """Sequence at which ``action_id`` was claimed, or None when not found.

    The fold does not carry sequence numbers, so the claim is located by
    scanning for its ACTION_RECORDED event. Full history: a pending action
    claimed before a compaction has its claim in the archived prefix, and a
    live-only scan would make every one of them look unclaimed (issue #647).
    """
    for event in events:
        if event.type is EventType.ACTION_RECORDED and event.payload.get("action_id") == action_id:
            return event.sequence
    return None


class OtelSpanReconciler(Reconciler):
    """Settles an uncertain action from a recorded tool-call span.

    Looks through the run's observations for a tool span that names the same
    tool as the action's type, agrees on the identity tokens, and was recorded
    after the claim. A matching span that completed is evidence the effect
    landed; a matching span that failed is not evidence of anything decisive
    (see the module docstring) and returns ``None``.
    """

    name = "otel_span"

    def __init__(
        self,
        storage: Storage,
        run_id: str,
        *,
        tool: str | None = None,
        identity_keys: tuple[str, ...] = _DEFAULT_IDENTITY_KEYS,
    ) -> None:
        self._storage = storage
        self._run_id = run_id
        self._tool = tool
        self._identity_keys = identity_keys

    def _matches(self, action: Action, payload: Mapping[str, Any]) -> bool:
        tool = self._tool or action.action_type
        if payload.get("tool") != tool:
            return False
        # Only tokens present on both sides constrain the match; an action with
        # no path to compare must not match every write the tool ever made.
        agreed = 0
        for key in self._identity_keys:
            claimed = action.arguments.get(key)
            observed = payload.get(key)
            if claimed is None or observed is None:
                continue
            if claimed != observed:
                return False
            agreed += 1
        return True

    def resolve(self, action: Action) -> Resolution | None:
        """Find a matching completed span after the claim and settle from it.

        Returns ``None`` when no span matches, and also when the only matching
        span failed: a failed tool call is not proof the side effect did not
        happen, so it must not license a retry on its own.
        """
        events = list(self._storage.read_all_events(self._run_id))
        claim_at = _claim_sequence(events, action.action_id)
        if claim_at is None:
            # The claim event is the thing being reconciled; without it there is
            # no "after the claim" to search and no provenance to cite.
            return None
        for event in events:
            if event.sequence <= claim_at:
                continue
            if event.type not in (EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
                continue
            if not self._matches(action, event.payload):
                continue
            if event.type is EventType.TOOL_FAILED:
                return None
            citation = self._citation(event)
            return Resolution(
                occurred=True,
                external_id=(event.payload.get("external_id") or event.event_id),
                result=dict(event.payload),
                note=f"settled from {citation}",
            )
        return None

    @staticmethod
    def _citation(event: Any) -> str:
        span_id = event.payload.get("span_id")
        trace_id = event.payload.get("trace_id")
        parts = [f"event {event.event_id} (seq {event.sequence})"]
        if span_id:
            parts.append(f"span {span_id}")
        if trace_id:
            parts.append(f"trace {trace_id}")
        return " ".join(parts)


class ArtifactCheckReconciler(Reconciler):
    """Settles a path-scoped action by checking whether its artifact exists.

    ``path`` is a literal, or ``path_key`` names the action argument carrying
    the path, so one registry entry serves every target the action writes.
    Existence plus a SHA-256 digest is the evidence; a missing file is evidence
    of absence. When ``expect_sha256`` is given and the present file does not
    match it, the artifact is somebody else's write, so the probe declines
    rather than attributing it to this action.
    """

    name = "artifact_check"

    def __init__(
        self,
        path: str | None = None,
        *,
        path_key: str | None = None,
        expect_sha256: str | None = None,
    ) -> None:
        self._path = path
        self._path_key = path_key
        self._expect_sha256 = expect_sha256

    def resolve(self, action: Action) -> Resolution | None:
        """Check the filesystem for the action's artifact and settle from it."""
        target = resolve_artifact_path({"path": self._path, "path_key": self._path_key}, action)
        if target is None:
            return None
        digest = file_digest(target)
        if digest is None:
            return Resolution(
                occurred=False,
                note=f"artifact absent at {target}",
            )
        if self._expect_sha256 is not None and digest != self._expect_sha256:
            # The file exists but is not this action's write: an earlier run, a
            # different tool, or a partial write. Attribute it to nobody.
            return None
        stat = _stat(target)
        return Resolution(
            occurred=True,
            external_id=digest,
            result={"path": target, "sha256": digest, **(stat or {})},
            note=f"artifact present at {target} (sha256 {digest[:12]})",
        )


def resolve_artifact_path(spec: Mapping[str, Any], action: Action) -> str | None:
    """Resolve the path an artifact_check applies to, from the spec or the action.

    A literal ``path`` wins; otherwise ``path_key`` selects an action argument.
    Returns ``None`` when neither yields a usable string.
    """
    literal = spec.get("path")
    if isinstance(literal, str) and literal:
        return literal
    key = spec.get("path_key")
    if isinstance(key, str) and key:
        value = action.arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _stat(path: str) -> dict[str, Any] | None:
    """Size and mtime of a file, as observation attributes. None on error."""
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return {"bytes": info.st_size, "mtime": datetime.fromtimestamp(info.st_mtime).isoformat()}


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """A place where the ledger and the observed world disagree."""

    action_id: str
    action_type: str
    kind: str
    """``completed_but_absent`` or ``present_but_unrecorded``."""

    detail: str

    def as_dict(self) -> dict[str, Any]:
        """Render as plain data for JSON output."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "kind": self.kind,
            "detail": self.detail,
        }


def detect_discrepancies(
    storage: Storage,
    run_id: str,
    probes: Mapping[str, Mapping[str, Any]],
    *,
    flag: bool = True,
) -> list[Discrepancy]:
    """Cross-check settled actions against the filesystem and escalate gaps.

    Two contradictions are looked for, both only for action types carrying an
    ``artifact_check`` probe, because those are the ones with an independent
    reality to check against:

    - the ledger says COMPLETED but the artifact is missing (the completion is
      unsupported, and re-settling it false would discard work, so review it);
    - the artifact exists but the ledger never recorded completion (an effect
      outside the log, which no automatic settlement should silently adopt).

    When ``flag`` is true each finding is written through
    :meth:`ActionLedger.flag_for_review`, which sets ``REQUIRES_REVIEW``. The
    write is the point: a finding nobody recorded cannot block a resume.
    """
    from continuum.actions import ActionLedger

    findings: list[tuple[str, Discrepancy]] = []
    ledger = ActionLedger(storage, run_id)
    for key, action in ledger.folded().items():
        spec = probes.get(action.action_type)
        if spec is None or spec.get("type") != ArtifactCheckReconciler.name:
            continue
        probe = ArtifactCheckReconciler(
            spec.get("path"),
            path_key=spec.get("path_key"),
            expect_sha256=spec.get("expect_sha256"),
        )
        resolution = probe.resolve(action)
        if resolution is None:
            # Unattributable: no path resolvable, or a digest mismatch means the
            # file is not this action's write. Neither side can be contradicted.
            continue
        target = resolve_artifact_path(spec, action)
        label = target if target is not None else action.action_type
        present = resolution.occurred
        if action.status is ActionStatus.COMPLETED and not present:
            findings.append(
                (
                    key,
                    Discrepancy(
                        action.action_id,
                        action.action_type,
                        "completed_but_absent",
                        f"ledger records {action.action_type} completed but {label} is missing",
                    ),
                )
            )
        elif action.status is not ActionStatus.COMPLETED and present:
            findings.append(
                (
                    key,
                    Discrepancy(
                        action.action_id,
                        action.action_type,
                        "present_but_unrecorded",
                        f"{label} exists but the ledger never recorded "
                        f"{action.action_type} completed",
                    ),
                )
            )
    if flag:
        for key, finding in findings:
            ledger.flag_for_review(key, finding.detail)
    return [finding for _, finding in findings]


def build_probe(spec: Mapping[str, Any], storage: Storage, run_id: str) -> Reconciler:
    """Build one of the built-in evidence probes from a registry spec.

    Only handles the built-in types; the command probe is wrapped where the
    registry is parsed (``continuum.reconcilers``), so this function stays free
    of the subprocess dependency and importable from there without a cycle.
    """
    kind = spec.get("type")
    if kind == OtelSpanReconciler.name:
        return OtelSpanReconciler(
            storage,
            run_id,
            tool=spec.get("tool"),
            identity_keys=tuple(spec.get("identity", _DEFAULT_IDENTITY_KEYS)),
        )
    if kind == ArtifactCheckReconciler.name:
        return ArtifactCheckReconciler(
            spec.get("path"),
            path_key=spec.get("path_key"),
            expect_sha256=spec.get("expect_sha256"),
        )
    raise ReconcilerConfigError(f"unknown probe type {kind!r}")


class ReconcilerConfigError(ValueError):
    """A built-in probe spec cannot be honoured."""


def ebpf_collector_available() -> bool:
    """True when an out-of-process eBPF collector appears usable.

    Linux-only by construction: Tetragon and AgentSight are eBPF tooling, and
    ``shutil.which`` returns ``None`` for a binary that is not on PATH. Tests
    that exercise a real collector skip on a False return, so CI on a machine
    without one stays green without a collector being faked (issue #268 AC4).
    """
    if sys.platform == "win32" or sys.platform == "darwin":
        return False
    return any(shutil.which(name) is not None for name in ("tetra", "agentsight"))
