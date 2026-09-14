"""OpenTelemetry bridge: mirror tool-call spans into the event log (seam 5).

Production agent stacks overwhelmingly emit OpenTelemetry. This module turns
that existing telemetry into CONTINUUM evidence with zero framework
cooperation and zero code changes in the traced application: register one
span processor, and every span that looks like a tool call lands in the
active run's hash-chained log exactly as `continuum observe` records them.

Design notes:

- The pure core (:func:`observation_from_span`, :func:`record_span`) depends
  on nothing: spans are duck-typed objects with ``name`` / ``attributes`` /
  ``status``, so tests run without the OTel SDK installed.
- The SDK-facing glue (:func:`make_span_processor`) imports OpenTelemetry
  lazily and raises an actionable hint when it is absent, mirroring how the
  adapters treat their optional frameworks.
- Recognition is deliberately heuristic and documented: production pipelines
  use several attribute conventions (``gen_ai.tool.name`` per the GenAI
  semantic conventions, plus popular vendor spellings), so anything that
  names a tool is treated as a tool span. Non-tool spans are ignored; a
  failed span records TOOL_FAILED instead of TOOL_COMPLETED.

Provenance follows house rules: observations are EXTERNAL_AGENT evidence,
never trusted state.
"""

from __future__ import annotations

from typing import Any

from continuum.events import EventType
from continuum.models import Origin
from continuum.storage.base import Storage

__all__ = ["observation_from_span", "record_span", "make_span_processor"]

#: Span-attribute keys recognised as "this span called a tool", by convention
#: family. First match wins.
_TOOL_NAME_KEYS = (
    "gen_ai.tool.name",
    "tool.name",
    "openinference.tool.name",
    "mcp.tool.name",
    "function.name",
)

#: Attribute keys recognised as a primary file path, matching observe's
#: extraction order so OTel and hook observations look identical downstream.
_PATH_KEYS = ("file_path", "notebook_path", "path", "filepath", "target")


def _attr(attributes: Any, key: str) -> Any:
    try:
        return attributes.get(key)
    except AttributeError:
        if isinstance(attributes, dict):
            return attributes.get(key)
        return None


def observation_from_span(name: str, attributes: Any, *, ok: bool = True) -> dict[str, Any] | None:
    """Extract an observation payload from one span, or None to ignore it.

    A span qualifies when any known attribute names a tool. The payload shape
    matches `continuum observe`'s: {"tool": ..., "path": ..., optional size
    and digest keys are never invented here because a span does not carry the
    file's bytes}.
    """
    tool: str | None = None
    for key in _TOOL_NAME_KEYS:
        value = _attr(attributes, key)
        if isinstance(value, str) and value:
            tool = value
            break
    if tool is None:
        return None

    payload: dict[str, Any] = {"tool": tool}
    for key in _PATH_KEYS:
        value = _attr(attributes, key)
        if isinstance(value, str) and value:
            payload["path"] = value
            break
    payload["via"] = "otel"
    payload["ok"] = bool(ok)
    for _key in ("continuum.correlation_id", "correlation_id"):
        _value = _attr(attributes, _key)
        if _value is not None:
            from continuum.provenance.correlation import normalize_correlation_id

            try:
                _clean = normalize_correlation_id(_value)
            except ValueError:
                _clean = None
            if _clean is not None:
                payload["correlation_id"] = _clean
            break
    return payload


def record_span(
    storage: Storage,
    name: str,
    attributes: Any,
    *,
    ok: bool = True,
    run_id: str | None = None,
) -> str | None:
    """Record one span into the active (or explicit) run. Returns run_id."""
    payload = observation_from_span(name, attributes, ok=ok)
    if payload is None:
        return None
    target = run_id
    if target is None:
        active = storage.get_active_run()
        target = active.run_id if active else None
    if target is None:
        # Same tolerance as observe: telemetry without a run is dropped, not
        # an error, because tracing exists across every session in the repo.
        return None
    event_type = EventType.TOOL_COMPLETED if ok else EventType.TOOL_FAILED
    storage.append_event(target, event_type, payload, source=Origin.EXTERNAL_AGENT)
    return target


def make_span_processor(
    storage: Storage,
    *,
    run_id: str | None = None,
) -> Any:
    """Return an OpenTelemetry SpanProcessor that mirrors tool spans.

    Raises RuntimeError with an install hint when the OpenTelemetry API is
    not importable; add ``opentelemetry-api`` (and your existing exporter of
    choice stays untouched - this processor only observes).
    """
    try:
        from opentelemetry.sdk.trace import SpanProcessor
    except Exception as exc:  # pragma: no cover - exercised only without SDK
        raise RuntimeError(
            "OpenTelemetry is not installed. Install 'opentelemetry-api' "
            "(and 'opentelemetry-sdk' if you create the provider here) to use "
            "the CONTINUUM span processor."
        ) from exc

    from opentelemetry.sdk.trace import ReadableSpan

    base: type = SpanProcessor  # Any when the SDK is absent for mypy

    class ContinuumSpanProcessor(base):  # type: ignore[misc]
        """Mirror ended tool spans into the CONTINUUM event log."""

        def __init__(self, target_run_id: str | None = None) -> None:
            self._run_id = target_run_id

        def on_end(self, span: ReadableSpan) -> None:
            attributes = getattr(span, "attributes", {}) or {}
            status = getattr(span, "status", None)
            ok = getattr(status, "is_ok", True) if status is not None else True
            record_span(
                storage,
                span.name,
                attributes,
                ok=bool(ok),
                run_id=self._run_id,
            )

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def shutdown(self) -> bool:
            return True

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return ContinuumSpanProcessor(run_id)
