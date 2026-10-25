import sys
from unittest.mock import MagicMock, patch

import pytest

import continuum.adapters.browser as browser_mod
from continuum.adapters.browser import BrowserAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def storage() -> SQLiteStorage:
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="run_1", goal="env"))
    return store


@pytest.fixture
def run_id() -> str:
    return "run_1"


def test_browser_adapter_available_flag(storage: SQLiteStorage) -> None:
    adapter = BrowserAdapter(storage)

    with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
        assert adapter.available() is False

    mock_pw = MagicMock()
    with patch.dict(sys.modules, {"playwright": mock_pw, "playwright.sync_api": mock_pw}):
        assert adapter.available() is True


def test_browser_adapter_navigate_without_playwright(storage: SQLiteStorage, run_id: str) -> None:
    adapter = BrowserAdapter(storage)
    with patch.object(adapter, "available", return_value=False), pytest.raises(RuntimeError):
        adapter.navigate(run_id, "https://example.com")


def test_browser_adapter_navigate_success(storage: SQLiteStorage, run_id: str) -> None:
    adapter = BrowserAdapter(storage)

    mock_page = MagicMock()
    mock_page.content.return_value = "<html><body>Hello World</body></html>"

    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_pw_ctx = MagicMock()
    mock_pw_ctx.chromium.launch.return_value = mock_browser

    mock_sync_api = MagicMock()
    mock_sync_api.sync_playwright.return_value.__enter__.return_value = mock_pw_ctx

    with (
        patch.dict(
            sys.modules,
            {"playwright": MagicMock(), "playwright.sync_api": mock_sync_api},
        ),
        patch.object(adapter, "available", return_value=True),
    ):
        result = adapter.navigate(run_id, "https://example.com")

        assert result.status == "completed"
        assert "Hello World" in str(result.output)
        mock_page.goto.assert_called_once_with("https://example.com")
        mock_browser.close.assert_called_once()


def test_browser_adapter_navigate_failure_captured(storage: SQLiteStorage, run_id: str) -> None:
    adapter = BrowserAdapter(storage)

    mock_page = MagicMock()
    mock_page.goto.side_effect = Exception("Connection timed out")

    mock_browser = MagicMock()
    mock_browser.new_page.return_value = mock_page

    mock_pw_ctx = MagicMock()
    mock_pw_ctx.chromium.launch.return_value = mock_browser

    mock_sync_api = MagicMock()
    mock_sync_api.sync_playwright.return_value.__enter__.return_value = mock_pw_ctx

    with (
        patch.dict(
            sys.modules,
            {"playwright": MagicMock(), "playwright.sync_api": mock_sync_api},
        ),
        patch.object(adapter, "available", return_value=True),
    ):
        result = adapter.navigate(run_id, "https://invalid-url.fake")

        assert result.status == "failed"
        assert result.error is not None
        assert "Connection timed out" in str(result.error)


def test_browser_adapter_dep_scope_forwarding(storage: SQLiteStorage, run_id: str) -> None:
    adapter = BrowserAdapter(storage)

    with (
        patch.object(adapter, "available", return_value=True),
        patch.object(browser_mod, "run_action") as mock_run_action,
    ):
        adapter.navigate(run_id, "https://example.com", dep_scope="custom_scope")

        mock_run_action.assert_called_once()
        action_arg = mock_run_action.call_args[0][2]
        assert action_arg.dep_scope == "custom_scope"
