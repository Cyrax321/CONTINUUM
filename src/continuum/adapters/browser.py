"""Browser / HTTP environment adapter (playwright).

Optional dependency: ``playwright`` (plus a browser binary) must be installed.
The adapter imports it lazily so the package stays dependency-light; smoke
tests skip when it is absent. See issue #158.
"""

from __future__ import annotations

from continuum.adapters import run_action
from continuum.adapters.actions import AdapterAction, AdapterResult
from continuum.adapters.generic import GenericAgentAdapter
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.base import Storage

__all__ = [
    "BrowserAdapter",
]


class BrowserAdapter(GenericAgentAdapter):
    """Drives a browser via playwright, recorded as an action."""

    @staticmethod
    def available() -> bool:
        """Whether the ``playwright`` package imports.

        This probes the Python dependency only, not whether a browser binary
        is installed: that surfaces at execution time. Smoke tests skip on
        ``False`` instead of failing.
        """
        try:
            import playwright  # noqa: F401

            return True
        except ImportError:
            return False

    def __init__(self, storage: Storage, *, engine: RecoveryEngine | None = None) -> None:
        super().__init__(storage, engine=engine)

    def navigate(self, run_id: str, url: str, *, dep_scope: str | None = None) -> AdapterResult:
        """Load ``url`` in headless Chromium and return the rendered page.

        The HTML of the loaded page comes back as ``AdapterResult.output``;
        navigation failures are captured into the result (status ``failed``
        with ``error`` set) rather than raised, because the attempt is
        already recorded in the ledger. Raises ``RuntimeError`` up front
        when playwright is not installed. ``dep_scope`` names the
        dependency the action should be scoped to for recovery.
        """
        if not self.available():
            raise RuntimeError("playwright is not installed")

        def _run() -> str:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(url)
                content = page.content()
                browser.close()
                return str(content)

        return run_action(
            self,
            run_id,
            AdapterAction(name="browser.navigate", params={"url": url}, dep_scope=dep_scope),
            _run,
        )
