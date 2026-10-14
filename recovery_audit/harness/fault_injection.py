"""Deterministic fault-injection harness for the CONTINUUM recovery-safety audit.

See `recovery_audit/HARNESS_DESIGN.md` for the design and the seam table this
module implements. Summary:

- ``ExternalWorld``  - the fake external system. Records every attempt; an effect
  either lands immediately and atomically or raises. Nothing in the fake world has
  a window, so the only crash windows come from the ledger's real code paths.
- ``Crash`` / ``CrashPoint`` / ``CrashingStorage`` - a ``Crash`` is a
  ``BaseException`` so the library's broad ``except Exception`` handlers cannot
  swallow it. ``CrashingStorage`` IS a real ``SQLiteStorage`` (it subclasses it,
  so WAL / synchronous=FULL / hash chaining / BEGIN IMMEDIATE + ROLLBACK all still
  apply) that raises ``Crash`` on the Nth call to a named method. Counts are
  absolute from storage creation, which makes them predictable: the scenario setup
  is always append #1, so a claim's ACTION_RECORDED is #2 and a completion is #3.
- ``Worker.attempt(crash_at=...)`` - crashes at a *worker step*, which is how
  SEAM-1 (after claim's durable write, before the effect) and SEAM-2 (after the
  effect, before complete's durable write) are expressed. A SIGKILL at that moment
  leaves exactly the writes the worker has performed so far, so raising
  immediately after the step is a faithful simulation.
- ``ScenarioResult`` - a three-valued verdict (SAFE / UNSAFE / UNKNOWN_OUTCOME).

Everything is deterministic: no wall clock, no sleeps, no threads, no MagicMock.
Crashes are *counted*, never timed.

This module lives in the audit artifact tree, not in the repo's ``tests/``
directory, and it modifies no production code.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Sequence

from continuum.actions.ledger import ActionLedger
from continuum.concurrency.lease import InMemoryLeaseCoordinator
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run
from continuum.storage.sqlite import SQLiteStorage

# ---------------------------------------------------------------------------
# Crash
# ---------------------------------------------------------------------------


class Crash(BaseException):
    """Simulated process death.

    A ``BaseException``, deliberately: the ledger's budget helpers and metrics
    counters all swallow ``Exception``, so a crash that was one would be a silent
    no-op and the scenario would prove nothing.
    """


@dataclass(frozen=True)
class CrashPoint:
    """Where a *storage-level* crash lands.

    ``method`` is the storage method that raises on its ``n``th call since the
    storage was created. ``after`` means the crash fires as call ``n`` *returns*
    (a gap between two durable writes - SEAM-3/SEAM-4); ``inside`` means it fires
    as call ``n`` *enters* (the write never commits, because
    ``SQLiteStorage._write`` issues ``ROLLBACK`` for any ``BaseException``).

    Worker-step crashes (SEAM-1/SEAM-2) are NOT expressed this way; they use
    ``Worker.attempt(crash_at=...)``, because those windows fall between a storage
    write and a non-storage call and cannot be counted on the storage surface.
    """

    method: str
    n: int
    when: Literal["after", "inside"] = "after"


# ---------------------------------------------------------------------------
# ExternalWorld
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    """One call into the external system, recorded for the execution trace."""

    business_key: str
    payload: dict[str, Any]
    landed: bool
    error: str | None = None


class EvidenceUnavailable(Exception):
    """The probe cannot determine whether the effect landed (SEAM-9)."""


class ExternalWorld:
    """The deterministic fake external system.

    An accounting ledger, not a mock. ``perform`` is immediate and atomic by
    construction: there is deliberately no window inside the fake world, so any
    duplicate landing observed in a scenario was produced by the ledger, not by
    the fake. Unconfigured behaviour raises rather than returning a
    plausible-looking object.

    Idempotency is configurable *per business key* because real external systems
    split into two classes and the audit must be able to express both:

    - **Non-idempotent by default** (send an email, post a message): a second
      ``perform`` is a genuine duplicate side effect and appends a second landing.
      This is the dangerous class the mission is about.
    - **Idempotent** (a payment provider keyed on an idempotency token, listed in
      ``idempotent``): a second ``perform`` returns the same receipt and lands
      nothing new. The ledger still *asked*, which ``attempts`` records - itself a
      finding worth reporting, even when the external system absorbed it.
    """

    def __init__(
        self,
        *,
        failures: Sequence[str] = (),
        idempotent: Sequence[str] = (),
        liars: Sequence[str] = (),
        unavailable: Sequence[str] = (),
        external_ids: dict[str, str] | None = None,
    ) -> None:
        self.attempts: list[Attempt] = []
        # business_key -> the canonical receipt for its FIRST landing.
        self.occurred: dict[str, str] = {}
        # Every landing, in order. len > len(occurred) means a duplicate side
        # effect actually happened. This is the UNSAFE signal.
        self.landing_order: list[str] = []
        self.failures = set(failures)
        self.idempotent = set(idempotent)
        # Probes configured to report the opposite of the truth.
        self.liars = set(liars)
        # Probes configured to be unable to answer at all.
        self.unavailable = set(unavailable)
        self._external_ids = dict(external_ids or {})
        self._next_id = Counter()
        self.trace: list[str] = []

    # -- effects --------------------------------------------------------- #

    def perform(self, business_key: str, payload: dict[str, Any] | None = None) -> str:
        """Perform the side effect. Lands atomically or raises."""
        payload = dict(payload or {})
        if business_key in self.failures:
            self.attempts.append(
                Attempt(business_key, payload, landed=False, error="configured failure")
            )
            self.trace.append(
                f"world.perform({business_key!r}) -> RAISE configured failure"
            )
            raise RuntimeError(f"external system rejected {business_key!r}")
        if business_key in self.occurred and business_key in self.idempotent:
            # The external system dedupes on its own key. No new side effect, but
            # the ledger still asked - record the call.
            self.attempts.append(
                Attempt(business_key, payload, landed=True, error="deduped-by-world")
            )
            self.trace.append(
                f"world.perform({business_key!r}) -> IDEMPOTENT return "
                f"{self.occurred[business_key]} (no new side effect)"
            )
            return self.occurred[business_key]
        ext = self._mint_id(business_key)
        first = business_key not in self.occurred
        self.occurred.setdefault(business_key, ext)
        self.landing_order.append(business_key)
        self.attempts.append(Attempt(business_key, payload, landed=True))
        self.trace.append(
            f"world.perform({business_key!r}) -> LANDED external_id={ext}"
            + ("" if first else "  <<< DUPLICATE SIDE EFFECT")
        )
        return ext

    def _mint_id(self, business_key: str) -> str:
        if business_key in self._external_ids:
            return self._external_ids[business_key]
        self._next_id[business_key] += 1
        return f"ext_{business_key}_{self._next_id[business_key]}"

    # -- probing --------------------------------------------------------- #

    def receipt(self, business_key: str) -> str | None:
        """Probe the external system for whether the effect landed.

        Returns the external id if it did, ``None`` if it did not. Can be
        configured to lie (``liars``) or to be unable to answer (``unavailable``).
        """
        if business_key in self.unavailable:
            self.trace.append(f"world.receipt({business_key!r}) -> EVIDENCE UNAVAILABLE")
            raise EvidenceUnavailable(
                f"cannot determine the outcome of {business_key!r} from the external system"
            )
        truth = self.occurred.get(business_key)
        if business_key in self.liars:
            reported = None if truth is not None else "ext_lie"
            self.trace.append(
                f"world.receipt({business_key!r}) -> LYING, reports {reported!r} "
                f"(truth {truth!r})"
            )
            return reported
        self.trace.append(f"world.receipt({business_key!r}) -> {truth!r}")
        return truth

    # -- accounting ------------------------------------------------------ #

    @property
    def execution_count(self) -> int:
        """Total landings. Exceeding ``distinct_keys`` means a duplicate external
        side effect actually happened - the UNSAFE signal."""
        return len(self.landing_order)

    @property
    def distinct_keys(self) -> int:
        return len(self.occurred)

    @property
    def duplicate_executions(self) -> int:
        return self.execution_count - self.distinct_keys

    def landings_of(self, business_key: str) -> int:
        return self.landing_order.count(business_key)

    def attempts_of(self, business_key: str) -> int:
        return sum(1 for a in self.attempts if a.business_key == business_key)

    def recover(self, *business_keys: str) -> None:
        """Stop failing the named keys, so a retry can actually succeed.

        A permanent failure makes a retry untestable: the assertion "the retry
        landed" is set up to be impossible, which hides whether the ledger
        cooperated. Real retries happen because the external system came back, so
        the scenario says so explicitly rather than leaving the failure on and
        hoping. With no arguments every key recovers.
        """
        if not business_keys:
            self.failures.clear()
        else:
            self.failures.difference_update(business_keys)

    def reset_trace(self) -> None:
        self.trace = []


# ---------------------------------------------------------------------------
# CrashingStorage
# ---------------------------------------------------------------------------


class CrashingStorage(SQLiteStorage):
    """A real SQLiteStorage that raises ``Crash`` at a counted injection point.

    It subclasses ``SQLiteStorage`` rather than wrapping it, so every production
    durability property (WAL, ``synchronous=FULL``, hash chaining, ``BEGIN
    IMMEDIATE`` with rollback on any exception) applies unchanged. Only the
    instrumented methods are overridden; everything else is inherited.

    Counts are absolute from storage creation. The scenario setup always performs
    exactly one append (RUN_STARTED), so an author can name a crash point without
    instrumenting the call sequence first: the claim's ACTION_RECORDED is #2, the
    completion's ACTION_RECORDED is #3.

    A crash raised *inside* a write is equivalent to a SIGKILL that arrived before
    commit, because ``SQLiteStorage._write`` issues ``ROLLBACK`` for any
    ``BaseException``. A crash raised *after* a call returns models a gap between
    two durable writes. ``CrashPoint.when`` selects between the two.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._crash_point: CrashPoint | None = None
        self._calls: Counter[str] = Counter()
        self._crashed_at: CrashPoint | None = None
        # Durable work performed in a finally/__exit__/close during the crash
        # window would make an exception-based crash simulation unsound. The
        # harness asserts this stays empty; if it ever does not, the *harness* is
        # broken and every verdict it produced is suspect.
        self.cleanup_ran: list[str] = []

    def arm(self, point: CrashPoint) -> None:
        # Deliberately does NOT reset _calls: counts are absolute from creation.
        self._crash_point = point

    @property
    def crashed(self) -> bool:
        return self._crashed_at is not None

    @property
    def call_counts(self) -> dict[str, int]:
        return dict(self._calls)

    # -- instrumented storage surface ------------------------------------ #
    # Every method the ledger calls during claim/complete/fail/assess is
    # instrumented. Methods not listed are inherited unchanged.

    @contextmanager
    def _instrument(self, method: str) -> Iterator[None]:
        """Count a call, and raise Crash at the chosen moment within it.

        ``inside`` raises before the delegated call runs, so the write never
        commits; ``after`` raises once it has returned, so the write is durable
        and the crash lands in the gap following it.
        """
        self._enter(method)
        try:
            yield
        except Crash:
            raise
        self._exit(method)

    def _enter(self, method: str) -> None:
        # Count every call, armed or not, so an author can arm late and still name
        # a point absolutely: the scenario setup append is always #1.
        self._calls[method] += 1
        point = self._crash_point
        if point is None or self.crashed or point.when != "inside":
            return
        if method != point.method:
            return
        if self._calls[method] == point.n:
            self._crashed_at = point
            # The call never completed, so do not charge it.
            self._calls[method] -= 1
            raise Crash(
                f"simulated process death inside {method}() call #{point.n}"
            )

    def _exit(self, method: str) -> None:
        point = self._crash_point
        if point is None or self.crashed or point.when != "after":
            return
        if method != point.method:
            return
        if self._calls[method] == point.n:
            self._crashed_at = point
            raise Crash(
                f"simulated process death after {method}() call #{point.n} returned"
            )

    def append_event(self, *args: Any, **kwargs: Any) -> Any:
        with self._instrument("append_event"):
            return super().append_event(*args, **kwargs)

    def read_events(self, *args: Any, **kwargs: Any) -> Any:
        with self._instrument("read_events"):
            return super().read_events(*args, **kwargs)

    def read_all_events(self, *args: Any, **kwargs: Any) -> Any:
        with self._instrument("read_all_events"):
            return super().read_all_events(*args, **kwargs)

    def read_archived_events(self, *args: Any, **kwargs: Any) -> Any:
        with self._instrument("read_archived_events"):
            return super().read_archived_events(*args, **kwargs)

    def append_sealed(self, *args: Any, **kwargs: Any) -> Any:
        with self._instrument("append_sealed"):
            return super().append_sealed(*args, **kwargs)

    def extend_events(self, *args: Any, **kwargs: Any) -> Any:
        with self._instrument("extend_events"):
            return super().extend_events(*args, **kwargs)

    def __exit__(self, *exc: Any) -> Any:
        if self._crash_point is not None and not self.crashed:
            self.cleanup_ran.append("__exit__")
        return super().__exit__(*exc)

    def close(self) -> None:
        # close() during a crash window would be cleanup a real crash never ran.
        if self._crash_point is not None and not self.crashed:
            self.cleanup_ran.append("close")
        super().close()


