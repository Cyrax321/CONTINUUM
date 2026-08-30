"""Regression test for dashboard escaping (issue #334)."""

from __future__ import annotations

import html

from continuum.dashboard.app import render_dashboard_html, render_run_detail_html
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def test_dashboard_escaping_list_and_detail() -> None:
    storage = SQLiteStorage(":memory:")
    evil = '<script>alert(1)</script> & "quote" <b>bold</b>'
    run_id = "r1"
    storage.create_run(Run(run_id=run_id, goal=evil))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": evil})
    list_html = render_dashboard_html(storage)
    assert "<script>" not in list_html
    assert evil not in list_html
    assert html.escape(evil) in list_html
    assert "&lt;script&gt;" in list_html
    assert "&amp;" in list_html
    assert html.escape(evil) in list_html
    detail_html = render_run_detail_html(storage, run_id)
    assert "<script>" not in detail_html
    assert evil not in detail_html
    assert html.escape(evil) in detail_html
    assert "&lt;script&gt;" in detail_html
    assert "Goal:" in detail_html
    storage2 = SQLiteStorage(":memory:")
    storage2.create_run(Run(run_id="r2", goal="hello world"))
    storage2.append_event("r2", EventType.RUN_STARTED, {"goal": "hello world"})
    list2 = render_dashboard_html(storage2)
    assert "hello world" in list2
    assert "hello world" in render_run_detail_html(storage2, "r2")
    detail = render_run_detail_html(storage, run_id)
    assert "&quot;" in detail
    assert html.escape(evil) in detail
    assert "<script>" not in detail
    storage.close()
    storage2.close()


def test_dashboard_escaping_run_id_and_status() -> None:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="r1", goal="g"))
    storage.create_run(Run(run_id="r2", goal="<img src=x onerror=alert(1)>"))
    html_out = render_dashboard_html(storage)
    assert "<img" not in html_out
    assert "&lt;img" in html_out
    detail = render_run_detail_html(storage, "r2")
    assert "<img" not in detail
    assert "&lt;img" in detail
    storage.close()
