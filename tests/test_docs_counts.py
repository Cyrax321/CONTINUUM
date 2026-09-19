"""Guard the documented pytest counts against silent drift (#630, #1109).

README.md, the translated READMEs, CHANGELOG.md, docs/CONTRIBUTING_ONBOARDING.md,
and every ``references/*.md`` doc can state the collected total. The guard
asserts that every figure those files *do* state agrees with the others and
with a live ``pytest --collect-only`` within tolerance. Skips vary by
environment, so only collected totals are compared, never passed/skipped
splits. Regenerate the figures with ``pytest --collect-only -q; pytest -q``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Docs that must state the total. If one of these stopped stating it the guard
# would lose its spine, so absence here is an error.
REQUIRED_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "CONTRIBUTING_ONBOARDING.md",
    ROOT / "CHANGELOG.md",
)

# Docs that may state it. references/ and the translated READMEs are
# user-facing and used to drift unnoticed (#1109, #1071): a doc that states no
# total is skipped, a doc that states a wrong one fails.
OPTIONAL_FILES = (
    *sorted(ROOT.glob("README.*.md")),  # translated READMEs
    *sorted(ROOT.joinpath("references").glob("*.md")),
)

COUNTED_FILES = (*REQUIRED_FILES, *OPTIONAL_FILES)
# Small PRs move the total by a handful of tests; doc rot moves it by the
# hundreds (#316: exact, #630: 135). Tolerance 30 splits the difference.
TOLERANCE = 30

# Every prose form the docs use for the collected total (#664 review):
# "~2,053 collected", "roughly 2,053 tests collected", "approximately 2,053
# tests" (references/testing.md), "~2,053 tests". Passed/skipped figures are
# deliberately unmatched: they vary by environment.
# The `pytest -q` verify comment is listed last and reads every README
# regardless of language: translations rephrase all the prose but keep that
# code comment's shape, and its first number is always the collected total
# (#1071). `.` cannot cross the newline, so the figure stays on that line.
_COLLECTED_RES = (
    re.compile(r"~([\d,]+)`?\s+collected"),
    re.compile(r"roughly\s+([\d,]+)\s+tests\s+collected"),
    re.compile(r"approximately\s+([\d,]+)\s+tests\b"),
    re.compile(r"~([\d,]+)\s+tests\b"),
    re.compile(r"\bwith\s+~?([\d,]+)\s+tests\b", re.IGNORECASE),
    re.compile(
        r"\bvalidated(?:\s+\w+){0,5}\s+and\s+~?([\d,]+)\s+tests\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bvalidado(?:\s+\w+){0,5}\s+y\s+~?([\d,]+)\s+tests\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcerca\s+de\s+~?([\d,]+)\s+tests\b",
        re.IGNORECASE,
    ),
    re.compile(r"^pytest\s+-q\s+#.*?([\d,]+)", re.MULTILINE),
)


def documented_total(path: Path) -> int | None:
    """The collected total ``path`` states, or None if it states none.

    Two different totals in one file is always an error, even for a doc that
    is free to state none at all: a reader cannot tell which one to trust.
    """
    text = path.read_text(encoding="utf-8")
    matches = [m for rx in _COLLECTED_RES for m in rx.findall(text)]
    if not matches:
        return None
    totals = {int(m.replace(",", "")) for m in matches}
    assert len(totals) == 1, f"{path} states inconsistent figures: {sorted(totals)}"
    return totals.pop()


def live_total() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=ROOT,
    )
    assert proc.returncode == 0, f"collect-only failed:\n{proc.stderr[-2000:]}"
    match = re.search(r"(\d[\d,]*)\s+tests? collected", proc.stdout)
    assert match, f"unparsable collect-only output:\n{proc.stdout[-500:]}"
    return int(match.group(1).replace(",", ""))


def test_required_files_state_a_total() -> None:
    stated = {f.name: documented_total(f) for f in REQUIRED_FILES}
    missing = [name for name, total in stated.items() if total is None]
    assert not missing, f"no collected-total figure in: {missing}"


def test_documented_counts_agree() -> None:
    stated: dict[str, int] = {}
    for f in COUNTED_FILES:
        total = documented_total(f)
        if total is not None:
            stated[f.name] = total
    assert len(set(stated.values())) == 1, f"documented counts disagree: {stated}"


def test_documented_narrative_count_forms(tmp_path: Path) -> None:
    """Narrative English and Spanish count claims must remain detectable."""
    cases = (
        ("Built with ~2,241 tests.", 2241),
        ("Built with 2241 tests.", 2241),
        ("Validated with real kills and 1380 tests.", 1380),
        ("Validated with real kills and ~2,241 tests.", 2241),
        ("Validado con muertes reales y 1380 tests.", 1380),
        ("Validado con muertes reales y ~2,241 tests.", 2241),
        ("cerca de 2,311 tests", 2311),
    )
    for index, (text, expected) in enumerate(cases):
        path = tmp_path / f"doc-{index}.md"
        path.write_text(text, encoding="utf-8")
        assert documented_total(path) == expected


@pytest.mark.slow
def test_documented_count_matches_suite() -> None:
    documented = documented_total(ROOT / "README.md")
    assert documented is not None, "README.md states no collected-total figure"
    live = live_total()
    assert abs(live - documented) <= TOLERANCE, (
        f"suite collects {live} tests but docs say ~{documented}: re-sync "
        "README.md, the translated READMEs, CHANGELOG.md, "
        "docs/CONTRIBUTING_ONBOARDING.md, references/testing.md, and "
        "references/install.md (pytest --collect-only -q; pytest -q)"
    )


def test_documented_extras_exist_in_pyproject() -> None:
    """No doc installs an extra that pyproject.toml does not declare.

    ``docs/cloud_api.md`` taught ``uv pip install -e ".[cloud]"`` for an extra
    that never existed (#840): a reader following it got an error from pip and
    nowhere to turn. Every extras bracket in an install context, both the
    ``continuum-agent[x]`` and the editable ``.[x]`` spelling, is checked
    against the declared optional-dependencies.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(data["project"]["optional-dependencies"])

    scanned = [
        ROOT / "README.md",
        *sorted(ROOT.joinpath("docs").rglob("*.md")),
        *sorted(ROOT.joinpath("references").rglob("*.md")),
    ]
    for path in scanned:
        for line in path.read_text(encoding="utf-8").splitlines():
            # Only install-command lines carry extras, and the bracket must
            # sit directly on `continuum-agent` or the editable `.` target;
            # anything else (Codex's `[features]` toml section, markdown
            # links, regex examples) is not a pip extra.
            if "install" not in line.lower():
                continue
            for group in re.findall(r"(?:continuum-agent|\.)\[([\w,-]+)\]", line):
                for extra in group.split(","):
                    assert extra in declared, (
                        f"{path} installs the [{extra}] extra, but pyproject.toml "
                        f"declares only {sorted(declared)}"
                    )


# The landing page's meta description is the figure set search engines and
# link previews render, and it is HTML, so the markdown-only guard above never
# opened it. It drifted three times that way (#1283): the guard below reads it
# and pins both its figures to the code, so the next drift fails a test.
LANDING_PAGE = ROOT / "docs" / "index.html"
_META_CLI_COMMANDS = re.compile(r"([\d,]+)\s+CLI commands")
_META_TESTS = re.compile(r"([\d,]+)\s+tests")


def _meta_description() -> str:
    """The landing page's ``<meta name="description">`` content."""
    text = LANDING_PAGE.read_text(encoding="utf-8")
    match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', text)
    assert match, 'docs/index.html has no <meta name="description"> to guard'
    return match.group(1)


def test_landing_meta_states_the_real_cli_command_count() -> None:
    """Count the subcommands the parser actually builds, in-process, and pin
    the meta description's figure to it."""
    from continuum.cli.main import build_parser

    parser = build_parser()
    subparsers = next(
        action for action in parser._subparsers._group_actions if hasattr(action, "choices")
    )
    description = _meta_description()
    stated = _META_CLI_COMMANDS.search(description)
    assert stated, f"meta description states no CLI-command figure: {description!r}"
    assert int(stated.group(1).replace(",", "")) == len(subparsers.choices), (
        f"docs/index.html says {stated.group(1)} CLI commands, "
        f"build_parser() registers {len(subparsers.choices)}"
    )


@pytest.mark.slow
def test_landing_meta_states_the_real_suite_total() -> None:
    """The meta description's test figure tracks the suite, not the last
    hand-edit that happened to touch the page."""
    description = _meta_description()
    stated = _META_TESTS.search(description)
    assert stated, f"meta description states no test figure: {description!r}"
    live = live_total()
    documented = int(stated.group(1).replace(",", ""))
    assert abs(live - documented) <= TOLERANCE, (
        f"suite collects {live} tests but docs/index.html says {documented}: "
        "re-sync the meta description (pytest --collect-only -q)"
    )
