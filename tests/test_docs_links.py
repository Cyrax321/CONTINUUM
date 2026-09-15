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

# [text](target) and ![alt](target), capturing the target plus any title.
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _pages() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def _destination(raw: str) -> str:
    """Return a link's destination, dropping any optional Markdown title.

    ``[text](x.svg "Diagram")`` targets ``x.svg``; the trailing title is
    decoration, not part of the path, and left in place it never resolves to a
    file. A bracketed destination may legitimately contain spaces, so it is cut
    at its closing ``>`` rather than at the first whitespace run.
    """
    target = raw.strip()
    if not target:
        return target
    if target.startswith("<"):
        close = target.find(">")
        if close != -1:
            return target[1:close].strip()
    return target.split(None, 1)[0]


def _broken_targets(page: Path) -> list[str]:
    """Relative link targets in ``page`` that do not resolve to an existing file."""
    lines = page.read_text(encoding="utf-8").splitlines()
    broken: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for raw in _LINK.findall(line):
            target = _destination(raw)
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # A fragment or query does not change which file has to exist.
            path = target.split("#", 1)[0].split("?", 1)[0]
            if not path:
                continue
            if not (page.parent / path).resolve().is_file():
                broken.append(target)
    return broken


@pytest.mark.parametrize("page", _pages(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(page: Path) -> None:
    """Each relative link target resolves to a file that exists."""
    broken = _broken_targets(page)
    assert not broken, f"{page.relative_to(ROOT)} links to non-existent targets: {broken}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A destination with no title is passed through untouched.
        ("glossary.md", "glossary.md"),
        ("assets/x.svg", "assets/x.svg"),
        ("../guides/a.md", "../guides/a.md"),
        # An optional title is not part of the destination.
        ('assets/x.svg "Diagram"', "assets/x.svg"),
        ("assets/x.svg 'Diagram'", "assets/x.svg"),
        ("assets/x.svg (Diagram)", "assets/x.svg"),
        # Angle brackets are packaging, not part of the path, and may wrap spaces.
        ("<assets/x.svg>", "assets/x.svg"),
        ("<assets/a b.svg>", "assets/a b.svg"),
        ('<assets/a b.svg> "Diagram"', "assets/a b.svg"),
        # Whitespace around the destination is insignificant.
        ("  assets/x.svg  ", "assets/x.svg"),
        ("", ""),
    ],
)
def test_destination_separates_path_from_title(raw: str, expected: str) -> None:
    """The title never leaks into the path a link is resolved against."""
    assert _destination(raw) == expected


def test_valid_markdown_links_are_not_broken(tmp_path: Path) -> None:
    """Every supported Markdown form resolves instead of crying wolf."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "x.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "assets" / "a b.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "guide.md").write_text("guide", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "\n".join(
            [
                "[plain](guide.md)",
                "![image](assets/x.svg)",
                '[titled](assets/x.svg "Diagram")',
                '![titled image](assets/x.svg "Diagram")',
                "[bracketed](<assets/x.svg>)",
                '[bracketed and titled](<assets/x.svg> "Diagram")',
                "[spaced](<assets/a b.svg>)",
                '[spaced and titled](<assets/a b.svg> "Diagram")',
                "[fragment](guide.md#heading)",
                "[query](guide.md?v=1)",
                "[external](https://example.com/x.svg)",
                "[mailto](mailto:someone@example.com)",
                "[fragment only](#heading)",
                "[query only](?v=1)",
            ]
        ),
        encoding="utf-8",
    )
    assert _broken_targets(page) == []


def test_broken_relative_link_is_reported(tmp_path: Path) -> None:
    """A dead relative target is still caught, titles or not."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "x.svg").write_text("<svg/>", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "\n".join(
            [
                '![ok](assets/x.svg "Diagram")',
                '![dead](assets/missing.svg "Diagram")',
                "[dead plain](missing.md)",
            ]
        ),
        encoding="utf-8",
    )
    assert _broken_targets(page) == ["assets/missing.svg", "missing.md"]


def test_links_inside_fenced_code_are_skipped(tmp_path: Path) -> None:
    """A link inside a fenced block is prose, not a shipped URL."""
    page = tmp_path / "page.md"
    page.write_text(
        "\n".join(
            [
                "```markdown",
                '![not shipped](assets/never-existed.svg "Diagram")',
                "```",
            ]
        ),
        encoding="utf-8",
    )
    assert _broken_targets(page) == []
