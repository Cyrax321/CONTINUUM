"""Phase 6 recovery-correctness scenarios.

Each scenario exercises one stress condition against the real recovery
machinery (engine, validator, checkpoints, ledger, adapter funnel) and asserts
the system behaves safely. They are plain callables taking a ``ScenarioContext``
so they run both under pytest and the standalone benchmark script without
depending on a test framework.
"""

from __future__ import annotations

import time
import types
from dataclasses import replace
from typing import Any, NoReturn

from continuum.actions.ledger import ActionLedger
from continuum.adapters import recover, register_adapter
from continuum.adapters.base import AgentAdapter
from continuum.benchmark.phase6.harness import ScenarioContext, ScenarioFn
from continuum.checkpoint import CheckpointManager
from continuum.concurrency import InMemoryLeaseCoordinator
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import (
    EnvironmentSnapshot,
    EnvResource,
    RecoveryContract,
    RecoveryMode,
    RecoverySafety,
    Run,
    SemanticState,
    StateStatus,
)
from continuum.recovery import (
    DependencyGraph,
    MemoryLedgerBackend,
    RecoveryEngine,
    RecoveryLedger,
)
from continuum.recovery.ledger import LedgerLockError
from continuum.storage import SQLiteStorage
from continuum.testing import environment_fixture


def _new_store() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="scenario"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "scenario", "total": 10})
    return storage


def env_multi(**versions: str) -> EnvironmentSnapshot:
    """Build an environment snapshot from resource name to version pairs."""
    return capture(
        "run_1",
        StaticProvider(
            resources={
                name: EnvResource(name=name, version=value) for name, value in versions.items()
            }
        ),
    )


def seed_two(store: SQLiteStorage) -> None:
    """Two dependencies, each with its own evidence and finding."""
    store.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "dataset", "version": "v3"}
    )
    store.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "other", "version": "v3"}
    )
    store.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "ev_a", "summary": "a", "source": "dataset"},
    )
    store.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "ev_b", "summary": "b", "source": "other"},
    )
    store.append_event(
        "run_1", EventType.FINDING_ADDED, {"finding_id": "f_a", "claim": "A", "evidence": ["ev_a"]}
    )
    store.append_event(
        "run_1", EventType.FINDING_ADDED, {"finding_id": "f_b", "claim": "B", "evidence": ["ev_b"]}
    )
    CheckpointManager(store).checkpoint("run_1", environment=env_multi(dataset="v3", other="v3"))


def _state(store: SQLiteStorage) -> SemanticState:
    return CheckpointManager(store).restore("run_1").state


def _contract(version: int = 0) -> RecoveryContract:
    return RecoveryContract(
        run_id="run_1", checkpoint_version=version, recovery_status=RecoverySafety.REQUIRES_REPAIR
    )


# --- scenarios ------------------------------------------------------------- #


def scenario_single_dependency_corruption(ctx: ScenarioContext) -> None:
    """One stale dependency invalidates only its own subtree."""
    with environment_fixture(dependencies=("dataset", "other")) as fx:
        decision = fx.engine.assess(
            fx.run_id, current_environment=fx.capture(dataset="v2"), scope={"dataset"}
        )
    assert "dataset" in str(decision.contract.invalidated)
    other = decision.state.dependency("other")
    assert other is not None and other.status is StateStatus.VALID


def scenario_multi_dependency_corruption(ctx: ScenarioContext) -> None:
    """Stale dependencies are each reported invalid."""
    with environment_fixture(dependencies=("dataset", "other")) as fx:
        decision = fx.engine.assess(
            fx.run_id, current_environment=fx.capture(dataset="v2", other="v2")
        )
    assert "dataset" in str(decision.contract.invalidated)
    assert "other" in str(decision.contract.invalidated)


def scenario_external_edit_drift(ctx: ScenarioContext) -> None:
    """An external edit after anchoring reports drift on reconcile."""
    store = _new_store()
    seed_two(store)
    decision = RecoveryEngine(store).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v3"), scope={"dataset"}
    )
    ledger = RecoveryLedger(MemoryLedgerBackend())
    ledger.append_decision("run_1", decision.contract, anchor=True)
    behind = types.SimpleNamespace(version=decision.contract.checkpoint_version - 1)
    report = ledger.reconcile("run_1", behind)
    assert report.drift is True


