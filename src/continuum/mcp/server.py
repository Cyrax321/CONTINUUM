"""MCP server exposing CONTINUUM to Claude Code and other MCP clients.

A thin layer over ``GenericAgentAdapter``. Every tool delegates to the adapter,
which already wraps ``CheckpointManager``, ``RecoveryEngine`` and
``ActionLedger``. Nothing here re-implements recovery logic; if a behaviour
looks wrong, the fix belongs in the layer below.

Tool results mirror the CLI's ``--json`` output so an agent, a script and a
human reading terminal output all see the same shape.

The action-interception split
-----------------------------

``continuum_intercept_action`` cannot execute the side effect itself: a Python
callable does not cross the MCP boundary. So the protocol is two calls —

1. ``continuum_intercept_action`` claims the action and answers *may I?*
2. the caller performs the effect, then reports back with
   ``continuum_complete_action`` (or ``continuum_fail_action``)

That split matters. Between the two calls the ledger holds a ``STARTED``
record, so a crash in the gap is indistinguishable from a completed effect —
which is exactly the state the ledger is designed to surface rather than
paper over. A caller that never reports back leaves the action uncertain, and
recovery will refuse to resume until it is reconciled. That is the intended
behaviour, not a leak.

The optional dependency
-----------------------

The ``mcp`` SDK is an optional extra, but ``pip install continuum`` installs
the ``continuum-mcp`` console script regardless. So the entry point exists in
environments where its dependency does not, and importing the SDK at module
scope makes that combination fail with a bare ``ModuleNotFoundError``.

That failure is silent where it matters: the process dies before the
``initialize`` handshake, so the client reports only that the server never
became ready, and the traceback goes to a stderr log the operator is not
looking at. The SDK is therefore imported inside ``build_server``, and the
failure is translated by ``main`` into the one-line ``error:`` form the other
cold-start failures already use. Keep it that way -- hoisting these imports
back to module scope re-breaks the diagnosis, because no handler in ``main``
can run if the module never finished importing.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from continuum.actions.ledger import ActionLedger
from continuum.adapters.generic import GenericAgentAdapter
from continuum.environment import StaticProvider, capture
from continuum.events import Event, EventType
from continuum.mcp.authz import (
    CONFIRM_ENV_VAR,
    AuthorizationPolicy,
    AuthPolicy,
    ConfirmPolicy,
    caller_name,
    load_auth,
    load_confirm,
    load_policy,
    token_from,
)
from continuum.models import (
    ActionStatus,
    EnvironmentSnapshot,
    EnvResource,
    Origin,
    Run,
    SemanticState,
    UnknownSideEffect,
)
from continuum.recovery.contract import render_contract
from continuum.state.semantic import project
from continuum.storage import RunNotFound, SQLiteStorage, Storage

if TYPE_CHECKING:
    # Type-only: the runtime import lives in build_server. See the module
    # docstring's "The optional dependency" section.
    from mcp.server import MCPServer

__all__ = ["build_server", "ContinuumMCP", "MalformedRunLog", "DEFAULT_DB", "main"]

DEFAULT_DB = "continuum.db"
_DB_ENV_VAR = "CONTINUUM_DB"

#: Everything written through this server is asserted by a remote agent about
#: its own work. Nothing here is independently verified, so it is recorded as
#: self-certified and cannot by itself establish that a run is safe to resume.
AGENT_SOURCE = Origin.EXTERNAL_AGENT


class MalformedRunLog(RuntimeError):
    """A run's event log does not begin with ``RUN_STARTED``.

    Raised rather than repaired: backfilling a start event behind existing
    history would misorder the run and yield a projection that is wrong in a
    way nothing downstream can detect.
    """


def resolve_database(explicit: str | None = None) -> str:
    """Where to store runs: explicit argument, then env var, then cwd default."""
    return explicit or os.environ.get(_DB_ENV_VAR) or DEFAULT_DB


def _quarantine_path(sidecar: str) -> str:
    """An unused path to park a sidecar at, never clobbering an earlier one.

    A second crash must not overwrite the evidence from the first, so the
    suffix is bumped until the name is free.
    """
    candidate = f"{sidecar}.orphaned"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{sidecar}.orphaned.{counter}"
        counter += 1
    return candidate


def _open_server_storage(database: str) -> SQLiteStorage:
    """Open the server's store, recovering from a hard-killed predecessor.

    A server process killed with SIGKILL cannot run SQLite's WAL cleanup, so it
    can leave ``<db>-wal`` and ``<db>-shm`` sidecars in a state that makes the
    next WAL-mode open fail with a disk I/O error before it can serve a single
    request. Recovery proceeds from least to most destructive, because the two
    sidecars are not equally expendable:

    ``<db>-shm`` is a shared-memory index and genuinely is reconstructable from
    the database and the log, so removing it costs nothing. If a stale ``-shm``
    was the blocker, the retry replays the ``-wal`` and every committed
    transaction survives.

    ``<db>-wal`` is not reconstructable. It holds transactions that were
    committed but not yet checkpointed into the main database, which for a
    write-heavy run can be the entire history — deleting it turns durable work
    into silent loss, and an emptied database still verifies as an intact chain.
    So it is moved aside rather than unlinked: the server comes up, and the
    committed data remains on disk for recovery instead of being destroyed. If
    the retry still fails, the file is put back, since quarantining it bought
    nothing.

    Deliberately scoped to the server's startup rather than ``SQLiteStorage``
    itself: an in-process caller that hits a disk I/O error wants it raised, not
    papered over by moving files next to its database.
    """
    try:
        return SQLiteStorage(database)
    except sqlite3.OperationalError as exc:
        error: sqlite3.OperationalError = exc

    shm = f"{database}-shm"
    wal = f"{database}-wal"

    # Stage 1: discard the reconstructable sidecar only.
    try:
        os.remove(shm)
    except FileNotFoundError:
        pass
    else:
        try:
            return SQLiteStorage(database)
        except sqlite3.OperationalError as exc:
            error = exc

    # Stage 2: preserve the write-ahead log, but get out of its way.
    if os.path.exists(wal):
        quarantine = _quarantine_path(wal)
        try:
            os.replace(wal, quarantine)
        except OSError:
            raise error from None
        try:
            storage = SQLiteStorage(database)
        except sqlite3.OperationalError:
            # No better off than before: restore the log rather than leave it
            # parked under a name nothing looks for.
            with contextlib.suppress(OSError):
                os.replace(quarantine, wal)
            raise
        print(
            f"continuum: orphaned write-ahead log moved to {quarantine}. "
            "It may contain committed transactions that are NOT in "
            f"{database}; recover them before deleting it.",
            file=sys.stderr,
        )
        return storage

    # Nothing was clearable, so the error was something else entirely. Retrying
    # an identical open would fail identically.
    raise error


@contextlib.contextmanager
def _refusal_reaches_the_caller() -> Iterator[None]:
    """Re-raise a deliberate refusal as ``ToolError`` so its reason survives.

    Refusing a call is part of this server's contract, not a crash: an
    unauthorized caller, a progress counter that violates its own arithmetic, a
    run that does not exist, a log that never recorded RUN_STARTED. Each answer
    is only useful if the caller is told which one it was.

    The SDK draws that line by exception type. From mcp 2.1.0 a handler
    exception it does not recognise becomes ``UnexpectedToolError`` whose message
    is just ``"Error executing tool <name>"``, with the cause left on
    ``__cause__``, while a ``ToolError`` keeps its text. Every refusal here is
    raised as a domain exception (``PermissionError`` for authz, ``ValueError``
    for validation, ``RunNotFound``, ``MalformedRunLog``), so under 2.1.0 the
    caller was told nothing at all: not that it was a permissions problem, not
    which counter was wrong, and not the CONTINUUM_MCP_MUTATING_CLIENTS setting
    that fixes the first case. Converting here restores the guidance and states
    the intent, that these outcomes are expected rather than faults.

    Genuinely unexpected exceptions are deliberately not converted. Those should
    keep surfacing as unexpected, because a bug in this server is not a message
    to act on.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        yield
    except (PermissionError, ValueError, RunNotFound, MalformedRunLog) as exc:
        raise ToolError(str(exc)) from exc


