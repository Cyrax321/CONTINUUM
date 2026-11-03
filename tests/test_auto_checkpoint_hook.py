from pathlib import Path

from continuum.checkpoint import CheckpointManager
from continuum.events import EventType
from continuum.hooks import (
    count_sections,
    get_tail_section,
    make_async_auto_checkpoint_hook,
    make_async_file_derived_progress_hook,
    make_auto_checkpoint_hook,
    make_file_derived_progress_hook,
    record_file_progress,
)
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_auto_checkpoint_hook_respects_policy() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    manager = CheckpointManager(storage)
    hook = make_auto_checkpoint_hook(manager, "run_1")

    first = hook()
    assert first is True
    second = hook()
    assert second is False


def test_async_hook_does_not_block_and_still_checkpoints() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    manager = CheckpointManager(storage)
    hook = make_async_auto_checkpoint_hook(manager, "run_1")

    import time

    start = time.perf_counter()
    result = hook()
    elapsed = time.perf_counter() - start

    assert result is True
    assert elapsed < 0.1

    time.sleep(0.2)
    assert storage.latest_checkpoint("run_1") is not None


def test_count_sections_counts_headings(tmp_path: Path) -> None:
    file = tmp_path / "guide.md"
    file.write_text("# Title\n\n## A\n\n## B\n\nText\n", encoding="utf-8")
    assert count_sections(file) == 2


def test_file_derived_hook_records_from_file(tmp_path: Path) -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 8})
    manager = CheckpointManager(storage)
    file = tmp_path / "guide.md"
    file.write_text("## 1\n## 2\n## 3\n", encoding="utf-8")
    hook = make_file_derived_progress_hook(manager, "run_1", file, total=8)
    hook()
    from continuum.state.semantic import project

    state = project("run_1", storage.read_events("run_1"))
    assert state.progress.completed == 3


def test_get_tail_section_returns_last_section(tmp_path: Path) -> None:
    file = tmp_path / "guide.md"
    file.write_text("# Title\n\n## A\ncontent a\n\n## B\ncontent b\n", encoding="utf-8")
    tail = get_tail_section(file)
    assert tail.startswith("## B")
    assert "content b" in tail


def test_file_derived_hook_captures_tail_evidence(tmp_path: Path) -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g", "total": 8})
    manager = CheckpointManager(storage)
    file = tmp_path / "guide.md"
    file.write_text("## 1\ncontent 1\n\n## 2\ncontent 2\n", encoding="utf-8")
    hook = make_file_derived_progress_hook(manager, "run_1", file, total=8)
    hook()
    events = storage.read_events("run_1")
    evidence_events = [e for e in events if e.type == EventType.EVIDENCE_ADDED]
    assert any("content 2" in str(e.payload.get("summary", "")) for e in evidence_events)


def _drain_background_writes() -> None:
    """Block until the shared background executor has nothing left to run.

    The executor is single-worker and FIFO, so a sentinel submitted after a
    checkpoint write returns only once that write has finished: no fixed sleep
    to tune, and the pool stays open for the rest of the suite.
    """
    from continuum.hooks import _EXECUTOR

    _EXECUTOR.submit(lambda: None).result(timeout=10)


def test_count_sections_treats_a_missing_file_as_zero(tmp_path: Path) -> None:
    """A harness may point the hooks at a file it has not written yet (#1926)."""
    assert count_sections(tmp_path / "never_created.md") == 0


def test_get_tail_section_treats_a_missing_file_as_empty(tmp_path: Path) -> None:
    """A missing file degrades the same way a section-less one does (#1926)."""
    assert get_tail_section(tmp_path / "never_created.md") == ""


def test_get_tail_section_of_a_file_without_sections_is_empty(tmp_path: Path) -> None:
    """A file with no ## headings has no tail section to capture (#1926)."""
    file = tmp_path / "notes.md"
    file.write_text("# Title\n\nplain paragraphs only\n", encoding="utf-8")
    assert get_tail_section(file) == ""


