"""The browser adapter's execution body (issue #1683).

``navigate`` used to be tested only in the state where it refuses to work:
``test_environment_adapters.py`` has one test, and it skips whenever
playwright is installed. So the happy path, the failure path, and
``dep_scope`` forwarding were never exercised at all -- the adapter shipped
with coverage of exactly the branch that declines to run.

Here playwright is patched at the import boundary, so the execution path is
driven deterministically: no playwright package, no browser binary, and no
network. The page is a fake whose ``goto`` and ``content`` are the only
behaviour, and ``sync_playwright`` is installed into ``sys.modules`` so the
adapter's own lazy import resolves to it. Patching is scoped to each test
through ``monkeypatch`` so nothing leaks into the rest of the session.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import BrowserAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="browse"))
    return storage


class _FakePage:
    """The slice of the playwright page interface ``navigate`` touches."""

    def __init__(self, content: str, fail_on: str | None = None) -> None:
        self._content = content
        self._fail_on = fail_on
        self.visited: list[str] = []
        self.closed = False

    def goto(self, url: str) -> None:
        self.visited.append(url)
        if self._fail_on is not None:
            raise RuntimeError(self._fail_on)

    def content(self) -> str:
        return self._content


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        self.closed = True
        self._page.closed = True


class _FakeChromium:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.launches = 0

    def launch(self) -> _FakeBrowser:
        self.launches += 1
        return _FakeBrowser(self._page)


class _FakePlaywright:
    """What ``with sync_playwright() as p`` yields in the adapter's body."""

    def __init__(self, page: _FakePage) -> None:
        self.chromium = _FakeChromium(page)

    def __enter__(self) -> _FakePlaywright:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _patch_playwright(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> None:
    """Install a fake ``playwright`` package for the duration of one test."""
    sync_api = ModuleType("playwright.sync_api")

    def sync_playwright() -> _FakePlaywright:
        return _FakePlaywright(page)

    sync_api.sync_playwright = sync_playwright  # type: ignore[attr-defined]
    package = ModuleType("playwright")
    package.sync_api = sync_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def _without_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the driver-absent state regardless of the environment."""
    monkeypatch.setitem(sys.modules, "playwright", None)


# --- availability ------------------------------------------------------------ #


def test_available_is_true_when_playwright_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_playwright(monkeypatch, _FakePage("<html>ok</html>"))
    assert BrowserAdapter.available() is True


def test_available_is_false_without_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_playwright(monkeypatch)
    assert BrowserAdapter.available() is False


# --- refusal ----------------------------------------------------------------- #


def test_navigate_refuses_before_touching_anything_without_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver check happens up front, so nothing is recorded when it fails."""
    storage = _storage()
    _without_playwright(monkeypatch)

    with pytest.raises(RuntimeError, match="playwright is not installed"):
        BrowserAdapter(storage).navigate("run_1", "https://example.com")

    assert ActionLedger(storage, "run_1").all() == []


# --- the execution path ----------------------------------------------------- #


def test_navigate_returns_the_rendered_page_and_records_the_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML lands in the output, the URL is visited once, and the browser closes."""
    storage = _storage()
    page = _FakePage("<html>rendered</html>")
    _patch_playwright(monkeypatch, page)

    result = BrowserAdapter(storage).navigate("run_1", "https://example.com")

    assert result.status == "completed"
    assert result.output == "<html>rendered</html>"
    assert page.visited == ["https://example.com"]
    assert page.closed is True

    [entry] = ActionLedger(storage, "run_1").all()
    assert entry.action_type == "browser.navigate"
    assert entry.arguments == {"url": "https://example.com"}


def test_navigate_captures_a_navigation_failure_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt is already recorded by the time the page fails, so it is captured (#1683).

    A timeout or a refused connection does not prove the side effect did not
    happen, so the exception must not escape: it becomes a failed result the
    ledger already knows how to reconcile.
    """
    storage = _storage()
    page = _FakePage("<html>never loaded</html>", fail_on="net::ERR_CONNECTION_REFUSED")
    _patch_playwright(monkeypatch, page)

    result = BrowserAdapter(storage).navigate("run_1", "https://example.com")

    assert result.status == "failed"
    assert result.output is None
    assert "net::ERR_CONNECTION_REFUSED" in (result.error or "")
    # The uncertain attempt is in the ledger, not lost, and nothing raised.
    pending = ActionLedger(storage, "run_1").pending()
    assert len(pending) == 1
    assert "net::ERR_CONNECTION_REFUSED" in (pending[0].last_error or "")


def test_dep_scope_reaches_the_recorded_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dependency scope a caller passes flows through to the ledger entry."""
    storage = _storage()
    _patch_playwright(monkeypatch, _FakePage("<html>ok</html>"))

    BrowserAdapter(storage).navigate("run_1", "https://example.com", dep_scope="web:example.com")

    assert ActionLedger(storage, "run_1").all()[0].dep_scope == "web:example.com"
