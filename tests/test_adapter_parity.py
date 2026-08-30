"""Adapter coverage parity: crash-and-resume verification for LangGraph and OpenAI (issue #285)."""

from __future__ import annotations

import pytest

from continuum.adapters import GenericAgentAdapter
from continuum.environment import StaticProvider, capture
from continuum.models import Goal, RecoveryMode, SemanticState
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> SQLiteStorage:
    return SQLiteStorage(":memory:")


class TestGenericCrashResumeParity:
    def test_generic_crash_mid_action_blocks_resume_and_dedupes_on_retry(
        self, store: SQLiteStorage
    ) -> None:
        from continuum.events import EventType

        adapter = GenericAgentAdapter(store)
        # Generic start_run does not backfill RUN_STARTED, so ensure it exists
        from continuum.models import Run

        try:
            store.get_run("generic_crash_1")
        except Exception:
            store.create_run(Run(run_id="generic_crash_1", goal="generic crash"))
            store.append_event("generic_crash_1", EventType.RUN_STARTED, {"goal": "generic crash"})
        state = SemanticState(run_id="generic_crash_1", goal=Goal(description="generic crash"))
        env0 = capture("generic_crash_1", StaticProvider(gateway="v1"))
        adapter.capture_state("generic_crash_1", state, environment=env0)

        def flaky_charge() -> dict[str, str]:
            raise TimeoutError("gateway timeout")

        with pytest.raises(TimeoutError):
            adapter.intercept_action(
                "generic_crash_1", "stripe.charge", flaky_charge, arguments={"amount": 500}
            )

        from continuum.actions import ActionLedger
        from continuum.models import ActionStatus

        ledger = ActionLedger(store, "generic_crash_1")
        pending = ledger.pending()
        assert len(pending) == 1
        assert pending[0].status is ActionStatus.UNKNOWN
        assert pending[0].side_effect_uncertain

        env = capture("generic_crash_1", StaticProvider(gateway="v1"))
        decision = adapter.resume("generic_crash_1", current_environment=env)
        assert decision.mode is not RecoveryMode.RESUME
        assert not decision.safe
        assert decision.uncertain_actions

        calls = 0

        def charge2() -> dict[str, bool]:
            nonlocal calls
            calls += 1
            return {"ok": True}

        from continuum.models import UnknownSideEffect

        with pytest.raises(UnknownSideEffect):
            adapter.intercept_action(
                "generic_crash_1", "stripe.charge", charge2, arguments={"amount": 500}
            )
        assert calls == 0

        ledger.reconcile(
            pending[0].action_id, occurred=True, external_id="tx_1", result={"ok": True}
        )
        assert len(ledger.pending()) == 0

        calls = 0
        result = adapter.intercept_action(
            "generic_crash_1", "stripe.charge", charge2, arguments={"amount": 500}
        )
        assert result == {"ok": True}
        assert calls == 0


class TestLangGraphCrashResumeParity:
    def test_langgraph_crash_mid_action_blocks_resume_and_dedupes(
        self, store: SQLiteStorage
    ) -> None:
        pytest.importorskip("langgraph")
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)
        adapter.start_run(goal="langgraph crash", run_id="lg_crash_1")

        state = SemanticState(run_id="lg_crash_1", goal=Goal(description="langgraph crash"))
        env = capture("lg_crash_1", StaticProvider(gateway="v1"))
        adapter.capture_state("lg_crash_1", state, environment=env)

        @adapter.wrap_tool("payment.charge")
        def charge(continuum_run_id: str = "") -> dict[str, str]:
            raise TimeoutError("gateway timeout")

        with pytest.raises(TimeoutError):
            charge(continuum_run_id="lg_crash_1")

        from continuum.actions import ActionLedger
        from continuum.models import ActionStatus

        ledger = ActionLedger(store, "lg_crash_1")
        pending = ledger.pending()
        assert len(pending) == 1
        assert pending[0].status is ActionStatus.UNKNOWN

        decision = adapter.assess_graph_recovery("lg_crash_1", current_environment=env)
        assert decision.mode is not RecoveryMode.RESUME
        assert not decision.safe
        assert decision.uncertain_actions

        ledger.reconcile(
            pending[0].action_id, occurred=True, external_id="tx_1", result={"ok": True}
        )
        assert len(ledger.pending()) == 0

        result = adapter.intercept_action(
            "lg_crash_1", "payment.charge", lambda: {"ok": True}, arguments={}
        )
        assert result == {"ok": True}

    def test_langgraph_os_exit_style_crash_blocks_resume(self, store: SQLiteStorage) -> None:
        pytest.importorskip("langgraph")
        from continuum.actions import ActionLedger
        from continuum.adapters.langgraph import LangGraphAgentAdapter

        adapter = LangGraphAgentAdapter(store)
        adapter.start_run(goal="lg os_exit", run_id="lg_exit_1")
        ledger = ActionLedger(store, "lg_exit_1")
        outcome = ledger.claim("external.api", {"endpoint": "/data"}, key="k1")
        assert ledger.pending()

        env = capture("lg_exit_1", StaticProvider(gateway="v1"))
        state = SemanticState(run_id="lg_exit_1", goal=Goal(description="lg os_exit"))
        adapter.capture_state("lg_exit_1", state, environment=env)

        decision = adapter.assess_graph_recovery("lg_exit_1", current_environment=env)
        assert not decision.safe
        assert decision.uncertain_actions

        ledger.reconcile(outcome.action.action_id, occurred=False, note="checked, did not happen")
        assert not ledger.pending()
        decision2 = adapter.assess_graph_recovery("lg_exit_1", current_environment=env)
        assert decision2.safe or not decision2.uncertain_actions


