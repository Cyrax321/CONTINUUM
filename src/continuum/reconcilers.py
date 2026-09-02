"""Registered reconciliation probes for uncertain side effects (issue #218).

An uncertain action blocks resume until something settles it, and until now
that something was always a person. Most of the time the answer is
mechanically checkable ("is the invoice in the outbox?"), so this module lets
a project register one probe per action type:

    .continuum/reconcilers.json
    {"send_invoice": {"command": "check-outbox", "timeout": 10}}

A probe receives the full Action record as JSON on stdin and prints exactly
one verdict on its last stdout line: ``occurred=true``, ``occurred=false`` or
``occurred=unknown`` (a JSON object with an ``occurred`` field also works,
with true/false/null/unknown). The verdict is applied through
:meth:`ActionLedger.reconcile`, so it lands in the log like any other
reconciliation and is auditable there.

Provenance stays conservative and deliberately narrower than what the ledger
technically allows:

- A definitive probe verdict is settled automatically; the event is sourced
  ``DETERMINISTIC`` because a local, registered, auditable command produced
  it.
- Anything else, missing probe, non-zero exit, timeout, unparseable output,
  explicit unknown, leaves the action untouched and the human queue intact.

Auto-settlement therefore only ever shrinks the set of things a person must
look at; it never widens what an agent may certify on its own.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from continuum.models import Action
from continuum.storage.base import Storage

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


def load_reconcilers(path: Path) -> dict[str, dict[str, Any]]:
    """Read the registry. Empty dict when absent; raise when malformed."""
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
    probes: dict[str, dict[str, Any]] = {}
    for action_type, spec in (raw.get("probes") or {}).items():
        if not isinstance(spec, dict) or not isinstance(spec.get("command"), str):
            raise ReconcilerConfigError(
                f"{location}: probe {action_type!r} needs a string 'command'"
            )
        timeout = spec.get("timeout", _DEFAULT_TIMEOUT)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ReconcilerConfigError(
                f"{path}: probe {action_type!r} 'timeout' must be a positive number"
            )
        probes[action_type] = {"command": spec["command"], "timeout": float(timeout)}
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
    """Run one probe. Returns ``(verdict, detail)``.

    The verdict is True, False, None (probe ran but could not tell) or the
    string ``"error"`` (probe itself failed). ``detail`` carries whatever a
    human would want to see next to the outcome.
    """
    try:
        completed = subprocess.run(  # noqa: S602 - operator-configured command
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
        return len(self.settled_true) + len(self.settled_false)

    def as_dict(self) -> dict[str, Any]:
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
) -> SettleReport:
    """Probe every pending action of ``run_id`` and settle definitive ones."""
    from continuum.actions import ActionLedger  # local import: avoids a cycle at module load

    report = SettleReport()
    ledger = ActionLedger(storage, run_id)
    pending = ledger.pending()
    for action in pending:
        spec = probes.get(action.action_type)
        if spec is None:
            report.skipped_no_probe.append(action.action_id)
            continue
        verdict, detail = probe_verdict(spec, action)
        if verdict == "error":
            report.unresolved.append((action.action_type, detail))
            continue
        if verdict is None:
            report.unresolved.append(
                (action.action_type, detail or "probe could not determine the outcome")
            )
            continue
        assert isinstance(verdict, bool), f"unexpected verdict {verdict!r}"
        label = f"{action.action_type}:{action.external_id or action.action_id[:12]}"
        if dry_run:
            (report.settled_true if verdict else report.settled_false).append(label)
            continue
        ledger.reconcile(str(_key_for(storage, run_id, action)), occurred=verdict)
        (report.settled_true if verdict else report.settled_false).append(label)
    return report


def _key_for(storage: Storage, run_id: str, action: Action) -> Any:
    """Find the ledger key whose folded record is this action.

    The fold is keyed by derived idempotency key while the Action record does
    not carry it, so recover the key by matching action_id against the run's
    folded ledger.
    """
    from continuum.actions.ledger import fold_action_events

    folded = fold_action_events(storage.read_events(run_id))
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
    """Run one authority probe. Returns (verdict, detail)."""
    try:
        completed = subprocess.run(  # noqa: S602 - operator-configured command
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
    for ev in reversed(list(storage.read_events(run_id))):
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
