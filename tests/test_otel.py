"""OpenTelemetry bridge (seam 5).

Tool-call spans arriving through standard OTel pipelines become ordinary
CONTINUUM evidence. The pure core is tested without the OTel SDK installed;
spans are duck-typed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Origin, Run
from continuum.otel import _attr, observation_from_span, record_span
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "otel.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


class FakeSpan:
    """Duck-typed stand-in for a ReadableSpan."""

    def __init__(self, name: str, attributes: dict[str, object], ok: bool = True) -> None:
        self.name = name
        self.attributes = attributes

        class _Status:
            def __init__(self, ok: bool) -> None:
                self.is_ok = ok

        self.status = _Status(ok)


def test_gen_ai_tool_spans_are_recognised() -> None:
    payload = observation_from_span(
        "execute_tool",
        {"gen_ai.tool.name": "write_file", "file_path": "/tmp/a.txt"},
    )
    assert payload is not None
    assert payload["tool"] == "write_file"
    assert payload["path"] == "/tmp/a.txt"
    assert payload["via"] == "otel"


def test_vendor_attribute_families_are_recognised() -> None:
    for key in ("tool.name", "openinference.tool.name", "mcp.tool.name", "function.name"):
        payload = observation_from_span("span", {key: "search_web"})
        assert payload is not None and payload["tool"] == "search_web", key


def test_non_tool_spans_are_ignored() -> None:
    assert observation_from_span("llm_call", {"gen_ai.request.model": "gpt"}) is None
    assert observation_from_span("http get", {"url": "https://x"}) is None


def test_failed_tool_spans_record_as_failures(db: str) -> None:
    span = FakeSpan("execute_tool", {"tool.name": "send_invoice"}, ok=False)
    run = record_span(SQLiteStorage(db), span.name, span.attributes, ok=False)
    assert run == "run_1"
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    assert events[-1].type is EventType.TOOL_FAILED
    assert events[-1].source is Origin.EXTERNAL_AGENT


def test_successful_span_records_with_active_run_fallback(db: str) -> None:
    span = FakeSpan("execute_tool", {"gen_ai.tool.name": "write_file", "file_path": "/tmp/x.txt"})
    got = record_span(SQLiteStorage(db), span.name, span.attributes)
    assert got == "run_1"
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    assert events[-1].type is EventType.TOOL_COMPLETED
    assert events[-1].payload["path"] == "/tmp/x.txt"


def test_telemetry_without_a_run_is_dropped_silently(tmp_path: Path) -> None:
    path = str(tmp_path / "empty.db")
    with SQLiteStorage(path):
        pass
    span = FakeSpan("execute_tool", {"tool.name": "x"})
    assert record_span(SQLiteStorage(path), span.name, span.attributes) is None


def test_processor_end_to_end_when_sdk_present(db: str) -> None:
    otel = pytest.importorskip("opentelemetry.sdk.trace")
    from continuum.otel import make_span_processor

    processor = make_span_processor(SQLiteStorage(db))
    span = FakeSpan("execute_tool", {"tool.name": "write_file", "file_path": "/tmp/y.txt"})
    readable = span  # duck-typed; the processor only reads name/attributes/status
    processor.on_end(readable)  # type: ignore[arg-type]
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    assert events[-1].type is EventType.TOOL_COMPLETED
    assert events[-1].payload["tool"] == "write_file"
    assert otel is not None


def test_processor_factory_gives_install_hint_without_sdk(
    monkeypatch: pytest.MonkeyPatch, db: str
) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", blocked)
    from continuum.otel import make_span_processor

    with pytest.raises(RuntimeError, match="opentelemetry-api"):
        make_span_processor(SQLiteStorage(db))


def test_attr_fallback_on_objects_lacking_get() -> None:
    class CustomAttributes:
        pass

    assert observation_from_span("custom_tool", CustomAttributes()) is None

    class RaisingAttributes:
        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    assert observation_from_span("custom_tool", RaisingAttributes()) is None

    class FlakyGetDict(dict):
        called = False

        def __getattribute__(self, name: str) -> object:
            if name == "get" and not FlakyGetDict.called:
                FlakyGetDict.called = True
                raise AttributeError("transient failure")
            return super().__getattribute__(name)

    assert _attr(FlakyGetDict({"tool.name": "build"}), "tool.name") == "build"


def test_record_span_records_span_and_trace_id(db: str) -> None:
    span = FakeSpan("execute_tool", {"tool.name": "send_invoice"})
    got = record_span(
        SQLiteStorage(db),
        span.name,
        span.attributes,
        run_id="run_1",
        span_id="span-123",
        trace_id="trace-456",
    )
    assert got == "run_1"
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    last_event = events[-1]
    assert last_event.type is EventType.TOOL_COMPLETED
    assert last_event.payload["span_id"] == "span-123"
    assert last_event.payload["trace_id"] == "trace-456"
    assert last_event.payload["tool"] == "send_invoice"


def test_record_span_non_qualifying_span_returns_none(db: str) -> None:
    got = record_span(
        SQLiteStorage(db),
        "llm_call",
        {"gen_ai.request.model": "gpt-4"},
        run_id="run_1",
    )
    assert got is None


def test_processor_lifecycle_methods_and_context(monkeypatch: pytest.MonkeyPatch, db: str) -> None:
    import sys
    import types

    from continuum.otel import make_span_processor

    fake_trace = types.ModuleType("opentelemetry.sdk.trace")

    class SpanProcessor:
        pass

    class ReadableSpan:
        pass

    fake_trace.SpanProcessor = SpanProcessor
    fake_trace.ReadableSpan = ReadableSpan

    monkeypatch.setitem(sys.modules, "opentelemetry", types.ModuleType("opentelemetry"))
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", types.ModuleType("opentelemetry.sdk"))
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", fake_trace)

    processor = make_span_processor(SQLiteStorage(db), run_id="run_1")
    assert processor.on_start(None) is None
    assert processor.shutdown() is True
    assert processor.force_flush() is True
    assert processor.force_flush(1000) is True

    class FakeContext:
        span_id = "span-001"
        trace_id = "trace-999"

    class ContextSpan(FakeSpan):
        def __init__(self, name: str, attributes: dict[str, object], ok: bool = True) -> None:
            super().__init__(name, attributes, ok=ok)
            self.context = FakeContext()

    span = ContextSpan("execute_tool", {"tool.name": "read_file", "path": "/data/f.txt"})
    processor.on_end(span)  # type: ignore[arg-type]

    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    last = events[-1]
    assert last.type is EventType.TOOL_COMPLETED
    assert last.payload["tool"] == "read_file"
    assert last.payload["path"] == "/data/f.txt"
    assert last.payload["span_id"] == "span-001"
    assert last.payload["trace_id"] == "trace-999"