def _project_candidate(
    ctx: ContinuumMCP,
    run_id: str,
    event_type: EventType,
    payload: Mapping[str, Any],
) -> tuple[SemanticState, int]:
    """Fold the log with ``payload`` appended, without committing it (issue #364).

    The write path must reject exactly what the read path rejects, and the only
    way to know what the read path will do is to run it. Validating a field in
    isolation is not equivalent: a payload is legal or not *relative to the
    state it lands on*, so a guard that inspects only the arguments is blind to
    every invariant that spans events.

    ``continuum_record_progress`` demonstrated the cost of getting this wrong.
    It appended first and projected after, so a payload the fold refused was
    already durable when the refusal arrived. Because the fold validates each
    intermediate state, no later event could correct it, and every projecting
    surface for that run stayed dead permanently: ``record_progress``,
    ``checkpoint``, ``validate`` and ``resume`` over MCP, plus ``status``,
    ``inspect``, ``replay``, ``show-contract`` and ``briefing`` over the CLI.
    Meanwhile the action tools kept working, so the run could still authorise
    real side effects while recovery was unable to say whether continuing was
    safe. That inversion is what makes an unprojectable log worse than a
    rejected call.

    The candidate event is constructed in memory rather than written and rolled
    back. There is no transaction spanning the append here, and a rollback that
    fails would leave behind precisely the state this prevents.

    Returns the projected state and the head sequence it was validated against.
    The caller passes that sequence to ``append_event`` as ``expected_sequence``:
    validation and append are two statements, so a second writer can advance the
    run in between and two individually-legal payloads can compose into a log
    neither of them would have been allowed to produce (for example ``total=50``
    landing between the read and the write of a ``completed=75`` that omits
    ``total``). One run has one owner by design, but the failure being guarded
    here is unrecoverable, so it is worth not relying on that.
    """
    history = list(ctx.storage.read_events(run_id))
    head = history[-1].sequence if history else 0
    candidate = Event(
        run_id=run_id,
        sequence=head + 1,
        type=event_type,
        payload=dict(payload),
        source=AGENT_SOURCE,
    )
    try:
        return project(run_id, [*history, candidate]), head
    except ValueError as exc:
        # pydantic's ValidationError is a ValueError, so this covers both the
        # model invariants and the projector's own checks. Re-raised with the
        # payload named, because the bare pydantic message reports the folded
        # figures without saying which call produced them.
        raise ValueError(
            f"{event_type.value} {dict(payload)} would leave run {run_id!r} unprojectable "
            f"and was not recorded: {exc}"
        ) from exc


def _append_projectable(
    ctx: ContinuumMCP,
    run_id: str,
    event_type: EventType,
    payload: Mapping[str, Any],
    *,
    attempts: int = 3,
) -> tuple[SemanticState, Event]:
    """Commit ``payload`` only if the fold accepts it against the state it lands on.

    Retries on ``ConcurrentWriteError`` rather than failing, because losing the
    optimistic-concurrency race says nothing about whether the caller's update is
    valid. Re-validation against the new head is the point: the update may still
    be legal, and if the intervening write made it illegal that is exactly what
    the next fold reports. Bounded, so a permanently busy run answers rather than
    spinning.
    """
    from continuum.storage import ConcurrentWriteError

    for remaining in range(attempts - 1, -1, -1):
        state, expected = _project_candidate(ctx, run_id, event_type, payload)
        try:
            event = ctx.storage.append_event(
                run_id,
                event_type,
                payload,
                expected_sequence=expected,
                source=AGENT_SOURCE,
            )
        except ConcurrentWriteError:
            if remaining:
                continue
            raise ValueError(
                f"run {run_id!r} is being written concurrently and this update lost the "
                f"race {attempts} times; nothing was recorded. One run is meant to have "
                f"one owner at a time, so check whether another agent holds this run."
            ) from None
        return state, event
    raise AssertionError("unreachable: the loop either returns or raises")


