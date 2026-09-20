"""Guard version drift between pyproject.toml, git tags, and README (#838)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'version\s*=\s*"([^"]+)"', text)
    assert m, "no version in pyproject.toml"
    return m.group(1)


def test_pyproject_version_matches_package():
    import continuum

    assert continuum.__version__ == _pyproject_version()


def test_readme_pins_match_pyproject():
    v = _pyproject_version()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"continuum-agent=={v}" in readme


def test_git_tag_exists_or_skip():
    v = _pyproject_version()
    tags = subprocess.check_output(["git", "tag", "--list", f"v{v}"], text=True)
    if not tags.strip():
        import pytest

        pytest.skip(f"tag v{v} not found locally")
    assert f"v{v}" in tags
