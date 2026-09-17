"""Registered reconciliation probes for uncertain side effects (issue #218).

An uncertain action blocks resume until something settles it, and until now
that something was always a person. Most of the time the answer is
mechanically checkable ("is the invoice in the outbox?"), so this module lets
a project register one probe per action type:

    .continuum/reconcilers.json
    {"probes": {"send_invoice": {"command": "check-outbox", "timeout": 10}}}

Since issue #268 a probe is either a ``command`` (the shape above) or one of
the built-in evidence probes, selected by ``type``:

    {"probes": {
        "write_report": {"type": "artifact_check", "path_key": "path"},
        "tool.call":    {"type": "otel_span", "identity": ["path"]}
    }}

A spec without a ``type`` is a command probe, so an existing registry keeps
working unchanged. :func:`load_reconcilers` validates each type's required
keys and refuses unknown ones, because a typo in a probe spec should fail the
operator loudly rather than register a probe that silently does nothing.

The ``probes`` wrapper is the shape :func:`load_reconcilers` reads: a
non-empty file that maps action types at the top level instead is valid
JSON but the wrong shape, and is refused rather than silently registering
nothing (issue #1062). ``timeout`` is in seconds, optional, and defaults to
10 seconds; it must be a positive number, and a probe that outlives it is
an error rather than a verdict, so its action stays in the human queue
(issue #322).

A command probe receives the full Action record as JSON on stdin and prints
exactly one verdict on its last stdout line: ``occurred=true``,
``occurred=false`` or ``occurred=unknown`` (a JSON object with an
``occurred`` field also works, with true/false/null/unknown). The verdict is
applied through :meth:`ActionLedger.reconcile`, so it lands in the log like
any other reconciliation and is auditable there.

Provenance stays conservative and deliberately narrower than what the ledger
technically allows:

- A definitive probe verdict is settled automatically; the event is sourced
  ``DETERMINISTIC`` because a local, registered, auditable probe produced it,
  and the evidence that produced it (span ids, file digest) rides in the
  settlement's ``result`` and ``note``.
- Anything else, missing probe, non-zero exit, timeout, unparseable output,
  explicit unknown, leaves the action untouched and the human queue intact.
  In strict mode such an action escalates to ``REQUIRES_REVIEW`` instead, so
  a run that tolerates no uncertainty degrades to a human rather than to a
  silent retry.

Auto-settlement therefore only ever shrinks the set of things a person must
look at; it never widens what an agent may certify on its own.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from continuum.models import Action
from continuum.storage.base import Storage

if TYPE_CHECKING:
    pass

__all__ = [
    "DEFAULT_RECONCILERS_PATH",
    "ReconcilerConfigError",
    "load_reconcilers",
    "probe_verdict",
    "settle_run",
    "probe_authority_verdict",
    "settle_authority",
    "AuthoritySettleReport",
]

#: Where the registry lives relative to the project root a hook or CLI
#: invocation runs in. JSON, matching gate.json and mcp-policy.json.
DEFAULT_RECONCILERS_PATH = ".continuum/reconcilers.json"

_DEFAULT_TIMEOUT = 10.0


class ReconcilerConfigError(ValueError):
    """The reconciler registry exists but cannot be honoured."""


#: Keys each probe type accepts. ``type`` itself is stripped before storage.
_COMMAND_KEYS = {"command", "timeout"}
_BUILTIN_KEYS = {
    "otel_span": {"tool", "identity"},
    "artifact_check": {"path", "path_key", "expect_sha256"},
}


def load_reconcilers(path: Path) -> dict[str, dict[str, Any]]:
    """Read the registry. Empty dict when absent; raise when malformed.

    Every command spec carries a ``timeout``, 10 seconds when the file left it
    out, so a caller never has to supply the default itself. A ``timeout`` that
    is not a positive number is refused rather than clamped: zero or negative
    expires every probe the moment it starts, which would look like an external
    system nobody can reach instead of a registry typo (issue #322).

    A spec's ``type`` selects a command probe (the default when ``type`` is
    absent, and the only type that runs a subprocess) or one of the built-in
    evidence probes. Each type's required keys are checked, and keys it does not
    accept are refused: a probe spec with a typo'd key would otherwise register
    as a probe that never settles anything (issue #268).
    """
    if not path.exists():
        return {}
    # Absolute, so the message names a file the operator can open: the
    # relative form depends on the cwd of whatever loaded the registry
    # (a hook, the sidecar, a CI step). Matches gate.py per #333.
    location = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReconcilerConfigError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("probes", {}), dict):
        raise ReconcilerConfigError(f"{location}: expected {{'probes': {{...}}}}")
    if raw and "probes" not in raw:
        raise ReconcilerConfigError(
            f"{location}: expected {{'probes': {{...}}}}, found top-level keys "
            f"{sorted(raw)!r} instead, wrap them under a 'probes' key"
        )
    probes: dict[str, dict[str, Any]] = {}
    for action_type, spec in (raw.get("probes") or {}).items():
        if not isinstance(spec, dict):
            raise ReconcilerConfigError(f"{location}: probe {action_type!r} must be an object")
        kind = spec.get("type", "command")
        if kind == "command" and "command" not in spec:
            # A spec with neither a type nor a command registers a probe that
            # cannot run; name the omission instead of letting reconcile report
            # every action of this type as unprobed.
            raise ReconcilerConfigError(
                f"{location}: probe {action_type!r} needs a string 'command' or a 'type'"
            )
        if kind == "command":
            if not isinstance(spec.get("command"), str):
                raise ReconcilerConfigError(
                    f"{location}: probe {action_type!r} needs a string 'command'"
                )
            timeout = spec.get("timeout", _DEFAULT_TIMEOUT)
            # ``bool`` subclasses ``int``, so a JSON ``true`` clears the numeric
            # check and registers as a one second timeout. Every probe would then
            # be killed just after it starts and its action would reach the human
            # queue carrying a timeout detail, rather than the config error this
            # arm promises.
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ReconcilerConfigError(
                    f"{path}: probe {action_type!r} 'timeout' must be a positive number"
                )
            probes[action_type] = {
                "command": spec["command"],
                "timeout": float(timeout),
            }
            continue
        allowed = _BUILTIN_KEYS.get(kind)
        if allowed is None:
            raise ReconcilerConfigError(
                f"{location}: probe {action_type!r} has unknown type {kind!r} "
                f"(expected command, {', '.join(sorted(_BUILTIN_KEYS))})"
            )
        unexpected = sorted(set(spec) - allowed - {"type"})
        if unexpected:
            raise ReconcilerConfigError(
                f"{location}: probe {action_type!r} of type {kind!r} does not accept {unexpected!r}"
            )
        if kind == "artifact_check" and not (
            isinstance(spec.get("path"), str) or isinstance(spec.get("path_key"), str)
        ):
            raise ReconcilerConfigError(
                f"{location}: probe {action_type!r} of type 'artifact_check' needs "
                "'path' or 'path_key'"
            )
        entry: dict[str, Any] = {"type": kind}
        entry.update({k: v for k, v in spec.items() if k in allowed})
        probes[action_type] = entry
    return probes


def _parse_verdict(text: str) -> bool | Literal["unknown"]:
    """Parse a probe's final output line into occurred True/False/None."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    last = lines[-1]
    lowered = last.lower()
    if lowered.startswith("occurred="):
        value = lowered.split("=", 1)[1]
        return {"true": True, "false": False}.get(value, "unknown")
    try:
        parsed = json.loads(last)
    except json.JSONDecodeError:
        return "unknown"
    if isinstance(parsed, dict):
        occurred = parsed.get("occurred")
        if isinstance(occurred, bool):
            return occurred
        return "unknown"
    elif isinstance(parsed, bool):
        return parsed
    return "unknown"


def probe_verdict(
    spec: Mapping[str, Any], action: Action
) -> tuple[bool | None | Literal["error"], str]:
    """Run one command probe. Returns ``(verdict, detail)``.

    This is the subprocess half of the registry; built-in evidence probes
    (``otel_span``, ``artifact_check``) resolve in process through
    :func:`_resolve_spec` and never reach here.

    The verdict is True, False, None (probe ran but could not tell) or the
    string ``"error"`` (probe itself failed). ``detail`` carries whatever a
    human would want to see next to the outcome.

    ``spec["command"]`` runs through the platform shell, ``cmd.exe /c`` on
    Windows and ``/bin/sh -c`` elsewhere, so a probe string is silently
    shell-family-specific: a registry written on one platform (redirections,
    quoting, builtins) fails on the other, and the run then refuses to
    resume, fail-closed, until the probe is fixed (#842). Prefer an
    executable plus arguments that both shells resolve identically, or keep
    one registry per platform.
    """
    try:
        completed = subprocess.run(
            spec["command"],
            input=json.dumps(action.model_dump(mode="json")),
            capture_output=True,
            text=True,
            timeout=float(spec["timeout"]),
            shell=True,
        )
    except subprocess.TimeoutExpired:
        return (
            "error",
            f"probe for {action.action_type!r} timed out after {spec['timeout']}s",
        )
    except OSError as exc:
        return "error", f"probe for {action.action_type!r} failed to run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        return "error", f"probe exited {completed.returncode}: {detail}"
    verdict = _parse_verdict(completed.stdout)
    if verdict == "unknown":
        return None, (
            f"probe could not determine the outcome from output "
            f"{(completed.stdout or '').strip()[:120]!r}"
        )
    return verdict, (completed.stderr or "").strip()[:200]


@dataclass
class SettleReport:
    """What automatic reconciliation did for one run."""

    settled_true: list[str] = field(default_factory=list)
    settled_false: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    skipped_no_probe: list[str] = field(default_factory=list)

    @property
    def settled(self) -> int:
        """Total actions the reconciliation settled either way."""
        return len(self.settled_true) + len(self.settled_false)

    def as_dict(self) -> dict[str, Any]:
        """Report the settlement outcome as plain data."""
        return {
            "settled_occurred": self.settled_true,
            "settled_not_occurred": self.settled_false,
            "unresolved": [{"action_type": t, "detail": d} for t, d in self.unresolved],
            "no_probe_registered": self.skipped_no_probe,
            "settled_total": self.settled,
        }


def settle_run(
    storage: Storage,
    run_id: str,
    probes: dict[str, dict[str, Any]],
    *,
    dry_run: bool = False,
    strict: bool = False,
) -> SettleReport:
    """Probe every pending action of ``run_id`` and settle definitive ones.

    Dispatches on each spec's ``type``: a command probe runs its subprocess, a
    built-in probe (``otel_span``, ``artifact_check``) resolves in process from
    recorded observation or the filesystem. A verdict the probe could not reach
    leaves the action pending, or escalates it to ``REQUIRES_REVIEW`` when
    ``strict`` is set, so a run that tolerates no uncertainty degrades to a
    human instead of to a silent retry (issue #268).
    """
    from continuum.actions import ActionLedger  # local import: avoids a cycle at module load

    report = SettleReport()
    ledger = ActionLedger(storage, run_id)
    pending = ledger.pending()
    for action in pending:
        spec = probes.get(action.action_type)
        if spec is None:
            report.skipped_no_probe.append(action.action_id)
            continue
        verdict, detail, evidence = _resolve_spec(spec, action, storage, run_id)
        if verdict == "error":
            report.unresolved.append((action.action_type, detail))
            _maybe_escalate(ledger, storage, run_id, action, detail, strict, dry_run)
            continue
        if verdict is None:
            reason = detail or "probe could not determine the outcome"
            report.unresolved.append((action.action_type, reason))
            _maybe_escalate(ledger, storage, run_id, action, reason, strict, dry_run)
            continue
        assert isinstance(verdict, bool), f"unexpected verdict {verdict!r}"
        label = f"{action.action_type}:{action.external_id or action.action_id[:12]}"
        if dry_run:
            (report.settled_true if verdict else report.settled_false).append(label)
            continue
        ledger.reconcile(
            str(_key_for(storage, run_id, action)),
            occurred=verdict,
            external_id=evidence.get("external_id") if evidence else None,
            result=evidence.get("result") if evidence else None,
            note=(detail or "") if evidence else "",
        )
        (report.settled_true if verdict else report.settled_false).append(label)
    return report


def _maybe_escalate(
    ledger: Any,
    storage: Storage,
    run_id: str,
    action: Action,
    reason: str,
    strict: bool,
    dry_run: bool,
) -> None:
    """In strict mode, an unsettled action goes to review rather than pending."""
    if not strict or dry_run:
        return
    ledger.flag_for_review(str(_key_for(storage, run_id, action)), reason)


def _resolve_spec(
    spec: Mapping[str, Any],
    action: Action,
    storage: Storage,
    run_id: str,
) -> tuple[bool | None | Literal["error"], str, dict[str, Any] | None]:
    """Run one probe of any type. Returns ``(verdict, detail, evidence)``.

    ``evidence`` is what the settlement should carry for provenance: the
    observation payload and the receipt a later audit needs. Command probes
    produce none (their detail is the receipt), built-in ones do.
    """
    if spec.get("type", "command") == "command":
        verdict, detail = probe_verdict(spec, action)
        return verdict, detail, None
    from continuum.evidence import ReconcilerConfigError, build_probe

    try:
        probe = build_probe(spec, storage, run_id)
        resolution = probe.resolve(action)
    except ReconcilerConfigError as exc:
        return "error", str(exc), None
    except Exception as exc:  # an unreachable evidence source decides nothing
        return "error", f"{probe.name} probe failed: {exc}", None
    if resolution is None:
        return None, f"{probe.name} probe could not determine the outcome", None
    return (
        resolution.occurred,
        resolution.note,
        {
            "external_id": resolution.external_id,
            "result": dict(resolution.result) if resolution.result else None,
        },
    )


def _key_for(storage: Storage, run_id: str, action: Action) -> Any:
    """Find the ledger key whose folded record is this action.

    The fold is keyed by derived idempotency key while the Action record does
    not carry it, so recover the key by matching action_id against the run's
    folded ledger.

    Folds full history, not the live tail: pending actions claimed before a
    compaction live on as archived events (issue #647), and a live-only fold
    would make every one of them "vanish mid-reconcile" and abort settle_run.
    """
    from continuum.actions.ledger import fold_action_events

    folded = fold_action_events(storage.read_all_events(run_id))
    for key, candidate in folded.items():
        if candidate.action_id == action.action_id:
            return key
    raise LookupError(f"action {action.action_id} vanished from run {run_id} mid-reconcile")


def _parse_authority_verdict(text: str) -> bool | None | Literal["unknown"]:
    """Parse authority probe final output line into valid True/False/unknown."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    last = lines[-1]
    lowered = last.lower()
    if lowered.startswith("valid="):
        value = lowered.split("=", 1)[1]
        if value == "true":
            return True
        if value == "false":
            return False
        return "unknown"
    try:
        parsed = json.loads(last)
    except json.JSONDecodeError:
        return "unknown"
    if isinstance(parsed, dict):
        valid = parsed.get("valid")
        if isinstance(valid, bool):
            return valid
        if valid is None or (isinstance(valid, str) and valid.lower() == "unknown"):
            return "unknown"
        return "unknown"
    return "unknown"


def probe_authority_verdict(
    spec: Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[bool | None | Literal["error"], str]:
    """Run one authority probe. Returns (verdict, detail).

    ``spec["command"]`` runs through the platform shell, so the same
    shell-family caveat as :func:`probe_verdict` applies: a command string
    written for one shell family fails on another and the authority stays
    blocked, fail-closed (#842).
    """
    try:
        completed = subprocess.run(
            spec["command"],
            input=json.dumps(dict(payload)),
            capture_output=True,
            text=True,
            timeout=float(spec["timeout"]),
            shell=True,
        )
    except subprocess.TimeoutExpired:
        return (
            "error",
            f"authority probe timed out after {spec['timeout']}s",
        )
    except OSError as exc:
        return "error", f"authority probe failed to run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        return "error", f"authority probe exited {completed.returncode}: {detail}"
    verdict = _parse_authority_verdict(completed.stdout)
    if verdict == "unknown":
        return None, (
            f"authority probe could not determine validity from output "
            f"{(completed.stdout or '').strip()[:120]!r}"
        )
    if verdict is None:
        return None, "authority probe returned unknown"
    return verdict, (completed.stderr or "").strip()[:200]


@dataclass
class AuthoritySettleReport:
    """What authority probe did for one authority."""

    authority_id: str
    valid: bool | None
    detail: str
    settled: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Report the authority probe outcome as plain data."""
        return {
            "authority_id": self.authority_id,
            "valid": self.valid,
            "detail": self.detail,
            "settled": self.settled,
        }


def settle_authority(
    storage: Storage,
    run_id: str,
    authority_id: str,
    probes: dict[str, dict[str, Any]],
    *,
    dry_run: bool = False,
) -> AuthoritySettleReport:
    """Probe one authority and settle via AUTHORITY_RECONCILED when definitive."""
    from continuum.events import EventType
    from continuum.models import Origin

    payload: dict[str, Any] = {"authority_id": authority_id}
    # Full history, not the live tail: the AUTHORITY_CONSUMED row can predate a
    # compaction (issue #647), and a live-only scan would hand the probe a bare
    # authority_id, silently dropping the consumption context.
    for ev in reversed(list(storage.read_all_events(run_id))):
        if (
            ev.type is not None
            and str(ev.type) == "AUTHORITY_CONSUMED"
            and ev.payload.get("authority_id") == authority_id
        ):
            payload = {
                "authority_id": authority_id,
                "consumer_run_id": ev.payload.get("consumer_run_id"),
                "via_action_id": ev.payload.get("via_action_id"),
                "consumed_at": ev.payload.get("consumed_at"),
                "sequence": ev.sequence,
            }
            break

    spec = probes.get(authority_id) or probes.get("authority")
    if spec is None:
        return AuthoritySettleReport(
            authority_id, None, "no probe registered for authority", settled=False
        )

    verdict, detail = probe_authority_verdict(spec, payload)
    if verdict == "error":
        return AuthoritySettleReport(authority_id, None, detail, settled=False)
    if verdict is None:
        return AuthoritySettleReport(authority_id, None, detail, settled=False)

    if dry_run:
        return AuthoritySettleReport(authority_id, verdict, detail, settled=False)

    storage.append_event(
        run_id,
        EventType.AUTHORITY_RECONCILED,
        {
            "authority_id": authority_id,
            "valid": verdict,
            "reason": detail,
            "probed_payload": payload,
        },
        source=Origin.DETERMINISTIC,
    )
    return AuthoritySettleReport(authority_id, verdict, detail, settled=True)