def _environment(run_id: str, env: Mapping[str, str] | None) -> EnvironmentSnapshot | None:
    """Build a snapshot from a ``{name: version}`` mapping.

    Returns ``None`` when nothing was supplied. The validator treats that as
    *unverified*, not *unchanged* — omitting the environment must never look
    like having checked it and found nothing wrong.
    """
    if not env:
        return None
    resources = {
        name: EnvResource(name=name, version=str(version)) for name, version in env.items()
    }
    return capture(run_id, StaticProvider(resources))


def _declare_dependencies(ctx: ContinuumMCP, run_id: str, env: Mapping[str, str] | None) -> None:
    """Record the checkpointed environment as *declared dependencies* of the run.

    Capturing a snapshot is not enough to make drift matter. The validator
    decides staleness per ``external_dependencies`` entry and returns early when
    a state has none, so a checkpoint carrying only a snapshot produces a
    visible environment diff that invalidates nothing — the run reports
    ``safe_to_resume`` while the dataset underneath it has moved. Declaring each
    resource the agent pinned is what gives the diff something to invalidate,
    and what lets staleness propagate to the evidence resting on it.

    Declared as events rather than written straight onto the checkpoint's state:
    the log is the durable record, so the declaration survives later projections
    and restores, is covered by the hash chain, and carries the same
    ``EXTERNAL_AGENT`` provenance as everything else this server writes. That
    provenance does not weaken the check — unlike goal and progress, a
    dependency's status comes from comparing two snapshots rather than from
    trusting the claim, so the *comparison* stays independent of the agent that
    named the resource.

    Only new or re-pinned resources are appended. An agent checkpointing on a
    schedule with an unchanged environment would otherwise add an event per
    resource per checkpoint, and the projection would fold every one of them
    back down to the same entry.
    """
    if not env:
        return

    declared = {
        dependency.resource: dependency.version
        for dependency in project(run_id, ctx.storage.read_events(run_id)).external_dependencies
    }
    for name, version in env.items():
        if declared.get(name) == str(version):
            continue
        ctx.storage.append_event(
            run_id,
            EventType.DEPENDENCY_DECLARED,
            {"resource": name, "version": str(version)},
            source=AGENT_SOURCE,
        )


class ContinuumMCP:
    """Holds the storage handle and adapter shared by every tool."""

    def __init__(self, database: str | None = None, *, storage: Storage | None = None) -> None:
        self.database = resolve_database(database)
        self.storage: Storage = storage or _open_server_storage(self.database)
        self.adapter = GenericAgentAdapter(self.storage)

    def close(self) -> None:
        self.storage.close()

    # -- helpers shared by the tools -------------------------------------- #

    def ensure_run(self, run_id: str, goal: str | None = None) -> Run:
        """Fetch a run, creating it on first use if a goal was supplied.

        The run row and the ``RUN_STARTED`` event are separate facts: a row can
        exist without the event when the run was created directly through the
        storage API. Projection needs the event — without it, folding the log
        fails with "the log never recorded RUN_STARTED" — so it is backfilled
        when the log is empty.

        ``RUN_STARTED`` must be the *first* event, and this checks for exactly
        that rather than for any event at all. Checking "is the log non-empty"
        would be correct only by accident: it happens to hold today because
        every MCP tool calls this first, so nothing else can get in ahead. The
        moment another writer appends first, that assumption breaks silently.

        A non-empty log whose first event is not ``RUN_STARTED`` raises instead
        of backfilling. Appending it at that point would place the run's start
        *after* events that supposedly preceded it, and any state projected
        from that log would be quietly wrong — a worse outcome than an error
        naming the problem.
        """
        try:
            run = self.storage.get_run(run_id)
        except RunNotFound:
            if goal is None:
                raise
            run = self.storage.create_run(Run(run_id=run_id, goal=goal))

        first = self.storage.read_events(run_id, upto=1)
        if not first:
            self.storage.append_event(
                run_id,
                EventType.RUN_STARTED,
                {"goal": goal or run.goal},
                source=AGENT_SOURCE,
            )
        elif first[0].type is not EventType.RUN_STARTED:
            raise MalformedRunLog(
                f"run {run_id!r} does not begin with RUN_STARTED "
                f"(first event is {first[0].type.value}). CONTINUUM cannot backfill it "
                f"after the fact without misordering the run's history; recreate the "
                f"run, or record RUN_STARTED before any other event."
            )
        return run

    def ledger(self, run_id: str) -> ActionLedger:
        return ActionLedger(self.storage, run_id)


