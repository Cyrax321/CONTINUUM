"""Every relative link or image in ``docs/`` must resolve to a real file (#1113).

``docs/release-notes.md`` embedded the crash-recovery visual as
``docs/assets/crash-recovery.svg``. The file sits at ``docs/assets/``, so from
that page the URL resolved to ``docs/docs/assets/crash-recovery.svg`` and the
image never rendered. Every other page in the tree gets the relative form
right, so the doubled prefix was the outlier, not the convention — but nothing
checked it, and the page had carried a broken embed since it was written.

The guard resolves each relative target against the page that links it, so a
page that moves or a link that drops a ``../`` fails here instead of shipping
a dead URL. External URLs, fragments and mailto links are out of scope.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

# [text](target) and ![alt](target), capturing the target.
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _pages() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


@pytest.mark.parametrize("page", _pages(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(page: Path) -> None:
    """Each relative link target resolves to a file that exists."""
    lines = page.read_text(encoding="utf-8").splitlines()
    broken: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in _LINK.findall(line):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # Strip a fragment or query before resolving; only the file must exist.
            path = target.split("#", 1)[0].split("?", 1)[0]
            if not path:
                continue
            if not (page.parent / path).resolve().is_file():
                broken.append(target)
    assert not broken, f"{page.relative_to(ROOT)} links to non-existent targets: {broken}"
