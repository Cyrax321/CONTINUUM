from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import SpanProcessor

from continuum.events import EventType
from continuum.models import Run
from continuum.otel import make_span_processor
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "otel-span-processor.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="trace tool calls"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "trace tool calls"})
    return path


def _span(*, tool: str = "write_file", ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        name="execute_tool",
        attributes={"tool.name": tool, "file_path": "/tmp/output.txt"},
        status=SimpleNamespace(is_ok=ok),
        context=SimpleNamespace(span_id=11, trace_id=22),
    )


def test_factory_returns_a_span_processor_and_lifecycle_hooks_are_noops(db: str) -> None:
    processor = make_span_processor(SQLiteStorage(db), run_id="run_1")

    assert isinstance(processor, SpanProcessor)
    assert processor.on_start(None, None) is None
    assert processor.shutdown() is True
    assert processor.force_flush() is True


def test_on_end_records_a_successful_tool_span(db: str) -> None:
    processor = make_span_processor(SQLiteStorage(db), run_id="run_1")

    processor.on_end(_span())

    with SQLiteStorage(db) as store:
        event = store.read_events("run_1")[-1]
    assert event.type is EventType.TOOL_COMPLETED
    assert event.payload["tool"] == "write_file"
    assert event.payload["span_id"] == 11
    assert event.payload["trace_id"] == 22


def test_on_end_records_a_failed_tool_span(db: str) -> None:
    processor = make_span_processor(SQLiteStorage(db), run_id="run_1")

    processor.on_end(_span(tool="send_invoice", ok=False))

    with SQLiteStorage(db) as store:
        event = store.read_events("run_1")[-1]
    assert event.type is EventType.TOOL_FAILED
    assert event.payload["tool"] == "send_invoice"