def scenario_ledger_tamper_detected(ctx: ScenarioContext) -> None:
    """A tampered ledger entry fails verification at its index."""
    backend = MemoryLedgerBackend()
    ledger = RecoveryLedger(backend)
    ledger.append_decision("run_1", _contract(0))
    ledger.append_decision("run_1", _contract(1))
    entries = ledger.entries("run_1")
    tampered = replace(entries[0], note="hacked")
    backend.replace("run_1", [tampered, entries[1]])
    ok, broken_at = ledger.verify("run_1")
    assert ok is False and broken_at == 0


def scenario_recovery_lease_exhaustion(ctx: ScenarioContext) -> None:
    """Exhausted recovery attempts require a human."""
    ledger = RecoveryLedger(MemoryLedgerBackend())
    for _ in range(3):
        ledger.record_attempt("run_1")
    ctx.attempts = ledger.attempts("run_1")
    assert ledger.attempts("run_1") == 3
    assert ledger.requires_human("run_1", max_attempts=3) is True


def scenario_out_of_scope_side_effect(ctx: ScenarioContext) -> None:
    """An uncertain side effect tagged outside the repair scope must not block."""
    store = _new_store()
    seed_two(store)
    ledger = ActionLedger(store, "run_1")
    ledger.claim("model.push", {"x": 1}, dep_scope="other")
    decision = RecoveryEngine(store).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v3"), scope={"dataset"}
    )
    assert decision.mode is RecoveryMode.REPAIR_AND_RESUME
    assert decision.uncertain_actions == ()

    # Conservative counterpart: an untagged action still blocks the scoped call.
    ledger.claim("notify.slack", {"x": 2})
    rescope = RecoveryEngine(store).assess(
        "run_1", current_environment=env_multi(dataset="v4", other="v3"), scope={"dataset"}
    )
    assert rescope.mode is not RecoveryMode.RESUME
    assert len(rescope.uncertain_actions) == 1


def scenario_adapter_failure_across_environments(ctx: ScenarioContext) -> None:
    """An adapter failure during recovery surfaces instead of being swallowed."""

    class FailingAdapter(AgentAdapter):
        """Test double whose every capability raises."""

        def __init__(self, storage: object, **kwargs: Any) -> None:  # noqa: D401
            self._storage = storage

        def capture_state(self, *a: Any, **k: Any) -> NoReturn:
            """Raise instead of capturing state."""
            raise NotImplementedError

        def restore_state(self, *a: Any, **k: Any) -> NoReturn:
            """Raise instead of restoring state."""
            raise NotImplementedError

        def intercept_action(self, *a: Any, **k: Any) -> NoReturn:
            """Raise instead of intercepting an action."""
            raise NotImplementedError

        def resume(
            self,
            run_id: str,
            *,
            current_environment: Any = None,
            expected_model: Any = None,
            replay: bool = True,
        ) -> NoReturn:
            """Raise instead of resuming the run."""
            raise RuntimeError("adapter failure across environment")

    register_adapter("fail_p6", lambda: FailingAdapter)
    try:
        recover("fail_p6", "x", SQLiteStorage(":memory:"))
        ctx.fail("adapter failure was swallowed instead of surfaced")
    except RuntimeError:
        pass


def scenario_checkpoint_rollback_correctness(ctx: ScenarioContext) -> None:
    """Restoring without replay returns the checkpointed version."""
    store = _new_store()
    seed_two(store)
    mgr = CheckpointManager(store)
    target = mgr.restore("run_1").state.version
    store.append_event("run_1", EventType.WORK_COMPLETED, {})
    store.append_event("run_1", EventType.WORK_COMPLETED, {})
    rolled_back = mgr.restore("run_1", replay=False)
    assert rolled_back.checkpoint is not None
    assert rolled_back.state.version == target
    assert len(rolled_back.state.external_dependencies) == 2