def test_record_file_progress_recovers_from_an_unprojectable_run(tmp_path: Path) -> None:
    """A run that has no RUN_STARTED yet is mirrored, not raised over (#1926).

    The projection of the merged history raises, so the hook must fall through
    to "changed" and append the file-derived count rather than propagate the
    error to the agent turn.
    """
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_nogoal", goal="g"))
    manager = CheckpointManager(storage)
    file = tmp_path / "guide.md"
    file.write_text("## 1\n## 2\n", encoding="utf-8")

    completed = record_file_progress(manager, "run_nogoal", file, total=4)

    assert completed == 2
    events = storage.read_events("run_nogoal")
    assert any(e.type == EventType.TASK_UPDATED and e.payload.get("completed") == 2 for e in events)


def test_record_file_progress_appends_nothing_over_an_unchanged_file(tmp_path: Path) -> None:
    """The second call over an unchanged file returns -1 and writes nothing."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_unchanged", goal="g"))
    storage.append_event("run_unchanged", EventType.RUN_STARTED, {"goal": "g", "total": 4})
    manager = CheckpointManager(storage)
    file = tmp_path / "guide.md"
    file.write_text("## 1\n## 2\n", encoding="utf-8")

    assert record_file_progress(manager, "run_unchanged", file, total=4) == 2
    after_first = len(storage.read_events("run_unchanged"))

    assert record_file_progress(manager, "run_unchanged", file, total=4) == -1
    assert len(storage.read_events("run_unchanged")) == after_first


def test_async_auto_checkpoint_hook_declines_and_queues_nothing() -> None:
    """A policy no returns False and submits no second write (#1926).

    The async hook is called exactly once in the rest of the suite, so its
    decline path is never exercised: after the run is checkpointed the policy
    should say no on the caller's thread, with nothing landing on the
    background executor.
    """
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_async_dec", goal="g"))
    storage.append_event("run_async_dec", EventType.RUN_STARTED, {"goal": "g", "total": 1})
    manager = CheckpointManager(storage)
    hook = make_async_auto_checkpoint_hook(manager, "run_async_dec")

    assert hook() is True
    _drain_background_writes()
    marked = storage.latest_checkpoint("run_async_dec")
    assert marked is not None

    assert hook() is False
    _drain_background_writes()
    assert storage.latest_checkpoint("run_async_dec").checkpoint_id == marked.checkpoint_id


def test_async_file_derived_hook_declines_over_an_unchanged_file(tmp_path: Path) -> None:
    """An unchanged file makes the async hook return False before submitting (#1926)."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_fd_dec", goal="g"))
    storage.append_event("run_fd_dec", EventType.RUN_STARTED, {"goal": "g", "total": 4})
    manager = CheckpointManager(storage)
    file = tmp_path / "guide.md"
    file.write_text("## 1\n## 2\n", encoding="utf-8")
    hook = make_async_file_derived_progress_hook(manager, "run_fd_dec", file, total=4)

    assert hook() is True
    _drain_background_writes()
    marked = storage.latest_checkpoint("run_fd_dec")
    assert marked is not None

    assert hook() is False
    _drain_background_writes()
    assert storage.latest_checkpoint("run_fd_dec").checkpoint_id == marked.checkpoint_id


def test_record_file_progress_skips_evidence_when_the_file_has_no_tail(tmp_path: Path) -> None:
    """A section-less file records the count but appends no empty tail evidence."""
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_notail", goal="g"))
    manager = CheckpointManager(storage)
    file = tmp_path / "guide.md"
    file.write_text("plain prose, no headings yet\n", encoding="utf-8")

    assert record_file_progress(manager, "run_notail", file, total=4) == 0
    events = storage.read_events("run_notail")
    assert any(e.type == EventType.TASK_UPDATED and e.payload.get("completed") == 0 for e in events)
    assert not any(e.type == EventType.EVIDENCE_ADDED for e in events)
