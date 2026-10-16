"""Guard that every top-level markdown file is tracked by markdownlint (issue #1703).

``.github/workflows/markdownlint.yml`` enumerates files in a ``globs:`` block.
Without a guard checking that list against the repository contents, a file added
to the repository root can be silently overlooked and never linted.

This guard asserts that every top-level ``*.md`` file is either matched by a
glob in the workflow or explicitly listed in ``EXCLUDED_TOP_LEVEL_MARKDOWN``
with a documented reason. We read the workflow file and compare its glob list
to the root markdown set rather than changing the workflow to a blanket glob,
because an unvetted blanket glob would fail on files that have not yet been
audited (such as translated READMEs tracked by issue #1692).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "markdownlint.yml"

#: Top-level markdown files that are deliberately excluded from markdownlint,
#: mapped to the documented rationale for their exclusion.
EXCLUDED_TOP_LEVEL_MARKDOWN: dict[str, str] = {
    "AGENTS.md": "Internal operational guidance for AI agents and maintainers",
    "AUTHORS.md": "Raw contributor attribution roster",
    "CHANGELOG.md": "Keep a Changelog release history with historical formatting",
    "CONTINUUM_PROJECT_DOCUMENT.md": "Internal design document and project specifications",
    "README.es.md": "Translated Spanish README (unlinted pending issue #1692 audit)",
    "README.ja.md": "Translated Japanese README (unlinted pending issue #1692 audit)",
    "README.ko.md": "Translated Korean README (unlinted pending issue #1692 audit)",
    "README.pt-BR.md": "Translated Portuguese README (unlinted pending issue #1692 audit)",
    "README.zh-CN.md": "Translated Chinese README (unlinted pending issue #1692 audit)",
    "STATUS.md": "Internal project status snapshot and audit notes",
    "WORKLOG.md": "Internal developer session worklog",
    "project.md": "Internal high-level project notes",
    "test.md": "Internal test verification scorecard and history",
}


def _extract_globs(workflow_path: Path) -> list[str]:
    """Extract glob patterns configured for the markdownlint action."""
    assert workflow_path.is_file(), f"workflow file not found: {workflow_path}"

    try:
        import yaml

        with workflow_path.open(encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
        for step in doc.get("jobs", {}).get("markdownlint", {}).get("steps", []):
            with_block = step.get("with", {})
            if "globs" in with_block:
                globs_text = with_block["globs"]
                if isinstance(globs_text, str):
                    return [line.strip() for line in globs_text.splitlines() if line.strip()]
                if isinstance(globs_text, list):
                    return [str(g).strip() for g in globs_text if str(g).strip()]
    except ImportError:
        pass

    globs: list[str] = []
    in_globs = False
    for line in workflow_path.read_text(encoding="utf-8").splitlines():
        if re.match(r"\s*globs:\s*\|", line):
            in_globs = True
            continue
        if in_globs:
            match = re.match(r"\s+([^\s#]+)", line)
            if match:
                globs.append(match.group(1).strip())
            else:
                break
    return globs


def _is_covered(filename: str, globs: list[str]) -> bool:
    """Whether a filename matches at least one glob pattern."""
    return any(fnmatch.fnmatch(filename, pattern) for pattern in globs)


def _check_top_level_markdown_coverage(
    root_md_files: list[str], globs: list[str], exclusions: dict[str, str]
) -> list[str]:
    """Return the names of any top-level markdown files missing from globs and exclusions."""
    missing: list[str] = []
    for name in root_md_files:
        if not _is_covered(name, globs) and name not in exclusions:
            missing.append(name)
    return missing


def test_markdownlint_workflow_declares_globs() -> None:
    """The markdownlint workflow must exist and configure at least one glob."""
    globs = _extract_globs(WORKFLOW)
    assert globs, f"{WORKFLOW} does not define a non-empty globs: block under markdownlint step"


def test_every_top_level_markdown_file_is_covered_or_deliberately_excluded() -> None:
    """Every top-level markdown file must be in the lint globs or explicitly excluded."""
    globs = _extract_globs(WORKFLOW)
    root_md_files = sorted(p.name for p in REPO_ROOT.glob("*.md"))
    missing = _check_top_level_markdown_coverage(root_md_files, globs, EXCLUDED_TOP_LEVEL_MARKDOWN)
    assert not missing, (
        f"Top-level markdown file(s) missing from markdownlint globs: {missing}. "
        "Either add them to .github/workflows/markdownlint.yml globs or "
        "explicitly record them in EXCLUDED_TOP_LEVEL_MARKDOWN with a rationale (issue #1703)."
    )


def test_deliberate_exclusions_state_a_non_empty_reason() -> None:
    """Every exclusion must carry a substantive rationale."""
    for filename, reason in EXCLUDED_TOP_LEVEL_MARKDOWN.items():
        assert reason.strip(), f"Exclusion for {filename} must provide a non-empty reason"


def test_no_deliberately_excluded_file_is_already_covered_by_globs() -> None:
    """Files covered by globs should not linger in the deliberate exclusions mapping."""
    globs = _extract_globs(WORKFLOW)
    redundant = [name for name in EXCLUDED_TOP_LEVEL_MARKDOWN if _is_covered(name, globs)]
    assert not redundant, (
        f"The following files are already covered by workflow globs and should be "
        f"removed from EXCLUDED_TOP_LEVEL_MARKDOWN: {redundant}"
    )


def test_missing_file_failure_message_names_the_file() -> None:
    """Failure message must clearly name the unlinted file."""
    globs = ["README.md"]
    exclusions = {"CONTRIBUTING.md": "reason"}
    missing = _check_top_level_markdown_coverage(
        ["README.md", "CONTRIBUTING.md", "UNTRACKED_DOC.md"],
        globs,
        exclusions,
    )
    assert missing == ["UNTRACKED_DOC.md"]