class TestOpenAICrashResumeParity:
    def test_openai_crash_mid_action_blocks_resume_and_dedupes(self, store: SQLiteStorage) -> None:
        try:
            import agents  # noqa: F401
        except ImportError:
            pytest.skip("openai-agents not installed")

        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)
        adapter.start_run(goal="openai crash", run_id="oa_crash_1")
        state = SemanticState(run_id="oa_crash_1", goal=Goal(description="openai crash"))
        env = capture("oa_crash_1", StaticProvider(gateway="v1"))
        adapter.capture_state("oa_crash_1", state, environment=env)

        def flaky() -> dict[str, str]:
            raise TimeoutError("gateway timeout")

        with pytest.raises(TimeoutError):
            adapter.intercept_action(
                "oa_crash_1", "payment.charge", flaky, arguments={"amount": 500}
            )

        from continuum.actions import ActionLedger
        from continuum.models import ActionStatus

        ledger = ActionLedger(store, "oa_crash_1")
        pending = ledger.pending()
        assert len(pending) == 1
        assert pending[0].status is ActionStatus.UNKNOWN

        decision = adapter.assess_agent_recovery("oa_crash_1", current_environment=env)
        assert decision.mode is not RecoveryMode.RESUME
        assert not decision.safe

        ledger.reconcile(
            pending[0].action_id, occurred=True, external_id="tx_1", result={"ok": True}
        )
        assert not ledger.pending()
        result = adapter.intercept_action(
            "oa_crash_1", "payment.charge", lambda: {"ok": True}, arguments={"amount": 500}
        )
        assert result == {"ok": True}

    def test_openai_os_exit_style_crash_blocks_resume(self, store: SQLiteStorage) -> None:
        try:
            import agents  # noqa: F401
        except ImportError:
            pytest.skip("openai-agents not installed")

        from continuum.actions import ActionLedger
        from continuum.adapters.openai import OpenAIAgentAdapter

        adapter = OpenAIAgentAdapter(store)
        adapter.start_run(goal="oa os_exit", run_id="oa_exit_1")
        ledger = ActionLedger(store, "oa_exit_1")
        outcome = ledger.claim("external.api", {"endpoint": "/data"}, key="k1")
        assert ledger.pending()

        from continuum.environment import StaticProvider, capture
        from continuum.models import Goal, SemanticState

        env = capture("oa_exit_1", StaticProvider(gateway="v1"))
        state = SemanticState(run_id="oa_exit_1", goal=Goal(description="oa os_exit"))
        adapter.capture_state("oa_exit_1", state, environment=env)

        decision = adapter.assess_agent_recovery("oa_exit_1", current_environment=env)
        assert not decision.safe
        assert decision.uncertain_actions

        ledger.reconcile(outcome.action.action_id, occurred=False, note="checked")
        assert not ledger.pending()
        decision2 = adapter.assess_agent_recovery("oa_exit_1", current_environment=env)
        assert decision2.safe or not decision2.uncertain_actions
