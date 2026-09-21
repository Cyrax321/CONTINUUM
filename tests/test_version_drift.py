"""Guard version drift between pyproject.toml, CITATION.cff, git tags, and README.

The sites a release must move together (#838), plus the citation file, which
drifted two releases behind because no guard read it (#1120).
"""

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


def test_citation_file_matches_pyproject():
    """CITATION.cff must state the released version (#1120).

    The citation file is what a downstream paper or dataset records, so a stale
    entry is a silently wrong citation, not a cosmetic drift. It fell two
    releases behind before the guard covered it.
    """
    text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
    assert match, "CITATION.cff states no version"
    assert match.group(1) == _pyproject_version(), (
        f"CITATION.cff says {match.group(1)} but the package is "
        f"{_pyproject_version()}: bump it with the rest of the release sites"
    )


def test_git_tag_exists_or_skip():
    v = _pyproject_version()
    tags = subprocess.check_output(["git", "tag", "--list", f"v{v}"], text=True)
    if not tags.strip():
        import pytest

        pytest.skip(f"tag v{v} not found locally")
    assert f"v{v}" in tags
