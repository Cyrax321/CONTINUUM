import time
from pathlib import Path

from continuum.adapters.generic import GenericAgentAdapter
from continuum.events import EventType
from continuum.models import Run
from continuum.state.semantic import project
from continuum.storage import SQLiteStorage


def _seed_run(run_id: str = "run_1", goal: str = "g", total: int = 5) -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=run_id, goal=goal))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": goal, "total": total})
    return storage


def test_adapter_auto_derives_progress_from_file(tmp_path: Path) -> None:
    file = tmp_path / "guide.md"
    file.write_text("## A\n\n## B\n\n", encoding="utf-8")
    storage = _seed_run()
    adapter = GenericAgentAdapter(storage, auto_file=str(file), auto_total=5)
    state = project("run_1", storage.read_events("run_1"))
    adapter.capture_state("run_1", state)
    restored = project("run_1", storage.read_events("run_1"))
    assert restored.progress.completed == 2


def test_adapter_path_records_tail_evidence(tmp_path: Path) -> None:
    file = tmp_path / "guide.md"
    file.write_text("## A\ncontent a\n\n## B\ncontent b\n", encoding="utf-8")
    storage = _seed_run()
    adapter = GenericAgentAdapter(storage, auto_file=str(file), auto_total=5)
    state = project("run_1", storage.read_events("run_1"))
    adapter.capture_state("run_1", state)
    from continuum.recovery import RecoveryEngine

    decision = RecoveryEngine(storage).assess("run_1")
    assert decision.tail_evidence is not None
    assert "content b" in decision.tail_evidence


def test_repeated_capture_over_unchanged_file_does_not_bloat_log(
    tmp_path: Path,
) -> None:
    file = tmp_path / "guide.md"
    file.write_text("## A\n\n## B\n\n", encoding="utf-8")
    storage = _seed_run()
    adapter = GenericAgentAdapter(storage, auto_file=str(file), auto_total=5)
    state = project("run_1", storage.read_events("run_1"))
    adapter.capture_state("run_1", state)
    updates_after_first = sum(
        1 for e in storage.read_events("run_1") if e.type == EventType.TASK_UPDATED
    )
    assert updates_after_first == 1
    adapter.capture_state("run_1", state)
    updates_after_second = sum(
        1 for e in storage.read_events("run_1") if e.type == EventType.TASK_UPDATED
    )
    assert updates_after_second == 1


def test_intercept_action_fires_auto_progress_without_blocking(
    tmp_path: Path,
) -> None:
    file = tmp_path / "guide.md"
    file.write_text("## A\nfirst\n", encoding="utf-8")
    storage = _seed_run(total=5)
    adapter = GenericAgentAdapter(storage, auto_file=str(file), auto_total=5)

    def write_section() -> str:
        with file.open("a", encoding="utf-8") as fh:
            fh.write("\n## B\nsecond\n")
        return "done"

    result = adapter.intercept_action(
        "run_1", "test.append_section", write_section, key="section:B"
    )
    assert result == "done"
    # The derived TASK_UPDATED is appended synchronously before the call
    # returns, so the log already reflects the new section.
    updates = [e.payload for e in storage.read_events("run_1") if e.type == EventType.TASK_UPDATED]
    assert updates and updates[-1]["completed"] == 2
    # The checkpoint write is policy-gated and background; give the shared
    # executor a moment and require it to land without blocking the turn.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and storage.latest_checkpoint("run_1") is None:
        time.sleep(0.01)
    assert storage.latest_checkpoint("run_1") is not None


def test_subclass_adapters_forward_auto_options(tmp_path: Path) -> None:
    file = tmp_path / "guide.md"
    file.write_text("## A\n\n", encoding="utf-8")
    storage = _seed_run()

    from continuum.adapters.langchain import LangChainAgentAdapter
    from continuum.adapters.langgraph import LangGraphAgentAdapter
    from continuum.adapters.openai import OpenAIAgentAdapter

    for cls in (LangChainAgentAdapter, LangGraphAgentAdapter, OpenAIAgentAdapter):
        adapter = cls(storage, auto_file=str(file), auto_total=5)
        assert adapter.auto_file == str(file)
        assert adapter.auto_total == 5
