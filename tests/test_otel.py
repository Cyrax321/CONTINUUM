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


def test_non_tool_spans_are_ignored(db: str) -> None:
    assert observation_from_span("llm_call", {"gen_ai.request.model": "gpt"}) is None
    assert observation_from_span("http get", {"url": "https://x"}) is None
    assert record_span(SQLiteStorage(db), "http get", {"url": "https://x"}) is None


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


def test_attr_fallback_when_lacking_get_or_raising_attribute_error() -> None:
    """_attr handles non-mappings and dict subclasses raising AttributeError."""
    assert _attr(12345, "key") is None
    assert _attr("not_a_mapping", "key") is None
    assert _attr(["item"], "key") is None

    class DictRaisingAttributeError(dict):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._raised = False

        def get(self, key: str, default: object = None) -> object:
            if not self._raised:
                self._raised = True
                raise AttributeError("simulated lookup failure")
            return super().get(key, default)

    d = DictRaisingAttributeError({"tool.name": "custom_tool"})
    assert _attr(d, "tool.name") == "custom_tool"


def test_record_span_with_span_id_and_trace_id(db: str) -> None:
    """Explicit span_id and trace_id are recorded in the event payload."""
    span = FakeSpan("execute_tool", {"tool.name": "send_invoice", "path": "/inv/1"})
    run = record_span(
        SQLiteStorage(db),
        span.name,
        span.attributes,
        span_id="span-1234",
        trace_id="trace-5678",
    )
    assert run == "run_1"
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    payload = events[-1].payload
    assert payload["span_id"] == "span-1234"
    assert payload["trace_id"] == "trace-5678"
    assert payload["tool"] == "send_invoice"


def test_processor_lifecycle_methods_and_context_propagation(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ContinuumSpanProcessor methods on_start, shutdown, force_flush, and on_end context."""
    import sys
    import types

    if "opentelemetry.sdk.trace" not in sys.modules:
        trace_mod = types.ModuleType("opentelemetry.sdk.trace")

        class DummySpanProcessor:
            pass

        class DummyReadableSpan:
            pass

        trace_mod.SpanProcessor = DummySpanProcessor  # type: ignore[attr-defined]
        trace_mod.ReadableSpan = DummyReadableSpan  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", trace_mod)

    from continuum.otel import make_span_processor

    proc = make_span_processor(SQLiteStorage(db), run_id="run_1")
    assert proc.on_start(None) is None
    assert proc.shutdown() is True
    assert proc.force_flush(15000) is True

    class SpanContext:
        span_id = "span-ctx-100"
        trace_id = "trace-ctx-200"

    class DuckSpan:
        name = "tool_call"
        attributes = {"tool.name": "query_db", "path": "/db/query"}
        status = None
        context = SpanContext()

    proc.on_end(DuckSpan())  # type: ignore[arg-type]
    with SQLiteStorage(db) as store:
        events = store.read_events("run_1")
    last_payload = events[-1].payload
    assert last_payload["tool"] == "query_db"
    assert last_payload["span_id"] == "span-ctx-100"
    assert last_payload["trace_id"] == "trace-ctx-200"
    assert last_payload["ok"] is True