# ---------------------------------------------------------------------------
# InterleavingStorage (S5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterleavePoint:
    """Run ``callback`` just before the ``n``th call to ``method``.

    Used to expose the read-then-write race in ``ActionLedger.claim``: crashing
    cannot show it, because both halves of the race are a read and a write inside
    one process. This wrapper inserts worker B's entire claim into the middle of
    worker A's claim, at a counted point, with no timing involved.
    """

    method: str
    n: int


class InterleavingStorage(SQLiteStorage):
    """A real SQLiteStorage that runs a callback at a counted call.

    Like ``CrashingStorage`` it subclasses the real storage, so durability
    properties apply unchanged. The callback runs *before* the delegated call, so
    its writes land first and the interleaving is fully deterministic.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._point: InterleavePoint | None = None
        self._callback: Callable[[], Any] | None = None
        self._calls: Counter[str] = Counter()
        self.fired = False

    def arm(self, point: InterleavePoint, callback: Callable[[], Any]) -> None:
        self._point = point
        self._callback = callback

    def _maybe(self, method: str) -> None:
        # Count every call, armed or not, so a point can be named absolutely
        # from storage creation regardless of when the callback was armed.
        self._calls[method] += 1
        if self._point is None or self.fired or self._callback is None:
            return
        if method != self._point.method:
            return
        if self._calls[method] == self._point.n:
            self.fired = True
            self._callback()

    def append_event(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("append_event")
        return super().append_event(*args, **kwargs)

    def read_events(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("read_events")
        return super().read_events(*args, **kwargs)

    def read_all_events(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("read_all_events")
        return super().read_all_events(*args, **kwargs)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


# Where a worker-step crash may land. Each names a moment in the action loop
# whose durable state is exactly what a SIGKILL at that moment would leave.
WorkerCrash = Literal["after-claim", "after-effect", "after-complete"]


@dataclass
class AttemptResult:
    """What one worker attempt produced."""

    key: str | None
    status: ActionStatus | None
    external_id: str | None
    error: str | None = None
    crashed: bool = False


class Worker:
    """The smallest faithful caller of the action ledger.

    claim -> external effect -> complete / fail, in that order, with no retry
    cleverness. Any retry is an explicit scenario step, so a safety failure can
    never be hidden behind a clever caller (mission rule 7).
    """

    def __init__(
        self,
        storage: CrashingStorage,
        world: ExternalWorld,
        run_id: str,
        *,
        holder_id: str = "worker-A",
        lease: InMemoryLeaseCoordinator | None = None,
    ) -> None:
        self.storage = storage
        self.world = world
        self.run_id = run_id
        self.holder_id = holder_id
        self.lease = lease
        self.ledger = ActionLedger(storage, run_id, lease=lease, holder_id=holder_id)
        self.trace: list[str] = []

    # -- ledger convenience --------------------------------------------- #

    def action(self, key: str | None) -> Action | None:
        return self.ledger.get(key) if key else None

    def status(self, key: str | None) -> ActionStatus | None:
        a = self.action(key)
        return a.status if a else None

    # -- the action loop ------------------------------------------------- #

    def attempt(
        self,
        action_type: str,
        arguments: dict[str, Any],
        business_key: str,
        *,
        fail_mode: Literal["raise", "fail", "uncertain"] = "raise",
        key: str | None = None,
        on_unknown: Callable[[Any], Any] | None = None,
        crash_at: WorkerCrash | None = None,
        scoped_to_run: bool = True,
    ) -> AttemptResult:
        """One full claim -> effect -> settle pass.

        ``fail_mode`` selects the caller behaviour when the external call raises,
        which produces materially different durable states: ``raise`` (propagate),
        ``fail`` (``ledger.fail`` records FAILED), or ``uncertain``
        (``ledger.fail(certain=False)`` records UNKNOWN).

        ``crash_at`` raises :class:`Crash` immediately after that step. This is how
        SEAM-1 (``after-claim``) and SEAM-2 (``after-effect``) are expressed: a
        SIGKILL at that moment leaves exactly the writes performed so far.
        """
        self.trace.append(
            f"{self.holder_id}: claim({action_type!r}, key={business_key!r})"
        )
        outcome = self.ledger.claim(
            action_type,
            arguments,
            key=key,
            on_unknown=on_unknown,
            scoped_to_run=scoped_to_run,
        )
        if not outcome.fresh:
            self.trace.append(
                f"{self.holder_id}: claim returned fresh=False "
                f"status={outcome.action.status.value}"
            )
            return AttemptResult(
                key=outcome.key,
                status=outcome.action.status,
                external_id=outcome.action.external_id,
            )
        self.trace.append(
            f"{self.holder_id}: claim fresh=True status={outcome.action.status.value}"
        )
        if crash_at == "after-claim":
            self.trace.append(
                f"{self.holder_id}: !!! CRASH after claim, before the effect"
            )
            raise Crash(
                f"simulated process death after claim() returned for "
                f"{business_key!r}, before the external effect"
            )
        try:
            ext = self.world.perform(business_key, arguments)
        except Exception as exc:
            self.trace.append(
                f"{self.holder_id}: world.perform raised {type(exc).__name__}: {exc}"
            )
            if fail_mode == "raise":
                return AttemptResult(
                    key=outcome.key,
                    status=self.status(outcome.key),
                    external_id=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if fail_mode == "fail":
                a = self.ledger.fail(outcome.key, f"{type(exc).__name__}: {exc}")
                self.trace.append(
                    f"{self.holder_id}: ledger.fail -> {a.status.value}"
                )
                return AttemptResult(
                    key=outcome.key,
                    status=a.status,
                    external_id=None,
                    error=str(exc),
                )
            a = self.ledger.fail(
                outcome.key, f"{type(exc).__name__}: {exc}", certain=False
            )
            self.trace.append(
                f"{self.holder_id}: ledger.fail(certain=False) -> {a.status.value}"
            )
            return AttemptResult(
                key=outcome.key, status=a.status, external_id=None, error=str(exc)
            )
        self.trace.append(f"{self.holder_id}: effect landed external_id={ext}")
        if crash_at == "after-effect":
            self.trace.append(
                f"{self.holder_id}: !!! CRASH after the effect, before complete()"
            )
            raise Crash(
                f"simulated process death after the external effect landed for "
                f"{business_key!r}, before complete() recorded it"
            )
        a = self.ledger.complete(outcome.key, external_id=ext, result={"ok": True})
        self.trace.append(
            f"{self.holder_id}: ledger.complete -> {a.status.value} external_id={ext}"
        )
        if crash_at == "after-complete":
            self.trace.append(
                f"{self.holder_id}: !!! CRASH after complete() committed"
            )
            raise Crash(
                f"simulated process death after complete() committed for "
                f"{business_key!r}"
            )
        return AttemptResult(key=outcome.key, status=a.status, external_id=ext)

    def reconcile_from_probe(
        self, key: str, business_key: str
    ) -> AttemptResult:
        """Settle an uncertain action using the external probe (SEAM-9)."""
        self.trace.append(f"{self.holder_id}: reconcile({business_key!r})")
        try:
            ext = self.world.receipt(business_key)
        except EvidenceUnavailable as exc:
            self.trace.append(f"{self.holder_id}: probe unavailable: {exc}")
            return AttemptResult(
                key=key, status=self.status(key), external_id=None, error=str(exc)
            )
        if ext is not None:
            a = self.ledger.reconcile(
                key, occurred=True, external_id=ext, note="probe confirmed"
            )
            self.trace.append(
                f"{self.holder_id}: reconcile(occurred=True) -> {a.status.value}"
            )
            return AttemptResult(key=key, status=a.status, external_id=ext)
        a = self.ledger.reconcile(
            key, occurred=False, note="probe found no effect"
        )
        self.trace.append(
            f"{self.holder_id}: reconcile(occurred=False) -> {a.status.value}"
        )
        return AttemptResult(key=key, status=a.status, external_id=None)


# ---------------------------------------------------------------------------
# Scenario scaffolding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    """The verdict for one scenario.

    ``verdict`` is derived from ``executions``, not asserted by the scenario
    author: ``executions > 1`` for the same key is UNSAFE (a duplicate external
    side effect actually happened), a single safe settlement is SAFE, and a state
    where the outcome cannot be determined and must be escalated is
    UNKNOWN_OUTCOME - never flattened into a pass.
    """

    scenario_id: str
    verdict: Literal["SAFE", "UNSAFE", "UNKNOWN_OUTCOME"]
    executions: int
    recovery_action: str
    trace: list[str] = field(default_factory=list)
    assertions: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Assertions whose expected value is False because they are negations of a
    #: safety property ("no_duplicate", "the loser got no slot"). Evidence
    #: rendering uses this so a holding safety property is not reported as a
    #: defect, and the JSON carries it so a reader need not guess.
    expected_false: frozenset[str] = field(default_factory=frozenset)
    #: Descriptive outcomes rather than safety properties ("which worker won the
    #: race"). Printed as INFO, because either value is legitimate and labelling
    #: one of them FAIL would misreport a schedule detail as a defect.
    informational: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "verdict": self.verdict,
            "executions": self.executions,
            "recovery_action": self.recovery_action,
            "assertions": dict(self.assertions),
            "expected_false": sorted(self.expected_false),
            "informational": sorted(self.informational),
            "notes": list(self.notes),
            "trace": list(self.trace),
        }


class Scenario:
    """Owns one temp directory, one store, one world, and the worker lifecycles.

    ``crash`` runs ``fn`` with a crash point armed, catches the ``Crash`` and then
    abandons every in-memory object. ``restart`` rebuilds against the same
    on-disk artifacts - the simulation of a process restart. Only the
    ``ExternalWorld`` survives, which is correct: the external system is a separate
    process and keeps what it has.
    """

    def __init__(
        self,
        scenario_id: str,
        *,
        failures: Sequence[str] = (),
        idempotent: Sequence[str] = (),
        liars: Sequence[str] = (),
        unavailable: Sequence[str] = (),
        lease: bool = False,
        lease_clock: Any = None,
        lease_ttl_seconds: float = 5.0,
        budgets_path: str | Path | None = None,
    ) -> None:
        self.scenario_id = scenario_id
        self.tmpdir = tempfile.TemporaryDirectory(prefix=f"fi_{scenario_id}_")
        self.path = os.path.join(self.tmpdir.name, "run.db")
        self.world = ExternalWorld(
            failures=failures, idempotent=idempotent, liars=liars, unavailable=unavailable
        )
        self._lease_kwargs: dict[str, Any] = {}
        if lease:
            self._lease_kwargs = {"default_ttl": _timedelta(lease_ttl_seconds)}
            if lease_clock is not None:
                self._lease_kwargs["clock"] = lease_clock
        # The ledger reads CONTINUUM_BUDGETS_PATH once per budget operation, so
        # pointing it at a temp file lets a scenario configure attempt budgets
        # (SEAM-3/SEAM-4) without touching any production default.
        if budgets_path is not None:
            os.environ["CONTINUUM_BUDGETS_PATH"] = str(budgets_path)
        self._budgets_path = budgets_path
        self._run_created = False
        self.trace: list[str] = []

    # -- lifecycle ------------------------------------------------------- #

    def _storage(self) -> CrashingStorage:
        st = CrashingStorage(self.path)
        if not self._run_created:
            st.create_run(Run(run_id="r1", goal="audit scenario"))
            st.append_event("r1", EventType.RUN_STARTED, {"goal": "audit scenario"})
            self._run_created = True
        return st

    def worker(self, holder_id: str = "worker-A") -> Worker:
        lease = InMemoryLeaseCoordinator(**self._lease_kwargs) if self._lease_kwargs else None
        if lease is not None:
            # A lease only serializes when the holder has taken it; the scenario
            # acquires it explicitly so the ordering is visible in the trace.
            pass
        return Worker(
            self._storage(), self.world, "r1", holder_id=holder_id, lease=lease
        )

    def lease(self) -> InMemoryLeaseCoordinator | None:
        if not self._lease_kwargs:
            return None
        return InMemoryLeaseCoordinator(**self._lease_kwargs)

    def crash(
        self, point: CrashPoint, fn: Callable[[Worker], Any], worker: Worker
    ) -> None:
        """Run ``fn`` with ``point`` armed; abandon everything on crash.

        No ``close()``, no ``__exit__``, no flush. The storage object is dropped
        from the only reference holding it, which is what a SIGKILL leaves.
        """
        worker.storage.arm(point)
        try:
            fn(worker)
        except Crash as exc:
            self.trace.append(f"!!! CRASH: {exc}")
            self.trace.append(
                f"!!! abandoning worker={worker.holder_id} "
                f"storage_calls={worker.storage.call_counts}"
            )
            if worker.storage.cleanup_ran:
                self.trace.append(
                    f"!!! WARNING durability cleanup ran during the crash window: "
                    f"{worker.storage.cleanup_ran}"
                )
            return
        # Deliberately no finally: the crash path must not run any cleanup a real
        # SIGKILL would skip, and the no-crash path leaves the worker for the
        # caller to inspect.

    def crash_at_step(self, fn: Callable[[Worker], Any], worker: Worker) -> None:
        """Run ``fn`` expecting it to raise ``Crash`` at a worker step."""
        try:
            fn(worker)
        except Crash as exc:
            self.trace.append(f"!!! CRASH: {exc}")
            self.trace.append(
                f"!!! abandoning worker={worker.holder_id} "
                f"storage_calls={worker.storage.call_counts}"
            )
            if worker.storage.cleanup_ran:
                self.trace.append(
                    f"!!! WARNING durability cleanup ran during the crash window: "
                    f"{worker.storage.cleanup_ran}"
                )
            return
        raise RuntimeError(
            "crash_at_step: the worker step did not raise Crash - the scenario is "
            "misconfigured and would silently pass"
        )

    def restart(self, holder_id: str = "worker-A") -> Worker:
        """Rebuild against the durable artifacts. No object identity is shared."""
        w = self.worker(holder_id)
        self.trace.append(
            f"--- restart: new worker={holder_id} storage={type(w.storage).__name__}"
        )
        return w

    def events(self) -> list[dict[str, Any]]:
        """The raw durable event log - what actually survived."""
        st = SQLiteStorage(self.path)
        try:
            return [
                {"sequence": e.sequence, "type": e.type.value, "payload": e.payload}
                for e in st.read_events("r1")
            ]
        finally:
            st.close()

    def result(
        self,
        verdict: Literal["SAFE", "UNSAFE", "UNKNOWN_OUTCOME"],
        *,
        recovery_action: str,
        assertions: dict[str, bool] | None = None,
        notes: Sequence[str] = (),
        executions: int | None = None,
        expected_false: Sequence[str] = (),
        informational: Sequence[str] = (),
    ) -> ScenarioResult:
        full_trace = list(self.trace) + list(self.world.trace)
        return ScenarioResult(
            scenario_id=self.scenario_id,
            verdict=verdict,
            executions=self.world.execution_count
            if executions is None
            else executions,
            recovery_action=recovery_action,
            trace=full_trace,
            assertions=dict(assertions or {}),
            notes=list(notes),
            expected_false=frozenset(expected_false),
            informational=frozenset(informational),
        )

    def cleanup(self) -> None:
        if self._budgets_path is not None:
            os.environ.pop("CONTINUUM_BUDGETS_PATH", None)
        self.tmpdir.cleanup()

    # -- evidence -------------------------------------------------------- #

    def write_evidence(self, result: ScenarioResult, out_dir: Path) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{result.scenario_id}.json"
        path.write_text(json.dumps(result.as_dict(), indent=2) + "\n")
        txt = out_dir / f"{result.scenario_id}.txt"
        # An assertion is only a failure when it contradicts what the scenario
        # expected. Several load-bearing assertions are negations -- "no
        # duplicate", "the loser was not given a slot" -- and a False value is
        # the passing result there. Labelling those FAIL would report a safety
        # property holding as a defect, which is precisely the misreading the
        # audit exists to prevent. ``expected_false`` names them explicitly.
        expected_false = set(getattr(result, "expected_false", ()))
        informational = set(getattr(result, "informational", ()))
        judged = [
            (k, v)
            for k, v in result.assertions.items()
            if k not in informational
        ]
        txt.write_text(
            f"SCENARIO {result.scenario_id}\n"
            f"VERDICT {result.verdict}\n"
            f"EXECUTIONS {result.executions}\n"
            f"RECOVERY {result.recovery_action}\n\n"
            + "ASSERTIONS\n"
            + "".join(
                f"  [{'PASS' if v == (k not in expected_false) else 'FAIL'}] {k}"
                f"{' (expected_false)' if k in expected_false else ''}\n"
                for k, v in judged
                if isinstance(v, bool)
            )
            + "".join(
                f"  [INFO] {k} = {v!r}\n"
                for k, v in judged
                if not isinstance(v, bool)
            )
            + "".join(
                f"  [INFO] {k} = {result.assertions[k]!r}\n"
                for k in sorted(informational)
                if k in result.assertions
            )
            + "\nTRACE\n"
            + "".join(f"  {line}\n" for line in result.trace)
        )
        return path


def _timedelta(seconds: float) -> Any:
    import datetime

    return datetime.timedelta(seconds=seconds)


__all__ = [
    "Attempt",
    "AttemptResult",
    "Crash",
    "CrashPoint",
    "CrashingStorage",
    "EvidenceUnavailable",
    "ExternalWorld",
    "InterleavePoint",
    "InterleavingStorage",
    "Scenario",
    "ScenarioResult",
    "Worker",
    "WorkerCrash",
]
