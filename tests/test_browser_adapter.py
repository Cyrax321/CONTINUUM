from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import BrowserAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="test browser adapter"))
    return storage


def test_browser_adapter_available_when_playwright_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_playwright = ModuleType("playwright")
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    assert BrowserAdapter.available() is True


def test_browser_adapter_available_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    assert BrowserAdapter.available() is False


def test_browser_adapter_navigate_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BrowserAdapter, "available", staticmethod(lambda: False))
    adapter = BrowserAdapter(_storage())
    with pytest.raises(RuntimeError, match="playwright is not installed"):
        adapter.navigate("run_1", "https://example.com")


def test_browser_adapter_navigate_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage()
    adapter = BrowserAdapter(storage)
    monkeypatch.setattr(BrowserAdapter, "available", staticmethod(lambda: True))

    mock_page = MagicMock()
    mock_page.content.return_value = "<html><body>rendered content</body></html>"

    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_playwright_inst = MagicMock()
    mock_playwright_inst.chromium.launch.return_value = mock_browser

    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_playwright_inst
    mock_ctx.__exit__.return_value = False

    fake_sync_api = ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = MagicMock(return_value=mock_ctx)

    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    result = adapter.navigate("run_1", "https://example.com/test")

    assert result.status == "completed"
    assert result.output == "<html><body>rendered content</body></html>"
    mock_browser.new_page.assert_called_once()
    mock_page.goto.assert_called_once_with("https://example.com/test")
    mock_page.content.assert_called_once()
    mock_browser.close.assert_called_once()

    recorded = ActionLedger(storage, "run_1").all()
    assert len(recorded) == 1
    assert recorded[0].action_type == "browser.navigate"
    assert recorded[0].arguments == {"url": "https://example.com/test"}


def test_browser_adapter_navigate_failure_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage()
    adapter = BrowserAdapter(storage)
    monkeypatch.setattr(BrowserAdapter, "available", staticmethod(lambda: True))

    mock_page = MagicMock()
    mock_page.goto.side_effect = RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_playwright_inst = MagicMock()
    mock_playwright_inst.chromium.launch.return_value = mock_browser

    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_playwright_inst
    mock_ctx.__exit__.return_value = False

    fake_sync_api = ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = MagicMock(return_value=mock_ctx)

    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    result = adapter.navigate("run_1", "https://invalid.example.test")

    assert result.status == "failed"
    assert result.error is not None
    assert "RuntimeError: net::ERR_NAME_NOT_RESOLVED" in result.error
    assert result.output is None

    recorded = ActionLedger(storage, "run_1").pending()
    assert len(recorded) == 1
    assert recorded[0].action_type == "browser.navigate"
    assert recorded[0].arguments == {"url": "https://invalid.example.test"}


def test_browser_adapter_navigate_passes_dep_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage()
    adapter = BrowserAdapter(storage)
    monkeypatch.setattr(BrowserAdapter, "available", staticmethod(lambda: True))

    mock_page = MagicMock()
    mock_page.content.return_value = "<html>content</html>"

    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_playwright_inst = MagicMock()
    mock_playwright_inst.chromium.launch.return_value = mock_browser

    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_playwright_inst
    mock_ctx.__exit__.return_value = False

    fake_sync_api = ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = MagicMock(return_value=mock_ctx)

    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    adapter.navigate("run_1", "https://example.com", dep_scope="web_source")

    recorded = ActionLedger(storage, "run_1").all()
    assert len(recorded) == 1
    assert recorded[0].dep_scope == "web_source"
