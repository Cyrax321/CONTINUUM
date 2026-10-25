import json

from continuum.adapters import AdapterAction, AdapterResult, GenericAgentAdapter, run_action
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _adapter() -> GenericAgentAdapter:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="adapter_action"))
    return GenericAgentAdapter(storage)


def test_adapter_action_serializes() -> None:
    action = AdapterAction(name="pkg.install", params={"ver": 1}, dep_scope="numpy")
    payload = json.loads(json.dumps(action.to_json()))
    assert payload == {
        "name": "pkg.install",
        "params": {"ver": 1},
        "dep_scope": "numpy",
        "action_id": None,
    }
    restored = AdapterAction(**payload)
    assert restored == action


def test_adapter_emits_action() -> None:
    adapter = _adapter()
    result = run_action(
        adapter,
        "run_1",
        AdapterAction(name="pkg.install", params={"x": 1}, dep_scope="numpy"),
        lambda: 7,
    )
    assert isinstance(result, AdapterResult)
    assert result.status == "completed"
    assert result.output == 7


def test_adapter_action_failure_is_captured() -> None:
    adapter = _adapter()
    result = run_action(
        adapter,
        "run_1",
        AdapterAction(name="boom", params={}),
        lambda: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    assert result.status == "failed"
    assert "RuntimeError" in result.error


# --- telemetry hook (issue #162) ---------------------------------------------


def test_telemetry_disabled_by_default() -> None:
    adapter = _adapter()
    result = run_action(adapter, "run_1", AdapterAction(name="t"), lambda: 1)
    assert result.status == "completed"


def test_telemetry_receives_action_and_result() -> None:
    adapter = _adapter()
    seen: list[tuple[AdapterAction, AdapterResult]] = []
    action = AdapterAction(name="pkg.install", params={"x": 1}, dep_scope="numpy")
    result = run_action(
        adapter, "run_1", action, lambda: 7, on_event=lambda a, r: seen.append((a, r))
    )
    assert len(seen) == 1
    observed_action, observed_result = seen[0]
    assert observed_action == action
    assert observed_result is result
    assert observed_result.status == "completed"
    assert observed_result.output == 7


def test_telemetry_receives_failures() -> None:
    adapter = _adapter()
    seen: list[AdapterResult] = []

    def boom() -> None:
        raise RuntimeError("nope")

    def collect(a: AdapterAction, r: AdapterResult) -> None:
        seen.append(r)

    result = run_action(adapter, "run_1", AdapterAction(name="boom"), boom, on_event=collect)
    assert result.status == "failed"
    assert len(seen) == 1 and seen[0].status == "failed"


def test_raising_observer_cannot_break_the_action() -> None:
    adapter = _adapter()

    def bad_observer(a: AdapterAction, r: AdapterResult) -> None:
        raise RuntimeError("observer bug")

    result = run_action(
        adapter, "run_1", AdapterAction(name="t"), lambda: 42, on_event=bad_observer
    )
    assert result.status == "completed"
    assert result.output == 42


# --- failure messages carry the captured stderr (issue #1760) ---------------- #


def test_a_subprocess_failure_carries_its_stderr_tail() -> None:
    """A CalledProcessError's string form never includes stderr; the formatter does.

    Without this the half of the captured output that says how to fix the
    failure (which import failed, which file was missing, what the compiler
    said) is captured by the transport and then discarded.
    """
    import subprocess

    from continuum.adapters.actions import format_action_error

    exc = subprocess.CalledProcessError(
        42, ["python", "-m", "compile"], stderr="NameError: name 'x' is not defined\n"
    )

    message = format_action_error(exc)

    assert "CalledProcessError" in message
    assert "NameError: name 'x' is not defined" in message


def test_a_chatty_stderr_is_bounded_so_it_cannot_flood_the_ledger() -> None:
    """The ledger is durable and the result is rendered, so the tail is bounded."""
    import subprocess

    from continuum.adapters.actions import _STDERR_TAIL, format_action_error

    exc = subprocess.CalledProcessError(1, ["python", "-c", "noisy"], stderr="line\n" * 5000)

    message = format_action_error(exc)

    assert len(message) < 4 * _STDERR_TAIL
    assert message.endswith("line")
