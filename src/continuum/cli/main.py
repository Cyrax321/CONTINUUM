"""The ``continuum`` command-line interface.

Built on ``argparse`` from the standard library. That is a deliberate choice for
a *recovery* tool: the moment you most need to inspect a broken run is the worst
possible moment to discover your diagnostic tool cannot import its dependencies.
No third-party CLI framework is required.

Two principles shape the surface:

**Read-only by default.** ``inspect``, ``history``, ``validate``, ``diff`` and
``show-contract`` never write. They are safe against a live database while an
agent is mid-run. Only ``init``, ``checkpoint`` and ``resume --repair`` mutate,
and they say so.

**Exit codes carry the verdict.** ``continuum resume $RUN && ./start-agent.sh``
must not launch an agent onto stale state, so only a verified-safe run exits 0.
See ``continuum.cli.exitcodes``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from continuum import __version__
from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointError, CheckpointManager
from continuum.cli.colour import Palette
from continuum.cli.exitcodes import ExitCode, exit_code_for
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import ActionStatus, EnvironmentSnapshot, EnvResource, Origin, RecoveryMode
from continuum.observability import render_dashboard
from continuum.recovery import RecoveryEngine, render_contract
from continuum.security.attestation import (
    generate_keypair,
    sign_chain,
    verify_attestation,
)
from continuum.serve import cmd_serve
from continuum.state.diff import diff_states, render_diff
from continuum.state.semantic import ProjectionError, project
from continuum.state.versioning import state_fingerprint
from continuum.storage import (
    CheckpointNotFound,
    CorruptedRecord,
    RunNotFound,
    Storage,
    StorageError,
    open_storage,
)

__all__ = ["main", "build_parser"]

_DEFAULT_DB = "continuum.db"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _colourise(text: str, palette: Palette) -> str:
    """Add colour to already-rendered text.

    Deliberately a post-processing pass rather than colour woven through every
    command: the text is produced once, identically, and this only decides how
    it looks. It cannot alter wording, ordering or exit codes, because it never
    sees them — and when colour is off it returns the string untouched.
    """
    if not palette.enabled:
        return text

    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("[ok]"):
            out.append(line.replace("[ok]", palette.ok("[ok]"), 1))
        elif stripped.startswith("[!!]"):
            out.append(line.replace("[!!]", palette.bad("[!!]"), 1))
        elif stripped.startswith("[human]"):
            out.append(line.replace("[human]", palette.warn("[human]"), 1))
        elif stripped.startswith("[auto]"):
            out.append(line.replace("[auto]", palette.grey("[auto]"), 1))
        elif stripped.startswith("CONTINUUM RECOVERY"):
            out.append(palette.heading(line))
        elif stripped.startswith("Recovery decision:"):
            label, _, verdict = line.partition(":")
            out.append(f"{palette.bold(label)}:{palette.status(verdict, verdict.strip())}")
        elif stripped.startswith("Safe to resume:"):
            label, _, verdict = line.partition(":")
            coloured = palette.ok(verdict) if verdict.strip() == "yes" else palette.bad(verdict)
            out.append(f"{palette.bold(label)}:{coloured}")
        elif stripped.startswith("INTEGRITY FAILURE"):
            out.append(palette.bad(line))
        elif stripped.startswith("Event chain verified"):
            out.append(palette.ok(line))
        elif stripped.endswith("reconcile before resuming.") and stripped:
            out.append(palette.warn(line))
        elif line.startswith(("RUN ", "VERSION ", "STATUS ")):
            out.append(palette.bold(line))
        elif stripped.startswith(("+ ", "~ ", "- ", "! ")):
            sigil = stripped[0]
            colours = {"+": palette.ok, "-": palette.bad, "~": palette.warn, "!": palette.bad}
            out.append(line.replace(sigil, colours[sigil](sigil), 1))
        else:
            out.append(line)
    return "\n".join(out)


def _emit(
    payload: dict[str, Any],
    text: str,
    *,
    as_json: bool,
    stream: Any,
    palette: Palette | None = None,
) -> None:
    """Write either machine-readable JSON or human text, never both.

    JSON is never colourised: it is the machine path, and an escape sequence in
    it would be a parse error rather than a decoration.

    Flushed immediately: stdout is block-buffered when piped, so without this a
    later stderr hint would surface *before* the report it refers to.
    """
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=stream)
    else:
        print(_colourise(text, palette) if palette else text, file=stream)
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def _environment(args: argparse.Namespace, run_id: str) -> EnvironmentSnapshot | None:
    """Build a snapshot from ``--env name=version`` pairs.

    Returns ``None`` when nothing was supplied, which the validator treats as
    "unverified" rather than "unchanged" — omitting the flag must not look like
    a clean environment.
    """
    pairs: list[str] = list(getattr(args, "env", None) or [])
    if not pairs:
        return None
    # Built as explicit EnvResource values rather than **kwargs: a resource
    # legitimately named "resources" would otherwise collide with the
    # provider's own parameter.
    resources: dict[str, EnvResource] = {}
    for pair in pairs:
        name, separator, version = pair.partition("=")
        if not name or not separator:
            raise ValueError(f"--env expects name=version, got {pair!r}")
        if not version:
            # An empty version would be compared as a real value, quietly
            # reporting "v3 -> " as a dependency change. Almost always a typo
            # or an unexpanded shell variable, so refuse rather than guess.
            raise ValueError(
                f"--env {name}= has an empty version; "
                f"omit the flag entirely if the value is unknown"
            )
        resources[name] = EnvResource(name=name, version=version)
    return capture(run_id, StaticProvider(resources))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_init(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Create the database and report where it lives."""
    _emit(
        {"database": args.db, "schema": "ready"},
        f"Initialised CONTINUUM storage at {args.db}",
        as_json=args.json,
        stream=out,
    )
    return ExitCode.OK


