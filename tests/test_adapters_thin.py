"""Tests for the thin CrewAI / AutoGen / Pydantic AI adapters.

No framework SDK is required: each interception surface is exercised with
duck-typed stand-ins (a fake crewai.hooks registry, a fake AutoGen tool, a
fake pydantic-ai context), because what CONTINUUM depends on is the shape of
the seam, not the framework package.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from continuum.adapters.thin import (
    ContinuumToolGuard,
    autogen_available,
    crewai_available,
    install_crewai_hooks,
    pydantic_ai_available,
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


def test_args_dict_normalizes_every_framework_shape(db: str) -> None:
    """The derived key must be stable across framework argument shapes (#1925)."""
    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1")

        assert guard._args_dict({"customer": "c1"}) == {"customer": "c1"}

        class Model:
            """Duck-typed pydantic model: the seam needs only ``model_dump``."""

            def model_dump(self) -> dict[str, Any]:
                return {"customer": "c1"}

        assert guard._args_dict(Model()) == {"customer": "c1"}
        assert guard._args_dict("a bare string") == {"input": "a bare string"}
        assert guard._args_dict(42) == {"value": 42}


def test_settling_twice_or_with_an_unknown_token_is_a_no_op(db: str) -> None:
    """A framework that fires its after-hook twice cannot double-settle (#1925).

    Complete/fail on an already-settled or never-claimed token must be silent:
    the ledger keeps the first settlement and raises nothing, which is what
    keeps duplicate after-hooks exactly-once instead of exactly-erroring.
    """
    with SQLiteStorage(db) as store:
        guard = ContinuumToolGuard(store, "run_1")
        token = guard.claim("send_invoice", {"id": "I-7"})
        guard.complete(token, result="sent")
        assert last_action(db).status is ActionStatus.COMPLETED

        guard.complete(token, result="sent again")  # already settled
        guard.fail(token, "late failure")  # and not failed by the second hook
        guard.complete(999, result="never claimed")  # unknown token
        guard.fail(999, "never claimed")

    assert len(statuses(db)) == 1
    assert last_action(db).status is ActionStatus.COMPLETED


# --- availability probes -------------------------------------------------------- #


def test_probes_answer_a_bool_without_importing_the_framework() -> None:
    """Import probing, never importing: a bool either way, and never a raise."""
    assert isinstance(crewai_available(), bool)
    assert isinstance(pydantic_ai_available(), bool)


def test_probes_follow_find_spec_for_present_and_absent_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probes answer whatever ``find_spec`` says, for both answers."""

    def fake_find_spec(name: str, *rest: Any, **kwargs: Any) -> Any:
        return object() if name == "crewai" else None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    assert crewai_available() is True
    assert pydantic_ai_available() is False


def test_autogen_probe_raises_when_the_dotted_parent_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``find_spec`` on a dotted name imports the parent; an absent one raises.

    ``autogen_available`` documents ``ModuleNotFoundError`` on a machine without
    ``autogen_core``, so the probe is pinned to raise there rather than quietly
    answer False — a caller probing on a bare machine must be ready to catch it.
    """

    def fake_find_spec(name: str, *rest: Any, **kwargs: Any) -> Any:
        if "." in name:
            raise ModuleNotFoundError(f"No module named {name.rsplit('.', 1)[0]!r}")
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    with pytest.raises(ModuleNotFoundError, match="autogen_core"):
        autogen_available()


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
        self.error: str | None = None


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


def test_crewai_after_hook_settles_an_error_as_failed(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error attribute on the context fails the claim, and keeps the result."""
    fake = FakeCrewAIHooks()
    monkeypatch.setitem(sys.modules, "crewai", types.ModuleType("crewai"))
    monkeypatch.setitem(sys.modules, "crewai.hooks", fake)
    monkeypatch.setattr("continuum.adapters.thin.crewai_available", lambda: True)

    install_crewai_hooks(SQLiteStorage(db), "run_1")
    ctx = Context("send_invoice", {"id": "X"})
    fake.before[0](ctx)
    ctx.error = "smtp refused the connection"
    assert fake.after[0](ctx) is None  # the framework's own result stands
    assert last_action(db).status is ActionStatus.FAILED


class LegacyCrewAIHooks(types.ModuleType):
    """Stand-in for older crewai registries: clear-all, but no per-hook unregister.

    The uninstaller must degrade to ``clear_all_tool_call_hooks`` on these,
    otherwise it would leak every hook it registered (issue #1925).
    """

    def __init__(self) -> None:
        super().__init__("crewai.hooks")
        self.cleared = 0
        self.before: list[Any] = []
        self.after: list[Any] = []
        self.register_before_tool_call_hook = self.before.append
        self.register_after_tool_call_hook = self.after.append
        self.clear_all_tool_call_hooks = self._clear_all

    def _clear_all(self) -> None:
        self.cleared += 1


def test_crewai_uninstall_falls_back_to_clear_all_on_old_registries(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = LegacyCrewAIHooks()
    monkeypatch.setitem(sys.modules, "crewai", types.ModuleType("crewai"))
    monkeypatch.setitem(sys.modules, "crewai.hooks", fake)
    monkeypatch.setattr("continuum.adapters.thin.crewai_available", lambda: True)

    uninstall = install_crewai_hooks(SQLiteStorage(db), "run_1")
    assert len(fake.before) == 1 and len(fake.after) == 1

    uninstall()
    assert fake.cleared == 1, "old registries expose clear-all only"


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

    async def run_json(self, args: dict[str, object], cancellation_token: object = None) -> str:
        # AutoGen core declares ``async def run_json`` on both the Tool protocol
        # and BaseTool; a sync stand-in would let a sync wrapper pass CI while
        # never exercising the real coroutine surface (issue #1392).
        self.calls += 1
        if args.get("boom"):
            raise RuntimeError("upstream exploded")
        return f"ticket:{args.get('title')}"


@pytest.mark.asyncio
async def test_autogen_wrapper_claims_and_completes(db: str) -> None:
    tool = FakeAutoGenTool()
    wrapped = wrap_autogen_tool(
        tool, SQLiteStorage(db), "run_1", key_fn=lambda t, a: f"ticket:{a['title']}"
    )
    out = await wrapped.run_json({"title": "Fix login"}, cancellation_token=None)
    assert out == "ticket:Fix login"
    assert tool.calls == 1
    action = last_action(db)
    assert action.status is ActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_autogen_claim_stays_open_until_the_coroutine_is_awaited(db: str) -> None:
    """The ledger may not settle before the async tool body actually runs.

    ``run_json`` returns a coroutine; nothing executes until the framework
    awaits it, so nothing is settled at call time. A wrapper that settled at
    call time would record COMPLETED with an unrun coroutine, so a crash before
    the framework awaited it would leave the ledger claiming an effect that
    never happened (issue #1392).
    """
    tool = FakeAutoGenTool()
    wrapped = wrap_autogen_tool(tool, SQLiteStorage(db), "run_1")
    pending = wrapped.run_json({"title": "Fix login"})
    assert inspect.iscoroutine(pending)
    assert tool.calls == 0, "the tool body must not run until the coroutine is awaited"
    assert statuses(db) == [], "no claim may settle before the coroutine runs"

    assert await pending == "ticket:Fix login"
    assert tool.calls == 1
    assert last_action(db).status is ActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_autogen_failure_is_recorded_then_reraised(db: str) -> None:
    tool = FakeAutoGenTool()
    wrapped = wrap_autogen_tool(tool, SQLiteStorage(db), "run_1")
    pending = wrapped.run_json({"boom": True})
    assert statuses(db) == []
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await pending
    assert last_action(db).status is ActionStatus.FAILED


@pytest.mark.asyncio
async def test_autogen_wrapper_normalizes_non_dict_args(db: str) -> None:
    """A tool invoked with a bare value still claims under a dict argument shape."""

    class StringTool:
        name = "create_ticket"

        async def run_json(self, args: Any, cancellation_token: Any = None) -> str:
            return f"ticket:{args}"

    wrapped = wrap_autogen_tool(StringTool(), SQLiteStorage(db), "run_1")
    assert await wrapped.run_json("Fix login") == "ticket:Fix login"
    assert last_action(db).arguments == {"value": "Fix login"}


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


@pytest.mark.asyncio
async def test_pydantic_after_hook_without_a_stashed_token_is_passthrough(
    db: str,
) -> None:
    """A context with no stashed token is ignored, the result returned untouched."""
    hooks = wrap_pydantic_ai_hooks(SQLiteStorage(db), "run_1")

    class Ctx:
        pass

    out = await hooks.after_tool_call(Ctx(), "charge_card", {}, result="unchanged")
    assert out == "unchanged"
    assert not statuses(db), "an unmatched after-hook must do no ledger work"


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
