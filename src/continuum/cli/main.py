"""The ``continuum`` command-line interface.

Built on ``argparse`` from the standard library. That is a deliberate choice for
a *recovery* tool: the moment you most need to inspect a broken run is the worst
possible moment to discover your diagnostic tool cannot import its dependencies.
No third-party CLI framework is required.

Two principles shape the surface:

**Read-only by default.** ``inspect``, ``history``, ``validate``, ``diff`` and
``show-contract`` never write. They are safe against a live database while an
agent is mid-run. Only ``init``, ``start``, ``checkpoint``, ``confirm`` and
``resume --repair`` mutate, and they say so.

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
from continuum.clienthooks import (
    CLIENT_PROFILES,
    install_client_hook,
    observe_command,
    observe_event_payload,
    remove_claude_code_hook,
)
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.gate import (
    DEFAULT_GATE_CONFIG_PATH,
    GateConfigError,
    load_gate_config,
)
from continuum.gate import (
    decide as gate_decide,
)
from continuum.models import (
    ActionStatus,
    EnvironmentSnapshot,
    EnvResource,
    Origin,
    RecoveryMode,
    Run,
    RunStatus,
)
from continuum.observability import render_dashboard
from continuum.provenance_map import summarize
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
    ConcurrentWriteError,
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


def cmd_start(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Create a run with a goal, so the CLI can originate work.

    Until now a run could only be created through the Python API or an MCP
    client, which left the CLI's own "start one with ..." hint pointing at
    nothing (issue #204). The run row and its RUN_STARTED event are one fact,
    so they are written in a single transaction (a crash between two separate
    writes would strand a run that can be neither projected nor resumed); the
    goal is asserted by a human at the keyboard, so it is sourced Origin.HUMAN
    rather than self-certified.
    """
    parent_id = getattr(args, "parent", None)
    if parent_id:
        try:
            parent = storage.get_run(parent_id)
        except RunNotFound:
            print(f"error: parent run {parent_id!r} does not exist", file=err)
            return ExitCode.NOT_FOUND
        if parent.status.value == "completed":
            print(f"error: parent run {parent_id!r} is completed; cannot attach children", file=err)
            return ExitCode.ERROR

    metadata_extra: dict[str, Any] = {}
    a2a = getattr(args, "a2a_task", None)
    if a2a:
        metadata_extra["a2a_task_id"] = a2a

    if parent_id or metadata_extra:
        child_run = Run(
            run_id=args.run_id, goal=args.goal, parent_run_id=parent_id, metadata=metadata_extra
        )
    else:
        child_run = Run(run_id=args.run_id, goal=args.goal)
    try:
        run = storage.create_run_started(child_run, source=Origin.HUMAN)
    except ConcurrentWriteError as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.ERROR
    _emit(
        {"run_id": run.run_id, "goal": run.goal},
        f"Started run {run.run_id}: {args.goal}",
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
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


def cmd_status(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Show run status, optionally as a canonical provenance view (issue #148)."""
    restored = CheckpointManager(storage).restore(args.run_id)
    state = restored.state

    if args.provenance:
        rows: list[dict[str, str]] = []
        for kind, items in (
            ("evidence", state.evidence),
            ("finding", state.findings),
            ("decision", state.decisions),
        ):
            for item in items:
                view = summarize(item.provenance.origin, item.status, None)
                item_id = (
                    getattr(item, "evidence_id", None)
                    or getattr(item, "finding_id", None)
                    or getattr(item, "decision_id", None)
                )
                rows.append(
                    {
                        "kind": kind,
                        "id": item_id or "",
                        "who": view.who.value,
                        "trust": view.how_trusted.value,
                        "state": view.what_state.value,
                    }
                )
        if args.json:
            payload = {"provenance": rows}
            _emit(
                payload,
                json.dumps(payload, indent=2),
                as_json=True,
                stream=out,
                palette=getattr(args, "_palette", None),
            )
        else:
            lines = [f"run: {state.run_id}  (provenance view)"]
            for r in rows:
                lines.append(
                    f"  {r['kind']:<8} {r['id']:<14} who={r['who']:<14} "
                    f"trust={r['trust']:<14} state={r['state']}"
                )
            _emit(
                {},
                "\n".join(lines),
                as_json=False,
                stream=out,
                palette=getattr(args, "_palette", None),
            )
        return ExitCode.OK

    lines = [
        f"run:      {state.run_id}",
        f"goal:     {state.goal.description} (v{state.goal.version})",
        f"version:  v{state.version}",
        f"progress: {state.progress.completed} completed, "
        f"{state.progress.pending} pending, {state.progress.failed} failed",
        f"decisions: {len(state.valid_decisions())}/{len(state.decisions)} valid",
    ]
    _emit({}, "\n".join(lines), as_json=False, stream=out, palette=getattr(args, "_palette", None))
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


def _human_steps(decision: Any, run_id: str) -> list[str]:
    """Executable next steps for this decision, derived from live config.

    Read-only: the reconciler registry and gate config are inspected, never
    executed. Absent files simply mean fewer shortcuts to suggest.
    """
    from continuum.gate import DEFAULT_GATE_CONFIG_PATH
    from continuum.reconcilers import DEFAULT_RECONCILERS_PATH, load_reconcilers
    from continuum.recovery.guidance import human_steps_for

    try:
        probes = load_reconcilers(Path(DEFAULT_RECONCILERS_PATH))
        probed: list[str] = list(probes)
    except Exception:
        probed = []
    gate_configured = Path(DEFAULT_GATE_CONFIG_PATH).exists()
    return human_steps_for(
        decision,
        run_id=run_id,
        probed_types=probed,
        gate_configured=gate_configured,
    )


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
    steps = _human_steps(decision, args.run_id)
    if steps:
        text = (
            decision.render()
            + "\n\nNext steps:\n"
            + "\n".join(f"  {i}. {t}" for i, t in enumerate(steps, 1))
        )
    else:
        text = decision.render()
    payload = {
        "run_id": decision.run_id,
        "mode": decision.mode.value,
        "safe": decision.safe,
        "contract": decision.contract.model_dump(mode="json"),
        "rationale": list(decision.rationale),
        "repairs": [s.action_name for s in decision.plan.steps],
        "human_steps": steps,
    }
    _emit(
        payload,
        text,
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
                'No active run to resume. Start one with: continuum start <run_id> --goal "..."',
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

    # Family aggregation (#243): a parent may not RESUME while any child is
    # unsafe or blocked - the most cautious signal wins, house-style.
    from continuum.recovery.family import roll_up_children

    child_statuses, family_blocked = roll_up_children(storage, run_id)
    family_rationale = [
        f"child run {c.run_id} is {c.mode} (uncertain={c.uncertain_actions})"
        for c in child_statuses
        if not c.safe or c.mode != "resume"
    ]
    steps = _human_steps(decision, run_id)
    text = decision.render()
    if family_blocked and decision.mode.value == "resume":
        # House rule: the most cautious signal wins (#243). A clean parent
        # with an unsafe child is presented as request_human.
        text += "\n\nFAMILY BLOCKED: children of this run are not resumable.\n" + "\n".join(
            f"  !! {r}" for r in family_rationale
        )
    if steps:
        text += "\n\nNext steps:\n" + "\n".join(f"  {i}. {t}" for i, t in enumerate(steps, 1))
    if steps:
        text += "\n\nNext steps:\n" + "\n".join(f"  {i}. {t}" for i, t in enumerate(steps, 1))

    # Version pinning drift (issue #241): informational only.
    drift_lines: list[str] = []
    if args.pinning:
        from continuum.pinning import latest_pinning, normalize_pinning
        from continuum.pinning import pinning_drift as compute_drift

        try:
            current = normalize_pinning(json.loads(args.pinning))
            recorded = latest_pinning(storage.read_events(run_id))
            drift_lines = compute_drift(recorded, current)
            if drift_lines:
                text += "\n\nPinning drift (informational):\n" + "\n".join(
                    f"  - {line}" for line in drift_lines
                )
        except ValueError as exc:
            print(f"error: --pinning: {exc}", file=err)
            return ExitCode.ERROR

    presented_mode = (
        "request_human"
        if (family_blocked and decision.mode.value == "resume")
        else decision.mode.value
    )
    presented_safe = decision.safe and not (family_blocked and decision.mode.value == "resume")
    payload = {
        "run_id": decision.run_id,
        "goal": storage.get_run(run_id).goal,
        "mode": presented_mode,
        "safe": presented_safe,
        "next_allowed_action": decision.next_allowed_action,
        "human_steps": steps,
        "family_rationale": family_rationale,
        "children": [c.__dict__ for c in child_statuses],
        "pinning_drift": drift_lines,
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
        text,
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


def cmd_budget(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Report retry-budget usage per action type (issue #240). Read-only."""
    from continuum.budgets import (
        DEFAULT_BUDGETS_PATH,
        attempts_for_type,
        evaluate_budget,
        load_budgets,
    )

    storage.get_run(args.run_id)
    try:
        raw = load_budgets(Path(args.config) if args.config else Path(DEFAULT_BUDGETS_PATH))
    except Exception as exc:
        print(f"error: budget registry invalid: {exc}", file=err)
        return ExitCode.ERROR

    events = storage.read_events(args.run_id)
    types_seen = sorted(
        {
            e.payload.get("action", {}).get("action_type")
            for e in events
            if e.type is EventType.ACTION_RECORDED and isinstance(e.payload.get("action"), dict)
        }
        | set((raw.get("action_types") or {}).keys())
    )
    rows: list[dict[str, Any]] = []
    for action_type in types_seen:
        used = attempts_for_type(events, action_type)
        allowed, _, maximum = evaluate_budget(raw, action_type, 0)
        remaining = max(0, maximum - used)
        rows.append(
            {
                "action_type": action_type,
                "attempts": used,
                "max_attempts": maximum,
                "remaining": remaining,
                "exhausted": remaining == 0,
            }
        )
    payload = {"run_id": args.run_id, "budgets": rows}
    lines = [f"{'ACTION TYPE':<28} {'ATTEMPTS':>8} {'MAX':>4} {'REMAINING':>10}"]
    for r in rows:
        lines.append(
            f"{r['action_type']:<28} {r['attempts']:>8} {r['max_attempts']:>4} {r['remaining']:>10}"
        )
    _emit(
        payload,
        "\n".join(lines) or "No budgets configured.",
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_tree(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Show a parent run and its children with recovery states (issue #243)."""
    from continuum.recovery.family import children_of

    parent_id = args.run_id
    storage.get_run(parent_id)
    engine = RecoveryEngine(storage)
    lines: list[str] = []
    try:
        parent_decision = engine.assess(parent_id)
        lines.append(
            f"{parent_id}  [{parent_decision.mode.value}, safe={parent_decision.safe}]"
            f"  {storage.get_run(parent_id).goal[:50]}"
        )
    except Exception as exc:
        lines.append(f"{parent_id}  [assess error: {exc}]")

    children = children_of(storage, parent_id)
    if not children:
        lines.append("  (no children)")
    for child in children:
        try:
            d = engine.assess(child.run_id)
            mark = "ok " if d.safe else "!! "
            lines.append(
                f"  {mark}{child.run_id}  [{d.mode.value}, "
                f"uncertain={len(d.uncertain_actions)}]  {child.goal[:44]}"
            )
        except Exception as exc:
            lines.append(f"  !! {child.run_id}  [assess error: {exc}]")
    a2a = [
        (c.run_id, c.metadata.get("a2a_task_id")) for c in children if c.metadata.get("a2a_task_id")
    ]
    for rid, task in a2a:
        lines.append(f"  a2a: {rid} -> {task}")
    _emit(
        {
            "parent": args.run_id,
            "children": [{"run_id": c.run_id, "status": c.status.value} for c in children],
        },
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_compact(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Archive the pre-anchor prefix of a run's event log (issue #239). Mutates."""
    storage.get_run(args.run_id)
    if not args.force:
        print(
            "compact archives the pre-anchor event prefix and appends an "
            "EVENT_LOG_ANCHORED marker to the live chain. Re-run with --force "
            "to apply.",
            file=err,
        )
        return ExitCode.ERROR
    report = storage.compact_run(args.run_id)
    payload = {"run_id": args.run_id, **report}
    _emit(
        payload,
        f"Archived {report['archived']} event(s); the live log now starts at the anchor.",
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_complete(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Close a run as done, from the keyboard (the maintainer's escape hatch).

    Found missing during live testing: MCP-driven runs close via
    RUN_COMPLETED events from adapters, but there was no way to finish a run
    from the CLI, so finished work kept surfacing as the active run and
    hijacked every fresh session's resume. This appends REVIEW_CONFIRMED
    plus RUN_COMPLETED (both Origin.HUMAN, so they clear self-certification
    gates) and flips the run row to COMPLETED.
    """
    run = storage.get_run(args.run_id)  # raises RunNotFound -> NOT_FOUND
    note = {"summary": args.summary} if args.summary else {}
    storage.append_event(
        args.run_id,
        EventType.REVIEW_CONFIRMED,
        {"components": ["goal", "progress"]},
        source=Origin.HUMAN,
    )
    storage.append_event(
        args.run_id,
        EventType.RUN_COMPLETED,
        {"closed_by": "cli", **note},
        source=Origin.HUMAN,
    )
    updated = run.touch(status=RunStatus.COMPLETED)
    storage.update_run(updated)
    payload = {
        "run_id": args.run_id,
        "status": updated.status.value,
        "summary": args.summary or "",
    }
    _emit(
        payload,
        f"Run {args.run_id} completed.",
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_checkpoint(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Force a checkpoint. Mutates the run."""
    # Check existence before projecting: otherwise a typo'd run name surfaces
    # as a ProjectionError about a missing RUN_STARTED event (exit 1) instead
    # of the truth (exit 2), which is the same misdiagnosis issue #18 fixed
    # for `events`. Every other mutating command checks first; this one does too.
    storage.get_run(args.run_id)  # raises RunNotFound -> NOT_FOUND by the dispatcher
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


def cmd_observe(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Record one observed tool completion as durable evidence (issue #207).

    Reads a Claude Code PostToolUse hook payload (JSON on stdin, or from
    ``--payload-file``) and appends a ``TOOL_COMPLETED`` event to the target
    run: the explicit ``--run-id``, else the most recently active non-terminal
    run. This is what closes part of the durability gap: the recording happens
    in a host-side hook after every file-mutating tool call, outside the
    model's control, so work that landed on disk is never invisible to
    recovery even when no checkpoint was ever taken.

    With no active run the observation is dropped with exit 0 rather than an
    error: hooks fire for every Claude Code session in this directory,
    including ones with nothing to do with CONTINUUM, and a wall of failures
    would pressure the user into uninstalling the instrumentation. The note on
    stderr keeps the drop visible.
    """
    if args.payload_file:
        raw_text = Path(args.payload_file).read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()
    try:
        raw = json.loads(raw_text) if raw_text.strip() else None
    except json.JSONDecodeError as exc:
        print(f"error: observe payload is not valid JSON: {exc}", file=err)
        return ExitCode.ERROR

    run_id = args.run_id
    if not run_id:
        active = storage.get_active_run()
        run_id = active.run_id if active else None
    if not run_id:
        print("No active CONTINUUM run; observation not recorded.", file=err)
        return ExitCode.OK

    storage.get_run(run_id)  # raises RunNotFound -> NOT_FOUND by the dispatcher

    payload = observe_event_payload(raw if isinstance(raw, dict) else {})
    event = storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        payload,
        source=Origin.EXTERNAL_AGENT,
    )
    _emit(
        {
            "run_id": run_id,
            "sequence": event.sequence,
            "event_id": event.event_id,
            **payload,
        },
        f"Observed {payload.get('tool')} -> {run_id} (seq {event.sequence})",
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_briefing(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Session-start context injection (no CLAUDE.md required).

    Wired as a SessionStart hook by `hooks install`. Prints, as
    hook-consumable JSON plus human-readable text, everything a returning
    agent needs: the active run's goal, progress, recovery verdict,
    executable next steps, and disk-checked file observations. Read-only;
    with no active run it says exactly how to create one.
    """
    run_id = args.run_id
    if not run_id:
        active = storage.get_active_run()
        run_id = active.run_id if active else None

    if not run_id:
        text = (
            "CONTINUUM: no active run. Create one before working durably: "
            "`continuum start <run_id> --goal '...'`, or call "
            "continuum_record_progress(run_id, completed, total, goal=...) via MCP."
        )
        context = "[CONTINUUM] " + text
        _emit(
            {"active_run": None, "context": context},
            text,
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK

    decision = RecoveryEngine(storage).assess(run_id)
    steps = _human_steps(decision, run_id)
    contract = decision.contract
    state = decision.state

    lines = [
        f"CONTINUUM active run: {run_id}",
        f"goal: {state.goal.description}",
        f"progress: {state.progress.completed}/{state.progress.total or '?'} completed"
        + (f", {state.progress.failed} failed" if state.progress.failed else ""),
        f"recovery: {decision.mode.value} (safe={decision.safe})",
    ]
    # Newest reasoning summary (#235): the resumed agent inherits the dead
    # session's plan state, not just its progress counters.
    summaries = [e for e in storage.read_events(run_id) if e.type is EventType.REASONING_SUMMARY]
    if summaries:
        summary = summaries[-1].payload.get("summary", {})
        lines.append("where the last session left off (self-authored):")
        for item in summary.get("plan_stack", [])[:3]:
            lines.append(f"  plan: {item}")
        for d in summary.get("decisions", [])[-3:]:
            what = d.get("what", "")
            why = d.get("why", "")
            lines.append(f"  decision: {what}" + (f" ({why})" if why else ""))
        for q in summary.get("open_questions", [])[:3]:
            lines.append(f"  open: {q}")
        ws = summary.get("working_set", [])
        if ws:
            lines.append(f"  working set: {', '.join(map(str, ws[:5]))}")

    obs = contract.post_checkpoint_observations[:5]
    if obs:
        lines.append("files since checkpoint:")
        lines += [
            f"  [{o.get('status', '?')}] {o.get('path', '')}" for o in obs if not o.get("truncated")
        ]
    if steps:
        lines.append("next steps:")
        lines += [f"  {i}. {t}" for i, t in enumerate(steps, 1)]

    text = "\n".join(lines)
    context = text
    _emit(
        {
            "active_run": run_id,
            "mode": decision.mode.value,
            "safe": decision.safe,
            "context": context,
            "human_steps": steps,
            "hookSpecificOutput": {
                "hookEventName": args.hook_event_name,
                "additionalContext": context,
            },
        },
        text,
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_gateway(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Run the enforcing HTTP gateway (issue #213 seam 4). Long-running."""
    from continuum.gateway import (
        DEFAULT_GATEWAY_CONFIG_PATH,
        GatewayConfigError,
        GatewayServer,
        load_gateway_config,
    )

    config_path = Path(args.config) if args.config else Path(DEFAULT_GATEWAY_CONFIG_PATH)
    try:
        routes = load_gateway_config(config_path)
    except GatewayConfigError as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.ERROR
    if not routes:
        print(
            f"error: no upstreams registered in {config_path}; "
            "the gateway refuses to start as an open relay",
            file=err,
        )
        return ExitCode.ERROR

    active = storage.get_active_run()
    run_id = args.run_id or (active.run_id if active else None)
    server = GatewayServer(lambda: open_storage(args.db), run_id, routes, port=args.port)
    print(
        f"CONTINUUM gateway listening on 127.0.0.1:{server.port} "
        f"({len(routes)} upstream route(s), run={run_id or 'dynamic'})",
        file=err,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return ExitCode.OK


def cmd_hooks_install(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Wire a coding CLI's tool events into observe (and optionally gate).

    Per-client wiring is data-driven (#209): the profile supplies the
    settings-path default and the event/matcher names; the installed commands
    are the same client-agnostic ``continuum observe`` / ``continuum gate``.
    """
    from continuum.clienthooks import CLIENT_PROFILES

    profile = CLIENT_PROFILES[args.client]
    settings_path = Path(args.settings or profile["settings"])
    command = observe_command(db=args.db)
    gate_command = command[: -len("observe")] + "gate"

    briefing_command = command[: -len("observe")] + "briefing"
    statuses = [
        (
            install_client_hook(
                settings_path,
                command,
                event_name=profile["post_event"],
                matcher=profile["write_matcher"],
            ),
            profile["write_matcher"],
            "observe",
            profile["post_event"],
            command,
        ),
        (
            install_client_hook(
                settings_path,
                briefing_command,
                event_name=profile["start_event"],
                matcher="",
            ),
            "",
            "briefing",
            profile["start_event"],
            briefing_command,
        ),
    ]
    if getattr(args, "with_gate", False):
        statuses.append(
            (
                install_client_hook(
                    settings_path,
                    gate_command,
                    event_name=profile["pre_event"],
                    matcher=profile["any_matcher"],
                ),
                profile["any_matcher"],
                "gate",
                profile["pre_event"],
                gate_command,
            )
        )

    lines = [f"Hook configuration written to {settings_path}"]
    for st, matcher, kind, event, cmd in statuses:
        lines.append(f"  [{st}] {kind} on {event} (matcher {matcher})")
        lines.append(f"    command: {cmd}")
    payload: dict[str, Any] = {
        "client": args.client,
        "settings": str(settings_path),
        "hooks": [
            {"event": event, "matcher": m, "kind": k, "status": st, "command": cmd}
            for st, m, k, event, cmd in statuses
        ],
    }
    if args.client == "codex":
        hint = _codex_feature_flag_hint()
        if hint:
            lines.append(hint)
            payload["feature_flag_hint"] = hint
    _emit(
        payload,
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def _codex_feature_flag_hint() -> str:
    """Codex gates its hook engine behind a config flag; without it hooks are
    silent no-ops. We do not hand-edit TOML, so surface the exact line."""
    config = Path.home() / ".codex" / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return (
            "note: Codex hooks are off by default. Add '[features]\\ncodex_hooks = true' "
            f"to {config} (create it if needed), then restart Codex."
        )
    if "codex_hooks" not in text:
        return (
            f"note: 'codex_hooks' was not found in {config}; add "
            "'[features]\\ncodex_hooks = true', then restart Codex."
        )
    return ""


def cmd_hooks_remove(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Remove the observation hook previously installed for a coding CLI."""
    settings_path = Path(args.settings)
    removed = remove_claude_code_hook(settings_path)
    text = (
        f"Removed observation hook from {settings_path}"
        if removed
        else f"No observation hook found in {settings_path}"
    )
    _emit(
        {"client": args.client, "settings": str(settings_path), "removed": removed},
        text,
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    return ExitCode.OK


def cmd_gate(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Decide whether one tool call may proceed (issue #217).

    Designed as a pre-tool-use hook: exit 0 allows the call, exit 2 denies it
    with an actionable reason on stderr that the harness feeds back to the
    model. The decision is pure (:func:`continuum.gate.decide`); this command
    only resolves the run, loads the ledger projection and renders the
    verdict.

    Exit codes deliberately reuse the CLI contract where they agree: OK
    allows. Denial is reported as 2, which the CLI defines as NOT_FOUND but a
    hook transport defines as "block this tool call"; in both readings the
    caller must not proceed.
    """
    from continuum.actions.ledger import fold_action_events

    if args.payload_file:
        raw_text = Path(args.payload_file).read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()
    try:
        raw = json.loads(raw_text) if raw_text.strip() else None
    except json.JSONDecodeError as exc:
        # The payload cannot be matched against any pattern; there is nothing
        # to enforce, and blocking every call over a protocol hiccup would
        # make the harness unusable.
        print(f"gate: payload is not valid JSON ({exc}); allowing", file=err)
        return ExitCode.OK

    run_id = args.run_id
    if not run_id:
        active = storage.get_active_run()
        run_id = active.run_id if active else None

    config_path = Path(args.config) if args.config else Path(DEFAULT_GATE_CONFIG_PATH)
    try:
        config = load_gate_config(config_path)
    except GateConfigError as exc:
        print(f"gate: {exc}; denying until it is fixed", file=err)
        return 2

    if not isinstance(raw, dict):
        _emit(
            {"allow": True, "reason": "no payload"},
            "gate: no payload; allowing",
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK

    tool_name = str(raw.get("tool_name") or "")
    tool_input_raw = raw.get("tool_input")
    tool_input: dict[str, Any] = dict(tool_input_raw) if isinstance(tool_input_raw, dict) else {}

    if config is not None and tool_name in config and run_id is None:
        print(
            f"gate: {tool_name} is gated but there is no active CONTINUUM run. "
            f"Start or resume one (continuum start / continuum_record_progress) first.",
            file=err,
        )
        return 2

    actions_by_key = fold_action_events(storage.read_events(run_id)) if run_id is not None else {}
    decision = gate_decide(
        config,
        tool_name,
        tool_input,
        run_id=run_id or "",
        actions_by_key=actions_by_key,
    )
    if decision.allow:
        _emit(
            {"allow": True, "reason": decision.reason, "tool": tool_name},
            f"[ok] allow: {decision.reason}",
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK
    # A hook transport feeds stderr back to the model on a blocking exit, so
    # the actionable reason must live there; stdout keeps the machine view.
    print(f"[!!] deny: {decision.reason}", file=err)
    _emit(
        {"allow": False, "reason": decision.reason, "tool": tool_name},
        "",
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )

    return 2


def cmd_reconcile_auto(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Settle uncertain actions with registered probes (issue #218).

    Mutating by design (it appends ACTION_RECONCILED events through the
    ledger), which is why it is its own command rather than something
    `validate`/`resume` do implicitly: those stay read-only so the exit-code
    safety contract holds. With no registered probe for an action's type,
    that action is left exactly as the ledger holds it.
    """
    from continuum.actions.ledger import ActionLedger
    from continuum.reconcilers import (
        DEFAULT_RECONCILERS_PATH,
        ReconcilerConfigError,
        load_reconcilers,
        settle_run,
    )

    storage.get_run(args.run_id)
    try:
        probes = load_reconcilers(
            Path(args.config) if args.config else Path(DEFAULT_RECONCILERS_PATH)
        )
    except ReconcilerConfigError as exc:
        print(f"error: {exc}", file=err)
        return ExitCode.ERROR

    pending = ActionLedger(storage, args.run_id).pending()
    report = settle_run(storage, args.run_id, probes, dry_run=args.dry_run)
    payload = {"run_id": args.run_id, "dry_run": args.dry_run, **report.as_dict()}
    lines = [
        f"pending actions: {len(pending)}, "
        f"settled: {report.settled} "
        f"(occurred {len(report.settled_true)}, not-occurred {len(report.settled_false)}), "
        f"unresolved: {len(report.unresolved)}, "
        f"no probe registered: {len(report.skipped_no_probe)}"
    ]
    for action_type, detail in report.unresolved:
        lines.append(f"  [!!] {action_type}: {detail}")
    if args.dry_run:
        lines.append("dry run: nothing was written")
    _emit(
        payload,
        "\n".join(lines),
        as_json=args.json,
        stream=out,
        palette=getattr(args, "_palette", None),
    )
    remaining = len(pending) - report.settled
    return ExitCode.OK if remaining <= 0 else ExitCode.REQUIRES_HUMAN


def cmd_verify(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
    """Re-audit the event chain for tampering."""
    # A run that does not exist has an empty, trivially valid chain. Reporting
    # that as "verified" would let `continuum verify $TYPO && deploy` succeed on
    # a name nobody has ever written to.
    if args.repair_index and not args.index:
        print("error: --repair-index requires --index", file=err)
        return ExitCode.ERROR

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

    # Action index consistency (issue #216). The index is a projection of the
    # ACTION_* events, so any disagreement is drift in the projection, never
    # corruption of the truth, and repair is always safe.
    index_lines: list[str] = []
    if getattr(args, "index", False):
        drift: int | None = None
        has_index = hasattr(storage, "action_index_drift")
        rebuild = getattr(storage, "rebuild_action_index", None)
        if has_index:
            # Repair only a projection whose source of truth verified: a
            # tampered log must never be folded into the index (review 221).
            if args.repair_index and not report.ok:
                index_lines.append(
                    "[!!] action index repair refused: the event chain failed "
                    "verification; repair would launder tampered events"
                )
                payload["action_index_repair"] = "refused_chain_failed"
            else:
                drift = storage.action_index_drift()
                if drift and args.repair_index and rebuild is not None:
                    fixed = int(rebuild())
                    index_lines.append(
                        f"[ok] action index repaired from the log ({fixed} row(s) corrected)"
                    )
                    drift = storage.action_index_drift()
                elif drift:
                    index_lines.append(
                        f"[!!] action index drifted from the log ({drift} row(s)); "
                        f"run with --repair-index to rebuild it"
                    )
                else:
                    index_lines.append("[ok] action index matches the log")
        else:
            index_lines.append("[auto] this engine maintains no action index")
        text = text + "\n" + "\n".join(index_lines)
        payload["action_index_drift"] = drift

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

    anchored = (
        any(e.type is EventType.EVENT_LOG_ANCHORED for e in events)
        and storage.latest_version(args.run_id) is not None
    )
    if anchored and args.upto is None:
        # Compacted run (#239): fold the restored checkpoint state forward
        # over the post-anchor tail; the archived prefix lives in
        # events_archive and is audited by verify's deep mode.
        from continuum.state.semantic import project_incremental

        stored = storage.latest_version(args.run_id)
        base = CheckpointManager(storage).restore(args.run_id, replay=False).state
        # The anchor event sits exactly at the base boundary; folding it would
        # trip the monotonic-sequence check.
        tail = [e for e in events if base is None or e.sequence > base.source_sequence]
        state, _report = project_incremental(args.run_id, tail, base=base)
        payload = {
            "run_id": args.run_id,
            "events_replayed": len(events),
            "completed": state.progress.completed,
            "source_sequence": state.source_sequence,
            "verified": True,
            "verification": (
                f"anchored run: checkpoint v{stored.version if stored else 0} "
                f"+ {len(events)} tail event(s); prefix audited in events_archive"
            ),
        }
        _emit(
            payload,
            f"Anchored replay: folded v{stored.version if stored else 0} + "
            f"{len(events)} tail event(s)",
            as_json=args.json,
            stream=out,
            palette=getattr(args, "_palette", None),
        )
        return ExitCode.OK

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

    start = with_run(add("start", cmd_start, "Create a run with a goal. Mutates storage."))
    start.add_argument("--goal", required=True, help="what the run is trying to achieve")
    start.add_argument("--parent", default=None, help="attach as a child of this run")
    start.add_argument(
        "--a2a-task",
        dest="a2a_task",
        default=None,
        help="external A2A task id to record in metadata",
    )

    inspect = with_run(add("inspect", cmd_inspect, "Show semantic state."))
    inspect.add_argument("--version", type=int, dest="version", help="inspect a past version")

    status = with_run(add("status", cmd_status, "Show run status."))
    status.add_argument(
        "--provenance",
        action="store_true",
        help="render the canonical provenance view (issue #148)",
    )

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
    resume.add_argument(
        "--pinning",
        default=None,
        help="JSON object of environment pins to diff against the run (issue #241)",
    )

    confirm = with_env(
        with_run(add("confirm", cmd_confirm, "Confirm self-reported state so the run may resume."))
    )
    confirm.add_argument("--model", help="model that will run the resumed agent")
    confirm.add_argument("--tolerate-unknown", action="store_true")

    complete = with_run(add("complete", cmd_complete, "Close a run as done. Mutates storage."))
    complete.add_argument("--summary", default=None, help="one-line closing note")

    budget_cmd = with_run(add("budget", cmd_budget, "Retry-budget usage per action type."))
    budget_cmd.add_argument(
        "--config",
        default=None,
        help="budget registry path (default: .continuum/budgets.json)",
    )

    tree_parser = with_run(add("tree", cmd_tree, "Show a parent run and its children."))
    tree_parser.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)

    compact = with_run(
        add("compact", cmd_compact, "Archive the pre-anchor log prefix. Mutates storage.")
    )
    compact.add_argument("--force", action="store_true", help="apply without confirmation")

    checkpoint = with_env(with_run(add("checkpoint", cmd_checkpoint, "Force a checkpoint.")))
    checkpoint.add_argument("--trigger", default="manual")
    checkpoint.add_argument("--reason", default="")

    observe = add("observe", cmd_observe, "Record one observed tool completion. Mutates storage.")
    observe.add_argument(
        "--run-id",
        default=None,
        help="target run (default: the most recently active non-terminal run)",
    )
    observe.add_argument(
        "--payload-file",
        default=None,
        help="read the hook payload from this file instead of stdin",
    )

    gateway_cmd = add(
        "gateway",
        cmd_gateway,
        "Run the enforcing HTTP proxy for registered upstreams. Mutates storage.",
    )
    gateway_cmd.add_argument("--port", type=int, default=8765)
    gateway_cmd.add_argument("--run-id", default=None)
    gateway_cmd.add_argument(
        "--config",
        default=None,
        help="route registry path (default: .continuum/gateway.json)",
    )

    briefing = add(
        "briefing",
        cmd_briefing,
        "Session-start context: active run, progress, next steps. Read-only.",
    )
    briefing.add_argument(
        "--run-id",
        default=None,
        help="run to brief on (default: the most recently active non-terminal run)",
    )
    briefing.add_argument(
        "--hook-event-name",
        dest="hook_event_name",
        default="SessionStart",
        help=argparse.SUPPRESS,
    )

    gate = add(
        "gate",
        cmd_gate,
        "Decide whether a tool call may proceed (pre-tool-use hook). Read-only.",
    )
    gate.add_argument(
        "--run-id",
        default=None,
        help="target run (default: the most recently active non-terminal run)",
    )
    gate.add_argument(
        "--payload-file",
        default=None,
        help="read the hook payload from this file instead of stdin",
    )
    gate.add_argument(
        "--config",
        default=None,
        help=f"gate registry path (default: {DEFAULT_GATE_CONFIG_PATH})",
    )

    hooks = add("hooks", cmd_hooks_install, "Manage host-side observation hooks.")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", metavar="ACTION CLIENT", required=True)

    def hooks_client(p: argparse.ArgumentParser, func: Any) -> None:
        p.add_argument(
            "client",
            choices=tuple(CLIENT_PROFILES),
            help="which client to configure (claude-code, gemini, codex)",
        )
        p.add_argument(
            "--settings",
            default=None,
            help="path to the client's settings file (default: per client profile)",
        )
        p.set_defaults(func=func)

    install = hooks_sub.add_parser(
        "install", help="Install the observation hook. Mutates settings."
    )
    install.add_argument(
        "--db",
        default=None,
        help="bake a specific database path into the hook command",
    )
    install.add_argument(
        "--with-gate",
        action="store_true",
        help="also install a PreToolUse gate that denies unclaimed side-effect calls",
    )
    hooks_client(install, cmd_hooks_install)

    remove = hooks_sub.add_parser("remove", help="Remove the observation hook.")
    hooks_client(remove, cmd_hooks_remove)

    verify = with_run(add("verify", cmd_verify, "Re-audit the event chain."))
    verify.add_argument(
        "--index",
        action="store_true",
        help="also compare the derived action index against the log (issue #216)",
    )
    verify.add_argument(
        "--repair-index",
        action="store_true",
        help="rebuild drifted index rows from the log (requires --index)",
    )

    reconcile_auto = with_run(
        add(
            "reconcile",
            cmd_reconcile_auto,
            "Settle uncertain actions with registered probes. Mutates storage.",
        )
    )
    reconcile_auto.add_argument(
        "--dry-run", action="store_true", help="report what probes would settle, write nothing"
    )
    reconcile_auto.add_argument(
        "--config",
        default=None,
        help="probe registry path (default: .continuum/reconcilers.json)",
    )
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
        choices=("stdio", "http"),
        help="wire transport (default: stdio; http serves POST /<method> JSON)",
    )
    serve.add_argument("--port", type=int, default=8765, help="port for --transport http")

    def cmd_dashboard(args: argparse.Namespace, storage: Storage, out: Any, err: Any) -> int:
        from continuum.dashboard import serve_dashboard as _serve

        print(f"Serving dashboard at http://localhost:{args.port}", file=out)
        _serve(storage, port=args.port, host=args.host)
        return 0

    dashboard = add("dashboard", cmd_dashboard, "Serve the dashboard (presentation over run data).")
    dashboard.add_argument(
        "--port", type=int, default=8000, help="port to listen on (default: 8000)"
    )
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1; 0.0.0.0 exposes recovery data)",
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

    # hooks never touches a run, so it must not create an empty database as a
    # side effect of editing a settings file.
    if args.command in ("benchmark", "attest-keygen", "serve", "hooks"):
        return int(args.func(args, None, out, err))

    try:
        storage = open_storage(args.db)
    except (StorageError, ValueError, NotImplementedError, RuntimeError) as exc:
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
