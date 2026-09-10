"""Tests for the thin CrewAI / AutoGen / Pydantic AI adapters.

No framework SDK is required: each interception surface is exercised with
duck-typed stand-ins (a fake crewai.hooks registry, a fake AutoGen tool, a
fake pydantic-ai context), because what CONTINUUM depends on is the shape of
the seam, not the framework package.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from continuum.adapters.thin import (
    ContinuumToolGuard,
    install_crewai_hooks,
    wrap_autogen_tool,
    wrap_pydantic_ai_hooks,
)
from continuum.events import EventType
from continuum.models import ActionStatus, Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "thin.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


def last_action(db: str):
    with SQLiteStorage(db) as store:
        from continuum.actions.ledger import fold_action_events

        return list(fold_action_events(store.read_events("run_1")).values())[-1]


def statuses(db: str) -> list[str]:
    from continuum.actions.ledger import fold_action_events

    with SQLiteStorage(db) as store:
        folded = fold_action_events(store.read_events("run_1"))
    return [a.status.value for a in folded.values()]


# --- shared guard ------------------------------------------------------------- #


def test_guard_claims_and_completes_through_the_real_ledger(db: str) -> None:
    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1")
        token = guard.claim("send_invoice", {"id": "I-9"})
        assert statuses(db)[-1] == "started"
        guard.complete(token, result="sent")
    action = last_action(db)
    assert action.status is ActionStatus.COMPLETED


def test_key_fn_overrides_the_default_derivation(db: str) -> None:
    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1", key_fn=lambda t, a: f"inv:{a['id']}")
        token = guard.claim("send_invoice", {"id": "I-1"})
        guard.complete(token)
    action = last_action(db)
    assert action.action_type == "send_invoice"


def test_fail_marks_not_occurred_when_certain(db: str) -> None:
    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1")
        token = guard.claim("deploy", {"env": "prod"})
        guard.fail(token, "connection refused before request sent", certain=True)
    assert last_action(db).status is ActionStatus.FAILED


# --- CrewAI --------------------------------------------------------------------- #


class FakeCrewAIHooks(types.ModuleType):
    """A stand-in for crewai.hooks capturing registrations."""

    def __init__(self) -> None:
        super().__init__("crewai.hooks")
        self.before: list[Any] = []
        self.after: list[Any] = []
        self.register_before_tool_call_hook = self.before.append
        self.register_after_tool_call_hook = self.after.append
        self.unregister_before_tool_call_hook = self.before.remove
        self.unregister_after_tool_call_hook = self.after.remove


class Context:
    def __init__(self, tool_name: str, tool_input: dict[str, object], result: object = None):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.result = result
        self.error = None


def test_crewai_hooks_route_claims_and_completions(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCrewAIHooks()
    crewai_pkg = types.ModuleType("crewai")
    monkeypatch.setitem(sys.modules, "crewai", crewai_pkg)
    monkeypatch.setitem(sys.modules, "crewai.hooks", fake)
    monkeypatch.setattr("continuum.adapters.thin.crewai_available", lambda: True)

    uninstall = install_crewai_hooks(SQLiteStorage(db), "run_1")
    assert len(fake.before) == 1 and len(fake.after) == 1

    ctx = Context("send_invoice", {"id": "X"})
    assert fake.before[0](ctx) is None  # allowed
    ctx.result = "queued"
    assert fake.after[0](ctx) is None
    assert last_action(db).status is ActionStatus.COMPLETED

    uninstall()
    # After uninstall the registry is empty: the framework would call nothing.
    assert fake.before == [] and fake.after == []
    assert len(statuses(db)) == 1


def test_crewai_action_type_filter_passes_untracked_tools(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCrewAIHooks()
    monkeypatch.setitem(sys.modules, "crewai", types.ModuleType("crewai"))
    monkeypatch.setitem(sys.modules, "crewai.hooks", fake)
    monkeypatch.setattr("continuum.adapters.thin.crewai_available", lambda: True)

    install_crewai_hooks(SQLiteStorage(db), "run_1", action_types={"send_email"})
    fake.before[0](Context("read_file", {}))
    fake.after[0](Context("read_file", {}, result="ok"))
    assert not statuses(db)


def test_crewai_install_requires_the_package() -> None:
    from continuum.adapters import thin

    saved = thin.crewai_available
    try:
        thin.crewai_available = lambda: False
        with pytest.raises(ImportError, match="crewai"):
            install_crewai_hooks(None, "run_1")
    finally:
        thin.crewai_available = saved


# --- AutoGen ---------------------------------------------------------------------- #


class FakeAutoGenTool:
    name = "create_ticket"

    def __init__(self) -> None:
        self.calls = 0

    def run_json(self, args: dict[str, object], cancellation_token: object = None) -> str:
        self.calls += 1
        if args.get("boom"):
            raise RuntimeError("upstream exploded")
        return f"ticket:{args.get('title')}"


def test_autogen_wrapper_claims_and_completes(db: str) -> None:
    tool = FakeAutoGenTool()
    wrapped = wrap_autogen_tool(
        tool, SQLiteStorage(db), "run_1", key_fn=lambda t, a: f"ticket:{a['title']}"
    )
    out = wrapped.run_json({"title": "Fix login"}, cancellation_token=None)
    assert out == "ticket:Fix login"
    assert tool.calls == 1
    action = last_action(db)
    assert action.status is ActionStatus.COMPLETED


def test_autogen_failure_is_recorded_then_reraised(db: str) -> None:
    tool = FakeAutoGenTool()
    wrapped = wrap_autogen_tool(tool, SQLiteStorage(db), "run_1")
    with pytest.raises(RuntimeError):
        wrapped.run_json({"boom": True})
    assert last_action(db).status is ActionStatus.FAILED


# --- Pydantic AI ------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pydantic_capability_routes_async_hooks(db: str) -> None:
    hooks = wrap_pydantic_ai_hooks(SQLiteStorage(db), "run_1")

    class Ctx:
        pass

    ctx = Ctx()
    await hooks.before_tool_call(ctx, "charge_card", {"customer": "c1"})
    await hooks.after_tool_call(ctx, "charge_card", {"customer": "c1"}, result="charged")
    assert last_action(db).status is ActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_pydantic_error_path_fails_the_action(db: str) -> None:
    hooks = wrap_pydantic_ai_hooks(SQLiteStorage(db), "run_1")

    class Ctx:
        pass

    ctx = Ctx()
    await hooks.before_tool_call(ctx, "charge_card", {"customer": "c2"})
    await hooks.after_tool_call(
        ctx, "charge_card", {"customer": "c2"}, result=None, error="card declined"
    )
    assert last_action(db).status is ActionStatus.FAILED


# --- provenance -------------------------------------------------------------------- #


def test_guard_ledger_writes_carry_external_agent(db: str) -> None:
    """Framework-asserted facts must not self-certify as deterministic (#612)."""
    from continuum.models import Origin
    from continuum.provenance_map import derived_provenance_for_events

    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1")
        guard.complete(guard.claim("notify.customer", {"order_id": "O-9"}))
        action_events = [
            e for e in store.read_events("run_1") if e.type is EventType.ACTION_RECORDED
        ]
    assert action_events, "guard must record action events"
    assert {e.source for e in action_events} == {Origin.EXTERNAL_AGENT}
    with SQLiteStorage(db) as store:
        derived = derived_provenance_for_events(store.read_events("run_1"))
    assert derived is Origin.EXTERNAL_AGENT


def test_default_ledger_stays_deterministic(db: str) -> None:
    """The new source parameter must not change existing writers (#612)."""
    from continuum.actions import ActionLedger
    from continuum.models import Origin

    with SQLiteStorage(db) as store:
        ActionLedger(store, "run_1").claim("x.do", {}, key="plain")
        action_events = [
            e for e in store.read_events("run_1") if e.type is EventType.ACTION_RECORDED
        ]
    assert action_events
    assert {e.source for e in action_events} == {Origin.DETERMINISTIC}


def test_guard_source_override_is_respected(db: str) -> None:
    """Callers that own the facts can keep the deterministic stamp (#612)."""
    from continuum.models import Origin

    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1", source=Origin.DETERMINISTIC)
        guard.complete(guard.claim("notify.customer", {"order_id": "O-9"}))
        action_events = [
            e for e in store.read_events("run_1") if e.type is EventType.ACTION_RECORDED
        ]
    assert action_events
    assert {e.source for e in action_events} == {Origin.DETERMINISTIC}
