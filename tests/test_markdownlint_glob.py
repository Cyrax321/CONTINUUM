"""The markdownlint glob list must cover every top-level markdown file (#1703).

`.github/workflows/markdownlint.yml` enumerates four files. Nothing used to
check that list against what the repository actually contains, so a file added
to the root was simply never linted. That is how the five translated READMEs
came to be unlinted.

This guard reads the workflow file and compares its glob list to the set of
top-level `*.md` files. A file that is deliberately not linted must be named
in `EXCLUDED` below with a reason, so the exclusion is a decision rather than
an oversight.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "markdownlint.yml"


# Top-level markdown files that are deliberately not linted, with the reason.
EXCLUDED: dict[str, str] = {
    "AGENTS.md": "agent instructions, not documentation",
    "CONTINUUM_PROJECT_DOCUMENT.md": "generated project document, not hand-edited",
    "STATUS.md": "long-form status document, linted separately if at all",
    "WORKLOG.md": "working log, not documentation",
    "project.md": "project brief, not user-facing docs",
    "test.md": "adversarial MCP test report, not user-facing docs",
}


def lint_globs() -> set[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"globs: \|\n((?:            \S.*\n)+)", text)
    assert match, "could not find the globs block in markdownlint.yml"
    return {line.strip() for line in match.group(1).splitlines() if line.strip()}


def top_level_markdown() -> set[str]:
    return {path.name for path in ROOT.glob("*.md")}


def test_every_top_level_markdown_is_linted_or_explicitly_excluded() -> None:
    linted = lint_globs()
    present = top_level_markdown()
    unaccounted = present - linted - set(EXCLUDED)

    assert not unaccounted, (
        "top-level markdown files not covered by the markdownlint glob and not "
        "listed in EXCLUDED: " + ", ".join(sorted(unaccounted))
    )


def test_excluded_files_actually_exist() -> None:
    present = top_level_markdown()
    stale = set(EXCLUDED) - present

    assert not stale, "EXCLUDED names files that no longer exist: " + ", ".join(sorted(stale))


def test_linted_files_actually_exist() -> None:
    present = top_level_markdown()
    missing = lint_globs() - present

    assert not missing, "the markdownlint glob names files that do not exist: " + ", ".join(
        sorted(missing)
    )


def test_translated_readmes_are_linted() -> None:
    linted = lint_globs()

    for name in (
        "README.es.md",
        "README.ja.md",
        "README.ko.md",
        "README.pt-BR.md",
        "README.zh-CN.md",
    ):
        assert name in linted, f"{name} is not in the markdownlint glob"