def build_server(
    database: str | None = None,
    *,
    storage: Storage | None = None,
    policy: AuthorizationPolicy | None = None,
    auth: AuthPolicy | None = None,
    confirm_auth: ConfirmPolicy | None = None,
) -> tuple[MCPServer, ContinuumMCP]:
    """Construct the MCP server and its backing context.

    Returns both so tests can drive tools directly and inspect the store.

    ``policy`` decides which callers may use mutating tools. Omitted, it is
    resolved from the environment and then the project policy file, falling
    back to denying every mutation — an unconfigured server is read-only.

    ``auth`` verifies a shared secret before any mutating tool runs. Omitted,
    it is resolved from ``CONTINUUM_MCP_TOKEN`` and is disabled when that is
    unset, leaving the default local, no-account behavior unchanged.

    ``confirm_auth`` gates ``continuum_confirm`` specifically. Unlike the other
    two, it fails closed when unconfigured (issue #201): an agent allowed to
    record progress must not also be able to confirm that progress, which
    would reinstate the self-certification exploit. Omitted, confirmation over
    MCP refuses every caller; a human confirms with ``continuum confirm``, or
    the operator sets ``CONTINUUM_MCP_CONFIRM_TOKEN`` to opt in.

    Raises ``ModuleNotFoundError`` when the optional ``mcp`` extra is not
    installed; ``main`` reports that as an actionable error rather than a
    traceback.
    """
    # Imported here rather than at module scope so a missing optional extra is
    # reported by main() instead of killing the process during import, before
    # any handler can run. Importing continuum.mcp.server therefore succeeds
    # without the extra, which is what lets both entry points -- the
    # continuum-mcp script and `python -m continuum.mcp` -- reach main() at all.
    from mcp.server import MCPServer
    from mcp.server.mcpserver.context import Context
    from mcp.types import ToolAnnotations

    # Configuration is resolved before storage is opened, because both loaders
    # reject malformed input with ValueError (a bad policy file, a token entry
    # without a colon). Opening first would strand that handle with no owner to
    # close it, and would also leave an empty database behind for a server that
    # never started. Nothing here depends on the store, so the order is free.
    policy = load_policy() if policy is None else policy
    auth = load_auth() if auth is None else auth
    confirm_auth = load_confirm() if confirm_auth is None else confirm_auth
    _reject_reused_confirmation_secret(auth, confirm_auth)
    ctx = ContinuumMCP(database, storage=storage)
    server = MCPServer(
        name="continuum-mcp",
        title="CONTINUUM",
        instructions=(
            "Durable recovery for long-running work. Record progress as you go, "
            "checkpoint at meaningful milestones, and before resuming after any "
            "interruption call continuum_resume to find out whether it is safe to "
            "continue. Route every external side effect through "
            "continuum_intercept_action so it is never performed twice.\n"
            "\n"
            "At the start of a session, call continuum_resume with no run_id. If it "
            "returns a run, show its run_id, progress and goal, ask the user whether "
            "to resume it or start something new, and wait for the answer. If it "
            "returns no_active_run, just do what the user asked. The task is the "
            "run's goal, which continuum_resume gives you, so never read or write a "
            "side file to track it.\n"
            "\n"
            "mode=request_human on a run you created over MCP is expected and is not "
            "a blocker. It means the goal and progress are self-reported and nothing "
            "independent corroborates them. Recording progress, checkpointing and the "
            "action tools all keep working. Do not call continuum_confirm to clear "
            "it: that is refused over MCP by design, because an agent must not vouch "
            "for its own claims. Only a human running 'continuum confirm <run_id>' "
            "clears it. Until then, treat a recorded progress count as a claim to "
            "sanity-check rather than a verified fact, and say so once instead of "
            "stopping work. This matters most before skipping completed units: "
            "confirm the work really happened rather than trusting the counter."
        ),
    )

    read_only = ToolAnnotations(read_only_hint=True)
    mutating = ToolAnnotations(read_only_hint=False)

    def guard(fn: Callable[..., str]) -> Callable[..., str]:
        """Refuse a mutating tool unless the caller is on the allowlist.

        Applied per tool rather than globally so the read-only/mutating split
        is visible at each call site, and so a new tool has to make an explicit
        choice: undecorated means read-only, and a test asserts every mutating
        tool carries this.

        The check runs before the handler body, so a refused call writes
        nothing — the denial precedes the side effect rather than following it.
        """

        @functools.wraps(fn)
        def wrapper(*args: Any, ctx: Context | None = None, **kwargs: Any) -> str:
            caller = caller_name(ctx)
            # Authenticate before authorize: a caller proves the shared secret
            # first, then its declared name is checked against the allowlist.
            # Both must pass; either failure refuses the call before any write.
            with _refusal_reaches_the_caller():
                auth.verify(caller, token_from(ctx))
                policy.require(caller, fn.__name__)
                return fn(*args, **kwargs)

        # The SDK locates the context parameter via get_type_hints(), and
        # functools.wraps copies the *wrapped* function's annotations — which
        # have no `ctx`. Re-advertise it in both the annotations and the
        # signature, or the guard is never handed a context and every caller
        # looks unidentified.
        original = inspect.signature(fn)
        wrapper.__annotations__ = {**fn.__annotations__, "ctx": Context}
        wrapper.__signature__ = original.replace(  # type: ignore[attr-defined]
            parameters=[
                *original.parameters.values(),
                inspect.Parameter(
                    "ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context
                ),
            ]
        )
        return wrapper

    def confirm_gate(fn: Callable[..., str]) -> Callable[..., str]:
        """Authorize and authenticate ``continuum_confirm`` on its own terms.

        This replaces ``guard`` rather than stacking onto it (issue #201). The
        handshake carries a single ``_meta.authToken``, so a stacked check
        would demand two different secrets through one slot. Confirmation gets
        its own credential instead: the caller must be on the mutation
        allowlist *and* present the dedicated confirm secret. Without that
        secret configured the tool refuses everyone, because an agent allowed
        to record progress must not silently be able to confirm it too.
        """

        @functools.wraps(fn)
        def wrapper(*args: Any, ctx: Context | None = None, **kwargs: Any) -> str:
            caller = caller_name(ctx)
            # Authenticate before authorizing (CodeRabbit review, PR #206):
            # a caller that cannot present the confirmation secret must not
            # be able to probe the allowlist, or receive its contents in the
            # refusal, by sending requests without a token.
            with _refusal_reaches_the_caller():
                confirm_auth.verify(token_from(ctx))
                policy.require(caller, fn.__name__)
            return fn(*args, **kwargs)

        # Same fix-up as ``guard``: re-advertise the context parameter or the
        # SDK never hands us one and every caller looks tokenless.
        original = inspect.signature(fn)
        wrapper.__annotations__ = {**fn.__annotations__, "ctx": Context}
        wrapper.__signature__ = original.replace(  # type: ignore[attr-defined]
            parameters=[
                *original.parameters.values(),
                inspect.Parameter(
                    "ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context
                ),
            ]
        )
        return wrapper

    # -- progress --------------------------------------------------------- #

    @server.tool(
        name="continuum_record_progress",
        description=(
            "Record how far through a task you are. Call this as you complete units "
            "of work so progress survives a crash. Creates the run on first call if "
            "'goal' is given. Cheap — call it often."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_record_progress(
        run_id: str,
        completed: int,
        total: int | None = None,
        goal: str | None = None,
        failed: int = 0,
    ) -> str:
        """Record progress for a run."""
        # Reject impossible counters before anything is written, including the
        # run itself: a rejected call must not leave behind a runs row and a
        # RUN_STARTED event (issue #203).
        #
        # These two checks are kept even though `_project_candidate` below would
        # catch the same states, because they can answer without touching
        # storage and they name the offending argument rather than the folded
        # figures. They are not sufficient on their own: `total` is only known
        # here when the caller passes it, and a call that omits it is still
        # bounded by the `total` already on record (issue #364).
        if completed < 0 or failed < 0:
            raise ValueError("progress counters must be non-negative")
        if total is not None and completed + failed > total:
            raise ValueError(f"completed ({completed}) + failed ({failed}) exceeds total ({total})")
        ctx.ensure_run(run_id, goal)
        payload: dict[str, Any] = {"completed": completed, "failed": failed}
        if total is not None:
            payload["total"] = total
            payload["pending"] = max(total - completed - failed, 0)

        # Fold with this payload appended before committing it, and commit under
        # optimistic concurrency so the validated state is the one it lands on.
        # Appending first and projecting after leaves a rejected event
        # permanently in the log, which no later event can correct (issue #364).
        state, event = _append_projectable(ctx, run_id, EventType.TASK_UPDATED, payload)

        return _json(
            {
                "run_id": run_id,
                "completed": state.progress.completed,
                "pending": state.progress.pending,
                "failed": state.progress.failed,
                "total": state.progress.total,
                # From the committed event rather than the candidate: the two
                # agree because the append is guarded by expected_sequence, and
                # reporting the real sequence keeps the answer honest regardless.
                "source_sequence": event.sequence,
            }
        )

    # -- checkpointing ---------------------------------------------------- #

    @server.tool(
        name="continuum_checkpoint",
        description=(
            "Save a durable checkpoint of the current task state. Worth doing at "
            "milestones, before risky or irreversible steps, and before a long gap. "
            "Recovery replays from the newest checkpoint, so checkpointing bounds "
            "how much work a crash can cost."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_checkpoint(
        run_id: str,
        reason: str = "",
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a semantic checkpoint."""
        ctx.ensure_run(run_id)
        _declare_dependencies(ctx, run_id, env)
        state = project(run_id, ctx.storage.read_events(run_id))
        checkpoint = ctx.adapter.capture_state(
            run_id,
            state,
            environment=_environment(run_id, env),
            reason=reason,
        )
        return _json(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "run_id": run_id,
                "version": checkpoint.version,
                "trigger": checkpoint.trigger,
                "integrity_hash": checkpoint.integrity_hash,
                "completed": checkpoint.state.progress.completed,
                "source_sequence": checkpoint.state.source_sequence,
            }
        )

    # -- reasoning summaries ---------------------------------------------- #

    @server.tool(
        name="continuum_record_summary",
        description=(
            "Record a compact summary of WHERE your reasoning is, so a fresh "
            "session after any interruption inherits your plan instead of "
            "guessing. Call at natural checkpoints and before ending a turn. "
            "Schema: {plan_stack: [current step first], decisions: [{what, why}], "
            "open_questions: [...], working_set: [files/ids in play]}. Hard cap "
            "4096 characters serialized - summarise, never dump transcripts."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_record_summary(
        run_id: str,
        plan_stack: list[str] | None = None,
        decisions: list[dict[str, str]] | None = None,
        open_questions: list[str] | None = None,
        working_set: list[str] | None = None,
        note: str = "",
        pinning: dict[str, Any] | None = None,
    ) -> str:
        """Store one bounded reasoning summary (issue #235)."""
        from continuum.pinning import normalize_pinning

        pinning_clean = normalize_pinning(pinning)
        summary = {
            "plan_stack": plan_stack or [],
            "decisions": decisions or [],
            "open_questions": open_questions or [],
            "working_set": working_set or [],
            "note": note,
        }
        serialized = json.dumps(summary, ensure_ascii=False)
        if len(serialized) > 4096:
            from mcp.server.mcpserver.exceptions import ToolError

            raise ToolError(
                f"reasoning summary is {len(serialized)} chars; cap is 4096. "
                "Summarise harder: fewer, shorter entries."
            )
        ctx.ensure_run(run_id)
        payload: dict[str, Any] = {"summary": summary}
        if pinning_clean:
            payload["pinning"] = pinning_clean
        event = ctx.storage.append_event(
            run_id,
            EventType.REASONING_SUMMARY,
            payload,
            source=Origin.EXTERNAL_AGENT,
        )
        return _json(
            {
                "run_id": run_id,
                "sequence": event.sequence,
                "recorded": True,
                "bytes": len(serialized),
            }
        )

    # -- validation ------------------------------------------------------- #

    @server.tool(
        name="continuum_validate",
        description=(
            "Check whether saved state is still trustworthy, without changing "
            "anything. Pass 'env' as {resource: version} to declare what the world "
            "looks like now — a dependency that moved since the checkpoint "
            "invalidates the findings built on it. Read-only: safe to call anytime."
        ),
        annotations=read_only,
    )
    def continuum_validate(
        run_id: str,
        env: dict[str, str] | None = None,
        expected_model: str | None = None,
    ) -> str:
        """Validate a run's state against the current environment."""
        decision = ctx.adapter.resume(
            run_id,
            current_environment=_environment(run_id, env),
            expected_model=expected_model,
        )
        report = decision.validation.report
        return _json(
            {
                "run_id": run_id,
                "safe": decision.safe,
                "mode": decision.mode.value,
                "checkpoint_version": report.checkpoint_version,
                "reason": report.reason,
                "components": [
                    {
                        "component": e.component.value,
                        "component_id": e.component_id,
                        "status": e.status.value,
                        "detail": e.detail,
                    }
                    for e in report.statuses
                ],
                "environment_changes": [d.render() for d in decision.environment_diff.breaking],
            }
        )

    # -- recovery --------------------------------------------------------- #

    @server.tool(
        name="continuum_resume",
        description=(
            "Ask whether it is safe to continue a run after any interruption, and "
            "what to do first. Call this BEFORE resuming work. Returns a mode: "
            "'resume' means proceed; anything else means stop and perform "
            "'next_allowed_action' first. Read-only. If 'run_id' is omitted, targets "
            "the most recently active (interrupted) run, so a fresh session can "
            "resume without remembering the id."
        ),
        annotations=read_only,
    )
    def continuum_resume(
        run_id: str | None = None,
        env: dict[str, str] | None = None,
        expected_model: str | None = None,
    ) -> str:
        """Compute a recovery decision and contract."""
        if not run_id:
            active = ctx.storage.get_active_run()
            if active is None:
                return _json(
                    {
                        "run_id": None,
                        "mode": "no_active_run",
                        "safe": False,
                        "message": (
                            "No active run to resume. Start one with "
                            "continuum_record_progress(run_id, completed, total, goal=...)."
                        ),
                    }
                )
            run_id = active.run_id
        decision = ctx.adapter.resume(
            run_id,
            current_environment=_environment(run_id, env),
            expected_model=expected_model,
        )
        # Return the goal so a resumed session knows what to continue without
        # any external task file. The run's goal is the single source of truth
        # for "what was this task?" across interruptions.
        goal = ctx.storage.get_run(run_id).goal
        tail_evidence = decision.tail_evidence
        # Executable next steps (issue: actionable guidance). Derived from
        # the plan plus whatever automation this project has registered, so
        # the resuming agent never translates statuses into commands itself.
        from continuum.gate import DEFAULT_GATE_CONFIG_PATH
        from continuum.reconcilers import DEFAULT_RECONCILERS_PATH, load_reconcilers
        from continuum.recovery.guidance import human_steps_for, self_report_guidance

        try:
            probed = list(load_reconcilers(Path(DEFAULT_RECONCILERS_PATH)))
        except Exception:
            probed = []
        human_steps = human_steps_for(
            decision,
            run_id=run_id,
            probed_types=probed,
            gate_configured=Path(DEFAULT_GATE_CONFIG_PATH).exists(),
        )
        return _json(
            {
                "run_id": run_id,
                "goal": goal,
                "mode": decision.mode.value,
                "safe": decision.safe,
                "next_allowed_action": decision.next_allowed_action,
                "human_steps": human_steps,
                "rationale": list(decision.rationale),
                "repairs": [
                    {
                        "action": step.action_name,
                        "kind": step.kind.value,
                        "target": step.target,
                        "reason": step.reason,
                        "requires_human": step.requires_human,
                    }
                    for step in decision.plan.steps
                ],
                "uncertain_actions": [
                    {
                        "action_id": a.action_id,
                        "action_type": a.action_type,
                        "status": a.status.value,
                    }
                    for a in decision.uncertain_actions
                ],
                "progress": {
                    "completed": decision.state.progress.completed,
                    "pending": decision.state.progress.pending,
                    "failed": decision.state.progress.failed,
                    "total": decision.state.progress.total,
                },
                "tail_evidence": tail_evidence,
                "informed_retry": decision.informed_retry,
                "contract": decision.contract.model_dump(mode="json"),
                "contract_text": render_contract(decision.contract),
                "report": decision.render(),
                **self_report_guidance(decision),
            }
        )

    @server.tool(
        name="continuum_confirm",
        description=(
            "Confirm a run's self-reported goal and progress so it can resume. "
            "MCP/agent-reported runs are self_certified and would otherwise be "
            "stuck at request_human forever. REFUSED unless the server operator "
            "set CONTINUUM_MCP_CONFIRM_TOKEN and you present that secret in the "
            "handshake _meta.authToken: an agent must not confirm its own "
            "self-reported state. The normal path is for a human to run "
            "'continuum confirm <run_id>' on the host. Mutates the run."
        ),
        annotations=mutating,
    )
    @confirm_gate
    def continuum_confirm(
        run_id: str,
        expected_model: str | None = None,
    ) -> str:
        """Record a human confirmation of self-reported state."""
        ctx.storage.append_event(
            run_id,
            EventType.REVIEW_CONFIRMED,
            {"components": ["goal", "progress"]},
            source=Origin.HUMAN,
        )
        decision = ctx.adapter.resume(
            run_id,
            expected_model=expected_model,
        )
        return _json(
            {
                "run_id": run_id,
                "mode": decision.mode.value,
                "safe": decision.safe,
                "next_allowed_action": decision.next_allowed_action,
                "report": decision.render(),
            }
        )

    # -- side effects ----------------------------------------------------- #

    @server.tool(
        name="continuum_intercept_action",
        description=(
            "Ask permission before performing an external side effect (creating an "
            "issue, sending a message, charging a card). Returns proceed=true if you "
            "should do it, or proceed=false with the previous result if it was "
            "already done — do NOT repeat it in that case. If a previous attempt was "
            "interrupted, returns proceed=false with status='unknown': the effect may "
            "or may or may not have happened, so stop and ask a human. After performing the "
            "action, always call continuum_complete_action.\n\n"
            "Pass a stable `key` identifying the specific operation (for example the "
            "invoice id or external resource id), not incidental formatting. The key "
            "is what makes two attempts count as the same action, so reuse the exact "
            "same key for the same operation across sessions; a key derived from the "
            "resource identity makes deduplication immune to argument formatting "
            "differences (relative vs absolute paths, argument naming, and so on)."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_intercept_action(
        run_id: str,
        action_type: str,
        arguments: dict[str, Any] | None = None,
        key: str | None = None,
        scoped_to_run: bool = True,
        pinning: dict[str, Any] | None = None,
        grant: dict[str, Any] | None = None,
    ) -> str:
        """Claim an action in the ledger and report whether to proceed."""
        from continuum.actions.grants import GrantDenied, normalize_grant
        from continuum.actions.idempotency import idempotency_key
        from continuum.pinning import normalize_pinning

        pinning_clean = normalize_pinning(pinning)
        ctx.ensure_run(run_id)

        # Run-level retry budget (issue #240): every claim slot counts as one
        # attempt, so a model re-planning after failures hits the wall here
        # instead of hammering the upstream.
        from pathlib import Path as _Path

        from continuum.budgets import (
            DEFAULT_BUDGETS_PATH,
            BudgetConfigError,
            attempts_for_type,
            evaluate_budget,
        )

        try:
            from continuum.budgets import load_budgets as _lb

            budgets = _lb(_Path(DEFAULT_BUDGETS_PATH))
        except BudgetConfigError as exc:
            return _json(
                {
                    "run_id": run_id,
                    "action_type": action_type,
                    "proceed": False,
                    "reason": f"retry budget registry invalid: {exc}",
                }
            )

        ledger = ctx.ledger(run_id)

        # The budget may only gate a claim that would open a *new* attempt
        # slot. Re-claiming an action that already reached a terminal-or-frozen
        # state is not an attempt: a COMPLETED record returns the stored result
        # (the whole point of idempotency), and an UNKNOWN one raises
        # UnknownSideEffect asking for reconciliation. Gating either would make
        # an exhausted budget suppress the dedup and reconciliation paths a
        # recovering agent depends on, turning a safety limit into the cause of
        # a duplicate side effect (issue #309).
        existing = ledger.get(
            idempotency_key(
                action_type,
                arguments,
                scope=run_id if scoped_to_run else None,
                key=key,
            )
        )
        settled = existing is not None and existing.status in (
            ActionStatus.COMPLETED,
            ActionStatus.UNKNOWN,
        )

        if not settled:
            events = ctx.storage.read_events(run_id)
            attempts = attempts_for_type(events, action_type)
            allowed, used, maximum = evaluate_budget(budgets, action_type, attempts)
            if not allowed:
                from mcp.server.mcpserver.exceptions import ToolError

                raise ToolError(
                    f"retry budget exhausted for {action_type!r}: "
                    f"{used} attempt(s) recorded, budget is {maximum}. "
                    "Reconcile existing attempts or ask the operator to raise "
                    ".continuum/budgets.json."
                )

        try:
            grant_clean = normalize_grant(grant)
            outcome = ledger.claim(
                action_type,
                arguments=arguments,
                key=key,
                scoped_to_run=scoped_to_run,
                pinning=pinning_clean or None,
                grant=grant_clean,
            )
        except GrantDenied as exc:
            return _json(
                {
                    "run_id": run_id,
                    "action_type": action_type,
                    "proceed": False,
                    "reason_code": "grant_denied",
                    "grant_id": exc.grant_id,
                    "reason": str(exc),
                    "guidance": (
                        "This single-use authority was already consumed (recorded "
                        "in the ledger); it does not come back after a restore. "
                        "Ask the operator for a fresh grant."
                    ),
                }
            )
        except UnknownSideEffect as exc:
            return _json(
                {
                    "run_id": run_id,
                    "action_type": action_type,
                    "proceed": False,
                    "status": ActionStatus.UNKNOWN.value,
                    "reason": str(exc),
                    "guidance": (
                        "A previous attempt was interrupted and its outcome is "
                        "unknown. Do not retry. Verify with the external system "
                        "whether it happened, then report via "
                        "continuum_reconcile_action."
                    ),
                }
            )

        if outcome.fresh:
            return _json(
                {
                    "run_id": run_id,
                    "action_type": action_type,
                    "proceed": True,
                    "action_key": str(outcome.key),
                    "status": outcome.action.status.value,
                    "guidance": (
                        "Perform the action now, then call continuum_complete_action "
                        "with this action_key."
                    ),
                }
            )

        return _json(
            {
                "run_id": run_id,
                "action_type": action_type,
                "proceed": False,
                "action_key": str(outcome.key),
                "status": outcome.action.status.value,
                "external_id": outcome.external_id,
                "previous_result": dict(outcome.result) if outcome.result else None,
                "guidance": "Already performed. Reuse the previous result; do not repeat it.",
            }
        )

    @server.tool(
        name="continuum_complete_action",
        description=(
            "Report that an intercepted action succeeded. Call this immediately "
            "after performing the side effect, using the action_key returned by "
            "continuum_intercept_action. Skipping it leaves the action uncertain "
            "and blocks recovery."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_complete_action(
        run_id: str,
        action_key: str,
        external_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> str:
        """Mark a claimed action as completed."""
        action = ctx.ledger(run_id).complete(action_key, external_id=external_id, result=result)
        return _json(
            {
                "run_id": run_id,
                "action_id": action.action_id,
                "action_type": action.action_type,
                "status": action.status.value,
                "external_id": action.external_id,
            }
        )

    @server.tool(
        name="continuum_fail_action",
        description=(
            "Report that an intercepted action failed. Set certain=true only if you "
            "know nothing happened (e.g. the request was rejected before it was "
            "sent). For timeouts or dropped connections leave certain=false — the "
            "effect may still have landed, and treating it as failed could cause a "
            "duplicate."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_fail_action(
        run_id: str,
        action_key: str,
        error: str,
        certain: bool = False,
    ) -> str:
        """Mark a claimed action as failed or uncertain."""
        action = ctx.ledger(run_id).fail(action_key, error, certain=certain)
        return _json(
            {
                "run_id": run_id,
                "action_id": action.action_id,
                "status": action.status.value,
                "side_effect_uncertain": action.side_effect_uncertain,
            }
        )

    @server.tool(
        name="continuum_reconcile_action",
        description=(
            "Settle an action whose outcome was unknown, after checking the external "
            "system. occurred=true records it as done (never repeated); "
            "occurred=false frees it to be retried. Only call this with real "
            "evidence — guessing here causes either a duplicate or lost work."
        ),
        annotations=mutating,
    )
    @guard
    def continuum_reconcile_action(
        run_id: str,
        action_key: str,
        occurred: bool,
        external_id: str | None = None,
        note: str = "",
    ) -> str:
        """Resolve an uncertain action using external evidence."""
        action = ctx.ledger(run_id).reconcile(
            action_key, occurred=occurred, external_id=external_id, note=note
        )
        return _json(
            {
                "run_id": run_id,
                "action_id": action.action_id,
                "status": action.status.value,
                "external_id": action.external_id,
                "side_effect_uncertain": action.side_effect_uncertain,
            }
        )

    @server.tool(
        name="continuum_list_actions",
        description=(
            "List external side effects recorded for a run. Each row carries "
            "'outcome_unresolved': true when that action's real-world outcome is "
            "not known and must be reconciled before resuming. Read-only."
        ),
        annotations=read_only,
    )
    def continuum_list_actions(run_id: str) -> str:
        """List ledger entries for a run."""
        # Read-only: do not call `ensure_run`, which backfills RUN_STARTED when
        # the log is empty. A bare run (row exists, no history yet) is valid
        # here and just has no actions. Fetching the row raises RunNotFound for
        # a genuinely unknown run without writing anything.
        ctx.storage.get_run(run_id)
        ledger = ctx.ledger(run_id)
        actions = ledger.all()
        unresolved = {a.action_id for a in ledger.pending()}
        return _json(
            {
                "run_id": run_id,
                "actions": [
                    {
                        "action_id": a.action_id,
                        "action_type": a.action_type,
                        "status": a.status.value,
                        "external_id": a.external_id,
                        "side_effect_uncertain": a.side_effect_uncertain,
                        # The durable flag above is only set once an action has
                        # been *escalated* to UNKNOWN. An action still STARTED
                        # because the process died mid-flight has not been
                        # escalated yet, so the flag reads false while the
                        # outcome is in fact unresolved — which is what a
                        # recovering caller needs to see per row, not just in
                        # the aggregate count.
                        "outcome_unresolved": a.action_id in unresolved,
                    }
                    for a in actions
                ],
                "unresolved": len(unresolved),
            }
        )

    if os.environ.get("CONTINUUM_MCP_SLIM") == "1":
        keep = {"continuum_resume", "continuum_validate", "continuum_confirm"}
        for name in list(server._tool_manager._tools.keys()):
            if name not in keep:
                del server._tool_manager._tools[name]

    return server, ctx


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _reject_reused_confirmation_secret(auth: AuthPolicy, confirm_auth: ConfirmPolicy) -> None:
    """Refuse a configuration where one secret unlocks both progress and confirmation.

    The confirmation gate exists so that a caller trusted to record progress is
    not automatically trusted to certify it (issue #201). If the operator sets
    ``CONTINUUM_MCP_CONFIRM_TOKEN`` to the same value as the session secret, or
    to any per-client token, every holder of a mutating credential becomes a
    holder of the confirmation credential and the gate protects nothing. That
    is a configuration mistake, not a decision, so it fails fast at startup.
    """
    if confirm_auth.disabled or auth.disabled:
        return
    expected = confirm_auth.expected
    assert expected is not None  # disabled is checked above
    overlaps = [name for name, secret in (auth.tokens or {}).items() if secret == expected]
    if auth.expected == expected:
        overlaps.append("<shared session secret>")
    if overlaps:
        raise ValueError(
            f"{CONFIRM_ENV_VAR} must be distinct from every mutating credential; "
            f"it matches: {', '.join(overlaps)}. Reusing one secret would let an "
            f"agent that records progress also confirm it, which is what the "
            f"confirmation gate exists to prevent."
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``continuum-mcp``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="continuum-mcp",
        description="MCP server exposing CONTINUUM's durable recovery tools.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"storage path (default: ${_DB_ENV_VAR} or ./{DEFAULT_DB})",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="MCP transport (default: stdio)",
    )
    args = parser.parse_args(argv)

    try:
        server, ctx = build_server(args.db)
    except ModuleNotFoundError as exc:
        # The console script ships with the base package but the SDK it needs
        # does not, so "installed but unimportable" is a normal state to land
        # in rather than a broken build. Narrowed to the SDK itself: a missing
        # transitive dependency of some other package is a different fault and
        # must keep its traceback instead of being blamed on the extra.
        if exc.name != "mcp" and not (exc.name or "").startswith("mcp."):
            raise
        print(
            f"error: the MCP server needs the optional 'mcp' dependency, which is "
            f"not importable ({exc}). Install it with: pip install 'continuum[mcp]'",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        # A malformed policy file or token list is an operator mistake, so it is
        # reported the way the CLI reports the same class of failure (see
        # cli/main.py): a traceback would bury the useful part. It matters more
        # here than there, because a stdio server writes its traceback into the
        # protocol pipe, where the client surfaces it only as "not ready" with
        # no indication of what to fix.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # resolve_database, not args.db, so the reported path is the one that
        # was actually opened rather than None when --db was omitted.
        #
        # Quoted with literal delimiters rather than !r (issue #94), matching
        # cli/main.py: repr() escapes each backslash, so a Windows path came
        # back doubled and could not be copied into a shell or a config file.
        print(
            f"error: cannot open storage at '{resolve_database(args.db)}': {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        server.run(transport=args.transport)
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
