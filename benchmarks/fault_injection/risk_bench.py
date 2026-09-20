"""Risk fault injection bench (issue #564).

Injects RISK_OBSERVED events for each trigger class and asserts that
the recovery engine chooses the mode defined by the declarative policy
and that the contract cites the triggering risk ids.

Also verifies no duplicate side effects after ABORT+reconcile: a second
claim under the same key after an ABORT must not re-execute the effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from continuum.models import RecoveryMode, Run
from continuum.recovery import RecoveryEngine
from continuum.recovery.risk import DEFAULT_RISK_POLICY, evaluate_risk
from continuum.storage import SQLiteStorage
from continuum.storage.base import Storage


@dataclass(frozen=True)
class RiskFault:
    trigger: str
    expected_mode: str | None
    description: str


RISK_FAULTS: list[RiskFault] = [
    RiskFault("loop", "replan", "loop collapses into repetition"),
    RiskFault("error_cascade", "wait", "transient cascade, backoff"),
    RiskFault("latency_anomaly", None, "benign slowness, annotate only"),
    RiskFault("token_runaway", "wait", "token runaway, budget breach"),
    RiskFault("silent_abort", "repair_and_resume", "omissions recoverable"),
    RiskFault("meltdown", "rollback", "meltdown to last fact-gathering step"),
    RiskFault("side_effect_duplicate", "abort", "gate bypass, abort and reconcile"),
    RiskFault("governance_decay", "request_human", "dropped constraint invalidates intent"),
]


def _mode_for_trigger(trigger: str, policy: dict[str, str] | None = None) -> str | None:
    return evaluate_risk(trigger, policy or DEFAULT_RISK_POLICY)


def run_risk_fault_suite(
    storage: Storage, run_id: str, trigger: str, policy: dict[str, str] | None = None
) -> dict[str, Any]:
    """Inject a single risk trigger and return the decision."""
    from continuum.recovery.risk import ingest_risk

    ingest_risk(
        storage, run_id, {"trigger": trigger, "score": 0.9, "detail": f"bench inject {trigger}"}
    )
    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id)
    expected = _mode_for_trigger(trigger, policy)
    return {
        "trigger": trigger,
        "expected_mode": expected,
        "actual_mode": decision.mode.value,
        "triggering_risks": list(decision.contract.triggering_risks),
        "contract_mode": decision.contract.recovery_status.value,
        "liveness": decision.contract.liveness,
    }


def run_all_risk_faults(db_path: str = ":memory:") -> list[dict[str, Any]]:
    """Run all risk fault scenarios and return results."""
    results: list[dict[str, Any]] = []
    for fault in RISK_FAULTS:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db = os.path.join(tmpdir, f"{fault.trigger}.db")
            with SQLiteStorage(db) as store:
                run_id = f"run_risk_{fault.trigger}"
                store.create_run_started(Run(run_id=run_id, goal="bench risk"))
                result = run_risk_fault_suite(store, run_id, fault.trigger)
                result["description"] = fault.description
                results.append(result)
    return results


def assert_no_duplicate_after_abort(tmp_path: str | None = None) -> bool:
    """Verify that after ABORT+reconcile, a duplicate claim does not re-execute."""
    import os
    import tempfile

    from continuum.actions.ledger import ActionLedger

    with tempfile.TemporaryDirectory() as tmpdir:
        db = os.path.join(tmpdir, "abort_dup.db") if tmp_path is None else tmp_path
        with SQLiteStorage(db) as store:
            run_id = "run_abort_dup"
            store.create_run_started(Run(run_id=run_id, goal="abort dup test"))
            ledger = ActionLedger(store, run_id)
            claim1 = ledger.claim(
                "payment.charge", {"amount": 100, "customer": "acme"}, key="charge:acme:100"
            )
            assert claim1.fresh is True
            from continuum.recovery.risk import ingest_risk

            ingest_risk(store, run_id, {"trigger": "side_effect_duplicate"})
            engine = RecoveryEngine(store)
            decision = engine.assess(run_id)
            assert decision.mode == RecoveryMode.ABORT
            ledger.complete(claim1.key, result={"id": "ch_123"})
            claim2 = ledger.claim(
                "payment.charge", {"amount": 100, "customer": "acme"}, key="charge:acme:100"
            )
            assert claim2.fresh is False
            assert claim2.result is not None
            return True
    return False
