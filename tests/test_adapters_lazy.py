"""Lazy adapter imports (issue #214).

``continuum.adapters`` sits on the critical path of every entry point,
including the MCP server and each ``continuum observe`` hook subprocess.
Importing it must not pay for the optional SDKs (openai, langgraph,
langchain), while ``from continuum.adapters import LangGraphAgentAdapter``
must keep working unchanged.

Most assertions run in a subprocess: an in-process test would see modules
imported by earlier tests and prove nothing.
"""

from __future__ import annotations

import subprocess
import sys

_OPTIONAL_SDK_ROOTS = ("langgraph", "langchain", "openai", "agents", "playwright")


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_importing_the_package_does_not_import_optional_sdks() -> None:
    result = _run(
        "import continuum.adapters, sys\n"
        "polluted = [m for m in sys.modules if m.split('.')[0] in "
        f"{_OPTIONAL_SDK_ROOTS!r}]\n"
        "assert not polluted, polluted\n"
    )
    assert result.returncode == 0, result.stderr


def test_importing_the_top_level_package_does_not_import_optional_sdks() -> None:
    """``import continuum`` pulls in adapters via its own re-exports; the
    framework names must resolve lazily there too."""
    result = _run(
        "import continuum, sys\n"
        "polluted = [m for m in sys.modules if m.split('.')[0] in "
        f"{_OPTIONAL_SDK_ROOTS!r}]\n"
        "assert not polluted, polluted\n"
    )
    assert result.returncode == 0, result.stderr


def test_from_import_resolves_lazy_names() -> None:
    result = _run(
        "from continuum.adapters import (LangChainAgentAdapter, langchain_available,\n"
        "    LangGraphAgentAdapter, langgraph_available, OpenAIAgentAdapter,\n"
        "    ContinuumContext, openai_agents_available)\n"
        "from continuum import LangGraphAgentAdapter as TopLevel\n"
        "assert TopLevel is LangGraphAgentAdapter\n"
    )
    assert result.returncode == 0, result.stderr


def test_dir_lists_the_full_export_surface() -> None:
    result = _run(
        "import continuum.adapters\n"
        "missing = set(continuum.adapters.__all__) - set(dir(continuum.adapters))\n"
        "assert not missing, missing\n"
    )
    assert result.returncode == 0, result.stderr


def test_unknown_attribute_raises_attribute_error() -> None:
    import pytest

    import continuum.adapters

    with pytest.raises(AttributeError, match="definitely_not_an_adapter"):
        continuum.adapters.definitely_not_an_adapter  # noqa: B018


def test_repeated_access_is_cached_in_the_module_dict() -> None:
    """Caching means __getattr__ runs once: the name must be absent from the
    module dict before first access and present afterwards. Identity alone
    cannot prove this, because Python's import cache returns the same object
    even when __getattr__ fires every time."""
    result = _run(
        "import continuum.adapters\n"
        "assert 'langgraph_available' not in vars(continuum.adapters)\n"
        "_ = continuum.adapters.langgraph_available\n"
        "assert 'langgraph_available' in vars(continuum.adapters)\n"
    )
    assert result.returncode == 0, result.stderr