def cmd_runs(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    runs = storage.list_runs(limit=args.limit)
    if not runs:
        _emit(
            {"runs": []},
            "No runs recorded.",
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK

    payload = [
        {
            "run_id": r.run_id,
            "goal": r.goal,
            "status": r.status.value,
            "created_at": r.created_at,
            "events": storage.last_sequence(r.run_id),
        }
        for r in runs
    ]
    lines = [f"{'RUN':<24} {'STATUS':<12} {'EVENTS':>7}  GOAL"]
    lines += [
        f"{r.run_id:<24} {r.status.value:<12} {storage.last_sequence(r.run_id):>7}  {r.goal[:44]}"
        for r in runs
    ]
    _emit(
        {"runs": payload},
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_inspect(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Show semantic state, optionally at a past version."""
    if args.version is not None:
        state = storage.get_version(args.run_id, args.version)
    else:
        restored = CheckpointManager(storage).restore(args.run_id)
        state = restored.state

    payload = state.model_dump(mode="json")
    lines = [
        f"run:         {state.run_id}",
        f"goal:        {state.goal.description} (v{state.goal.version})",
        f"version:     v{state.version}  (events 1..{state.source_sequence})",
        f"progress:    {state.progress.completed} completed, "
        f"{state.progress.pending} pending, {state.progress.failed} failed",
        f"decisions:   {len(state.decisions)} ({len(state.valid_decisions())} valid)",
        f"findings:    {len(state.findings)}",
        f"evidence:    {len(state.evidence)}",
        f"pending:     {len(state.open_work())} task(s)",
    ]
    if state.external_dependencies:
        lines.append("dependencies:")
        lines += [
            f"  - {d.resource}: {d.version or 'unversioned'} [{d.status}]"
            for d in state.external_dependencies
        ]
    _emit(
        payload,
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_history(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    # Multiple checkpoints legitimately share a state version (put_version
    # returns the same version when the state fingerprint is unchanged), so we
    # list every checkpoint rather than keying by version, which would collapse
    # the lineage into a single row.
    storage.get_run(args.run_id)  # raises RunNotFound if it truly does not exist
    checkpoints = storage.list_checkpoints(args.run_id)
    if not checkpoints:
        _emit(
            {"checkpoints": []},
            "No checkpoints recorded.",
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK

    payload = []
    lines = [f"{'CHECKPOINT':<12} {'VERSION':<9} {'TRIGGER':<24} PROGRESS"]
    for checkpoint in checkpoints:
        state = storage.get_version(args.run_id, checkpoint.version)
        payload.append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "version": checkpoint.version,
                "trigger": checkpoint.trigger,
                "completed": state.progress.completed,
                "source_sequence": state.source_sequence,
            }
        )
        marker = checkpoint.checkpoint_id[:10]
        lines.append(
            f"{marker:<12} v{checkpoint.version:<8} {checkpoint.trigger:<24} "
            f"{state.progress.completed} completed"
        )
    _emit(
        {"checkpoints": payload},
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_events(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    storage.get_run(args.run_id)  # raises RunNotFound for a run that was never created
    events = storage.read_events(args.run_id, after_sequence=args.after, upto=args.upto)
    payload = [
        {
            "sequence": e.sequence,
            "type": e.type.value,
            "timestamp": e.timestamp,
            "payload": dict(e.payload),
        }
        for e in events
    ]
    lines = [f"{e.sequence:>5}  {e.type.value:<26} {dict(e.payload)}" for e in events]
    _emit(
        {"events": payload},
        "\n".join(lines) or "No events.",
        as_json=args.json,
        stream=out,
    )
    return ExitCode.OK


def cmd_diff(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    before = storage.get_version(args.run_id, args.from_version)
    after = storage.get_version(args.run_id, args.to_version)
    diff = diff_states(before, after)
    _emit(
        diff.model_dump(mode="json"),
        render_diff(diff),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_validate(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Assess a run without touching it. Exit code carries the verdict."""
    decision = RecoveryEngine(storage, strict_unknown=not args.tolerate_unknown).assess(
        args.run_id,
        current_environment=_environment(args, args.run_id),
        expected_model=args.model,
    )
    if getattr(args, "dashboard", False):
        out.write(render_dashboard(decision) + "\n")
        out.flush()
        return exit_code_for(decision.mode)
    payload = {
        "run_id": decision.run_id,
        "mode": decision.mode.value,
        "safe": decision.safe,
        "contract": decision.contract.model_dump(mode="json"),
        "rationale": list(decision.rationale),
        "repairs": [s.action_name for s in decision.plan.steps],
    }
    _emit(
        payload,
        decision.render(),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return exit_code_for(decision.mode)


def cmd_resume(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Report how a run may resume. Read-only unless ``--repair`` is given."""
    run_id = args.run_id
    if not run_id:
        active = storage.get_active_run()
        if active is None:
            print(
                "No active run to resume. Start one with: continuum checkpoint <run_id>",
                file=err,
            )
            return 2
        run_id = active.run_id
    engine = RecoveryEngine(storage, strict_unknown=not args.tolerate_unknown)
    decision = engine.assess(
        run_id,
        current_environment=_environment(args, run_id),
        expected_model=args.model,
    )

    payload = {
        "run_id": decision.run_id,
        "goal": storage.get_run(run_id).goal,
        "mode": decision.mode.value,
        "safe": decision.safe,
        "next_allowed_action": decision.next_allowed_action,
        "contract": decision.contract.model_dump(mode="json"),
        "repairs": [s.action_name for s in decision.plan.steps],
        "progress": {
            "completed": decision.state.progress.completed,
            "pending": decision.state.progress.pending,
            "failed": decision.state.progress.failed,
        },
    }
    _emit(
        payload,
        decision.render(),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )

    if decision.mode is not RecoveryMode.RESUME and not args.repair:
        print(
            "\nRun with --repair to record the repair plan, or resolve the items above first.",
            file=err,
        )

    if args.repair and decision.plan:
        storage.append_event(
            run_id,
            EventType.RECOVERY_STARTED,
            {
                "mode": decision.mode.value,
                "plan": [step.model_dump() for step in decision.plan.steps],
            },
        )
        print(
            f"\nRepair plan recorded ({len(decision.plan.steps)} step(s)). "
            f"Rerun resume to confirm progress.",
            file=err,
        )

    return exit_code_for(decision.mode)


def cmd_confirm(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Confirm an externally-driven run's self-reported state so it may resume.

    Appends a REVIEW_CONFIRMED event (sourced from Origin.HUMAN) which clears the
    REQUIRES_REVIEW that self_certified goal/progress would otherwise force, then
    re-assesses the run. This is the escape hatch for MCP/agent-reported runs
    that would otherwise be stuck at request_human with no way to proceed. See
    issue #35.
    """
    storage.append_event(
        args.run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )

    engine = RecoveryEngine(storage, strict_unknown=not args.tolerate_unknown)
    decision = engine.assess(
        args.run_id,
        current_environment=_environment(args, args.run_id),
        expected_model=args.model,
    )

    payload = {
        "run_id": decision.run_id,
        "mode": decision.mode.value,
        "safe": decision.safe,
        "next_allowed_action": decision.next_allowed_action,
    }
    _emit(
        payload,
        decision.render(),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    print("\nRun `continuum resume` to continue.", file=err)
    return exit_code_for(decision.mode)


def cmd_checkpoint(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Force a checkpoint. Mutates the run."""
    manager = CheckpointManager(storage)
    checkpoint = manager.checkpoint(
        args.run_id,
        trigger=args.trigger,
        reason=args.reason or "",
        environment=_environment(args, args.run_id),
    )
    payload = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "version": checkpoint.version,
        "trigger": checkpoint.trigger,
        "integrity_hash": checkpoint.integrity_hash,
        "completed": checkpoint.state.progress.completed,
    }
    _emit(
        payload,
        f"Checkpoint {checkpoint.checkpoint_id} written at v{checkpoint.version} "
        f"({checkpoint.state.progress.completed} completed)",
        as_json=args.json,
        stream=out,
    )
    return ExitCode.OK


def cmd_verify(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Re-audit the event chain for tampering."""
    # A run that does not exist has an empty, trivially valid chain. Reporting
    # that as "verified" would let `continuum verify $TYPO && deploy` succeed on
    # a name nobody has ever written to.
    storage.get_run(args.run_id)
    report = storage.verify_events(args.run_id)
    payload = report.model_dump(mode="json")
    if report.ok:
        text = f"Event chain verified: {report.checked} events, no violations."
    else:
        lines = [f"INTEGRITY FAILURE: {len(report.violations)} violation(s)"]
        lines += [f"  seq {v.sequence}: {v.kind} — {v.detail}" for v in report.violations[:20]]
        trusted = report.trusted_through.get(args.run_id, 0)
        lines.append(f"  trusted through sequence {trusted}")
        text = "\n".join(lines)
    _emit(payload, text, as_json=args.json, stream=out, palette=getattr(args, "_palette", None))
    return ExitCode.OK if report.ok else ExitCode.CORRUPTED


def cmd_actions(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """List recorded side effects and flag any with unknown outcomes."""
    # As with verify: "no actions" for a nonexistent run would read as
    # "nothing outstanding", which is the opposite of the truth.
    storage.get_run(args.run_id)
    ledger = ActionLedger(storage, args.run_id)
    actions = ledger.all()
    payload = [a.model_dump(mode="json") for a in actions]

    if not actions:
        _emit(
            {"actions": []},
            "No actions recorded.",
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK

    lines = [f"{'STATUS':<16} {'TYPE':<28} EXTERNAL ID"]
    lines += [f"{a.status.value:<16} {a.action_type:<28} {a.external_id or '-'}" for a in actions]
    uncertain = [
        a
        for a in actions
        if a.status in (ActionStatus.UNKNOWN, ActionStatus.STARTED, ActionStatus.REQUIRES_REVIEW)
    ]
    if uncertain:
        lines.append("")
        lines.append(
            f"{len(uncertain)} action(s) with unresolved outcomes — reconcile before resuming."
        )
    _emit(
        {"actions": payload},
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.REQUIRES_HUMAN if uncertain else ExitCode.OK


def cmd_contract(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    decision = RecoveryEngine(storage).assess(
        args.run_id, current_environment=_environment(args, args.run_id)
    )
    _emit(
        decision.contract.model_dump(mode="json"),
        render_contract(decision.contract),
        as_json=args.json,
        stream=out,
    )
    return exit_code_for(decision.mode)


def cmd_replay(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Re-derive state from events and confirm it matches the stored version."""
    # Check existence first: otherwise a typo'd name reports "never recorded
    # RUN_STARTED", which diagnoses the wrong problem entirely.
    storage.get_run(args.run_id)
    events = storage.read_events(args.run_id, upto=args.upto)

    if args.upto is not None and not any(e.type == EventType.RUN_STARTED for e in events):
        raise ValueError(
            f"--upto {args.upto} excludes the RUN_STARTED event for run "
            f"{args.run_id!r}; increase --upto or omit it to replay from the "
            f"beginning"
        )

    state = project(args.run_id, events)

    verified, verification = _verify_against_stored(args.run_id, storage)

    payload = {
        "run_id": args.run_id,
        "events_replayed": len(events),
        "completed": state.progress.completed,
        "source_sequence": state.source_sequence,
        "verified": verified,
        "verification": verification,
    }
    text = (
        f"Replayed {len(events)} events -> {state.progress.completed} completed, "
        f"{len(state.decisions)} decision(s), {len(state.findings)} finding(s)\n"
        f"Verification: {verification}"
    )
    _emit(payload, text, as_json=args.json, stream=out, palette=getattr(args, "_palette", None))
    if verified is False:
        print(
            f"replayed state does not match the stored version for run {args.run_id}",
            file=err,
        )
        return ExitCode.CORRUPTED
    return ExitCode.OK


def _verify_against_stored(run_id: str, storage: Storage) -> tuple[bool | None, str]:
    """Re-derive the stored version's own prefix and check it still projects to it.

    Returns (verified, human description). ``None`` means the comparison was not
    attempted, which is reported rather than quietly counted as a pass — a
    silent no-op that looks like a check is the bug this replaces.

    The prefix matters. A stored version is the projection of events up to its
    ``source_sequence``, and the log has usually grown since it was written, so
    comparing it against a replay of the *whole* log would report corruption for
    any run that simply did more work after its last checkpoint. Re-folding the
    same prefix is the invariant SemanticState actually promises: "folding the
    same prefix again must yield an equal state".

    This is why verification does not depend on ``--upto``: the prefix is chosen
    from the stored version, not from what the caller asked to display.
    """
    stored = storage.latest_version(run_id)
    if stored is None:
        return None, "skipped (no stored version to compare against)"
    prefix = storage.read_events(run_id, upto=stored.source_sequence)
    replayed = project(run_id, prefix)
    where = f"version {stored.version} at sequence {stored.source_sequence}"
    if state_fingerprint(replayed) == state_fingerprint(stored):
        return True, f"matches stored {where}"
    return False, f"DOES NOT match stored {where}"


def cmd_benchmark(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    import json

    from continuum.benchmark import (
        render,
        render_idempotency,
        run_benchmark,
        run_idempotency_benchmark,
    )

    total = getattr(args, "total", 200) or 200
    recovery = run_benchmark(total=total)
    idem = run_idempotency_benchmark(total=max(1, total // 4))
    print(render(recovery), file=out)
    print(file=out)
    print(render_idempotency(idem), file=out)
    if getattr(args, "json", False):
        payload = {
            "recovery": [r.as_dict() for r in recovery],
            "idempotency": [r.as_dict() for r in idem],
        }
        print(json.dumps(payload, indent=2), file=out)
    return ExitCode.OK


def cmd_attest_keygen(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Generate an Ed25519 signer key pair for event-chain attestation.

    Does not touch storage: key custody is the operator's responsibility, so the
    tool only writes the two PEM files and says where they went.
    """
    private_pem, public_pem = generate_keypair()
    priv_path = Path(args.out) if args.out else Path("signer.pem")
    pub_path = Path(args.pub) if args.pub else priv_path.with_suffix(priv_path.suffix + ".pub")
    priv_path.write_text(private_pem, encoding="utf-8")
    pub_path.write_text(public_pem, encoding="utf-8")
    payload = {"private_key": str(priv_path), "public_key": str(pub_path)}
    text = f"Wrote private key {priv_path} and public key {pub_path}. Keep the private key secret."
    _emit(payload, text, as_json=args.json, stream=out, palette=getattr(args, "_palette", None))
    return ExitCode.OK


def cmd_attest(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Sign the current head of a run's event chain into an attestation document.

    The signed point is the run's latest event: its sequence number and the
    event-log root hash. The document is portable and self-contained, so it can
    be handed to any third party who then runs ``attest-verify`` against their
    own copy of the store.
    """
    storage.get_run(
        args.run_id
    )  # Raising RunNotFound here is better than a silent empty attestation.
    events = storage.read_events(args.run_id)
    if not events:
        raise ValueError(f"run {args.run_id!r} has no events to attest")
    head = events[-1]
    if head.hash is None:
        raise ValueError(f"run {args.run_id!r} head event has no hash; the chain is incomplete")

    key_path = args.key or os.environ.get("CONTINUUM_SIGNER_KEY")
    if not key_path:
        raise ValueError("no signing key: pass --key PATH or set CONTINUUM_SIGNER_KEY")
    private_pem = Path(key_path).read_text(encoding="utf-8")
    signer = args.signer or os.environ.get("CONTINUUM_SIGNER")

    attest = sign_chain(
        private_pem,
        args.run_id,
        head.sequence,
        head.hash,
        signer=signer,
    )
    doc = attest.to_dict()

    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        payload = {"attestation_file": args.out, **doc}
        text = f"Attestation written to {args.out} (seq {head.sequence}, hash {head.hash[:12]}...)"
    else:
        payload = doc
        text = json.dumps(doc, indent=2, sort_keys=True)
    _emit(payload, text, as_json=args.json, stream=out, palette=getattr(args, "_palette", None))
    return ExitCode.OK


def cmd_attest_verify(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Verify a signed attestation against the run's live event chain.

    Three outcomes:
      SIGNED    — signature valid and the live chain still matches the signed point.
      ALTERED   — signature valid but the chain changed after signing.
      UNTRUSTED — the signature does not verify against the embedded public key.
    """
    storage.get_run(args.run_id)
    doc = json.loads(Path(args.attest).read_text(encoding="utf-8"))

    events = storage.read_events(args.run_id)
    live_hash = events[-1].hash if events else None
    live_seq = events[-1].sequence if events else 0

    signature_valid = verify_attestation(doc)
    chain_match = doc.get("chain_hash") == live_hash
    seq_match = doc.get("trusted_through_seq") == live_seq

    if not signature_valid:
        verdict = "UNTRUSTED"
    elif not chain_match or not seq_match:
        verdict = "ALTERED"
    else:
        verdict = "SIGNED"

    payload = {
        "run_id": args.run_id,
        "verdict": verdict,
        "signature_valid": signature_valid,
        "chain_match": chain_match,
        "signer": doc.get("signer"),
        "signed_seq": doc.get("trusted_through_seq"),
        "live_seq": live_seq,
    }
    if verdict == "SIGNED":
        text = (
            f"Attestation SIGNED by {doc.get('signer')} for seq {doc.get('trusted_through_seq')}; "
            f"chain matches."
        )
    elif verdict == "ALTERED":
        text = (
            f"Attestation ALTERED: chain changed since signing "
            f"(signed seq {doc.get('trusted_through_seq')} vs live {live_seq})."
        )
    else:
        text = "Attestation UNTRUSTED: signature does not verify against the embedded key."
    _emit(payload, text, as_json=args.json, stream=out, palette=getattr(args, "_palette", None))
    return ExitCode.OK if verdict == "SIGNED" else ExitCode.CORRUPTED


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuum",
        description="Semantic recovery layer for long-running AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"continuum {__version__}")
    parser.add_argument(
        "--db", default=_DEFAULT_DB, help=f"storage URL or path (default: {_DEFAULT_DB})"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    colour = parser.add_mutually_exclusive_group()
    colour.add_argument(
        "--color",
        "--colour",
        dest="color",
        action="store_true",
        default=None,
        help="force colour even when not writing to a terminal",
    )
    colour.add_argument(
        "--no-color",
        "--no-colour",
        dest="color",
        action="store_false",
        help="disable colour",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Any, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def with_run(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("run_id", help="the run to operate on")
        return p

    def with_env(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--env",
            action="append",
            metavar="NAME=VERSION",
            help="declare a current environment resource (repeatable)",
        )
        return p

    add("init", cmd_init, "Create storage.")
    add("runs", cmd_runs, "List runs.").add_argument("--limit", type=int, default=20)

    inspect = with_run(add("inspect", cmd_inspect, "Show semantic state."))
    inspect.add_argument("--version", type=int, dest="version", help="inspect a past version")

    with_run(add("history", cmd_history, "List state versions and checkpoints."))

    events = with_run(add("events", cmd_events, "List recorded events."))
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--upto", type=int, default=None)

    diff = with_run(add("diff", cmd_diff, "Compare two state versions."))
    diff.add_argument("from_version", type=int)
    diff.add_argument("to_version", type=int)

    validate = with_env(with_run(add("validate", cmd_validate, "Validate state. Read-only.")))
    validate.add_argument("--model", help="model that will run the resumed agent")
    validate.add_argument("--tolerate-unknown", action="store_true")
    validate.add_argument(
        "--dashboard", action="store_true", help="render the Phase 14 recovery dashboard"
    )

    resume = with_env(add("resume", cmd_resume, "Decide how a run may resume."))
    resume.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="the run to resume; omit to resume the most recently active run",
    )
    resume.add_argument("--model", help="model that will run the resumed agent")
    resume.add_argument("--tolerate-unknown", action="store_true")
    resume.add_argument("--repair", action="store_true", help="record the repair plan")

    confirm = with_env(
        with_run(add("confirm", cmd_confirm, "Confirm self-reported state so the run may resume."))
    )
    confirm.add_argument("--model", help="model that will run the resumed agent")
    confirm.add_argument("--tolerate-unknown", action="store_true")

    checkpoint = with_env(with_run(add("checkpoint", cmd_checkpoint, "Force a checkpoint.")))
    checkpoint.add_argument("--trigger", default="manual")
    checkpoint.add_argument("--reason", default="")

    with_run(add("verify", cmd_verify, "Re-audit the event chain."))
    with_run(add("actions", cmd_actions, "List external side effects."))
    with_env(with_run(add("show-contract", cmd_contract, "Print the recovery contract.")))

    replay = with_run(add("replay", cmd_replay, "Re-derive state from events."))
    replay.add_argument("--upto", type=int, default=None)

    add("benchmark", cmd_benchmark, "Run CONTINUUM-Bench (minimal harness).").add_argument(
        "--total", type=int, default=200, help="documents processed per run (default: 200)"
    )

    attest_keygen = add("attest-keygen", cmd_attest_keygen, "Generate an Ed25519 signer key pair.")
    attest_keygen.add_argument("--out", help="private key PEM path (default: signer.pem)")
    attest_keygen.add_argument("--pub", help="public key PEM path (default: signer.pem.pub)")

    attest = with_run(add("attest", cmd_attest, "Sign an event-chain attestation."))
    attest.add_argument("--key", help="private key PEM path (or CONTINUUM_SIGNER_KEY)")
    attest.add_argument("--signer", help="signer name (or CONTINUUM_SIGNER env)")
    attest.add_argument("--out", help="write attestation JSON here (default: stdout)")

    attest_verify = with_run(
        add(
            "attest-verify",
            cmd_attest_verify,
            "Verify a signed attestation against the live chain.",
        )
    )
    attest_verify.add_argument("--attest", required=True, help="path to attestation JSON")

    serve = add("serve", cmd_serve, "Run the CONTINUUM sidecar (JSON wire protocol over stdio).")
    serve.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio",),
        help="wire transport (default: stdio)",
    )
    return parser


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(
    argv: Sequence[str] | None = None,
    *,
    out: Any = None,
    err: Any = None,
) -> int:
    """Run the CLI. Returns a process exit status rather than raising."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    parser = build_parser()
    args = parser.parse_args(argv)
    # JSON is a machine format; an escape sequence in it is a parse error, not
    # a decoration. `_emit` already routes JSON around the colouriser, so this
    # is a second, independent guard: either alone is sufficient, and losing
    # one to a refactor should not be enough to corrupt machine output.
    args._palette = (
        Palette(False) if args.json else Palette.for_stream(out, force=getattr(args, "color", None))
    )
    if getattr(args, "func", None) is None:
        parser.print_help(file=out)
        return ExitCode.OK

    if args.command in ("benchmark", "attest-keygen", "serve"):
        return int(args.func(args, None, out, err))

    try:
        storage = open_storage(args.db)
    except (StorageError, ValueError, NotImplementedError) as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.ERROR
    except sqlite3.Error as exc:
        # An unreadable or unwritable database is an ordinary operator mistake
        # (bad path, no permission). A traceback would bury the useful part.
        #
        # Quoted with literal delimiters rather than !r (issue #94): repr()
        # escapes each backslash, so a Windows path came back doubled and what
        # was printed was not the path that was passed. The quotes are kept
        # because they still reveal leading or trailing whitespace.
        print(f"error: cannot open storage at '{args.db}': {exc}", file=err)
        return ExitCode.ERROR

    try:
        return int(args.func(args, storage, out, err))
    except (RunNotFound, CheckpointNotFound) as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.NOT_FOUND
    except CorruptedRecord as exc:
        print(f"integrity error: {exc}", file=err)
        return ExitCode.CORRUPTED
    except CheckpointError as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.NOT_FOUND
    except (ProjectionError, StorageError, ValueError) as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.ERROR
    finally:
        storage.close()


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess tests
    raise SystemExit(main())