def scenario_concurrent_recovery_safety(ctx: ScenarioContext) -> None:
    """A second holder cannot append while the lease is held."""
    coordinator = InMemoryLeaseCoordinator()
    assert coordinator.acquire("run_1", "holder-A")
    ledger = RecoveryLedger(MemoryLedgerBackend(), lock=coordinator, holder_id="holder-B")
    try:
        ledger.append_decision("run_1", _contract(0))
        ctx.fail("second holder appended while the lease was held")
    except LedgerLockError:
        pass
    coordinator.release("run_1", "holder-A")
    ledger.append_decision("run_1", _contract(0))
    assert len(ledger.entries("run_1")) == 1


def scenario_large_state_recovery_latency(ctx: ScenarioContext) -> None:
    """Assessing a hundred dependencies stays within the latency budget."""
    store = _new_store()
    n = 100
    for i in range(n):
        store.append_event(
            "run_1", EventType.DEPENDENCY_DECLARED, {"resource": f"dep{i}", "version": "v1"}
        )
        store.append_event(
            "run_1",
            EventType.EVIDENCE_ADDED,
            {"evidence_id": f"e{i}", "summary": "x", "source": f"dep{i}"},
        )
        store.append_event(
            "run_1",
            EventType.FINDING_ADDED,
            {"finding_id": f"f{i}", "claim": "x", "evidence": [f"e{i}"]},
        )
    base = {f"dep{i}": EnvResource(name=f"dep{i}", version="v1") for i in range(n)}
    CheckpointManager(store).checkpoint(
        "run_1", environment=capture("run_1", StaticProvider(resources=base))
    )
    current = capture(
        "run_1", StaticProvider(resources={**base, "dep0": EnvResource(name="dep0", version="v2")})
    )
    start = time.perf_counter()
    RecoveryEngine(store).assess("run_1", current_environment=current)
    elapsed = (time.perf_counter() - start) * 1000
    ctx.metrics["assess_ms"] = round(elapsed, 3)
    assert elapsed < 1000.0


def scenario_missing_dependency_graph_fallback(ctx: ScenarioContext) -> None:
    """Assessment without a dependency graph still yields a decision."""
    store = _new_store()
    seed_two(store)
    decision = RecoveryEngine(store).assess("run_1", current_environment=env_multi(other="v3"))
    assert decision is not None and decision.mode is not None
    impacted = DependencyGraph(_state(store)).impacted_by(set())
    assert impacted.all_items == frozenset()


def scenario_human_verdict_honored(ctx: ScenarioContext) -> None:
    """An approved human gate clears the pending gate."""
    ledger = RecoveryLedger(MemoryLedgerBackend())
    ledger.append_decision("run_1", _contract(0), gate="required")
    assert ledger.pending_gate("run_1") is not None
    ledger.record_gate("run_1", "approved")
    assert ledger.pending_gate("run_1") is None


def scenario_transient_network_failure_on_install(ctx: ScenarioContext) -> None:
    """A vanished dependency keeps the run unsafe to resume."""
    store = _new_store()
    store.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": "libx", "version": "v1"}
    )
    store.append_event(
        "run_1", EventType.EVIDENCE_ADDED, {"evidence_id": "ev_x", "summary": "x", "source": "libx"}
    )
    current = env_multi()
    decision = RecoveryEngine(store).assess("run_1", current_environment=current)
    assert decision.mode is not None
    assert decision.safe is False


ALL_SCENARIOS: list[tuple[str, ScenarioFn]] = [
    ("single_dependency_corruption", scenario_single_dependency_corruption),
    ("multi_dependency_corruption", scenario_multi_dependency_corruption),
    ("external_edit_drift", scenario_external_edit_drift),
    ("ledger_tamper_detected", scenario_ledger_tamper_detected),
    ("recovery_lease_exhaustion", scenario_recovery_lease_exhaustion),
    ("out_of_scope_side_effect", scenario_out_of_scope_side_effect),
    ("adapter_failure_across_environments", scenario_adapter_failure_across_environments),
    ("checkpoint_rollback_correctness", scenario_checkpoint_rollback_correctness),
    ("concurrent_recovery_safety", scenario_concurrent_recovery_safety),
    ("large_state_recovery_latency", scenario_large_state_recovery_latency),
    ("missing_dependency_graph_fallback", scenario_missing_dependency_graph_fallback),
    ("human_verdict_honored", scenario_human_verdict_honored),
    ("transient_network_failure_on_install", scenario_transient_network_failure_on_install),
]
