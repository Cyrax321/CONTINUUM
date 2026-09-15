"""Guard the documented pytest counts against silent drift (#630).

README.md, the five translated READMEs, docs/CONTRIBUTING_ONBOARDING.md, and
CHANGELOG.md each state the collected total. The guard asserts they agree with
each other and with a live ``pytest --collect-only`` within tolerance. Skips
vary by environment, so only collected totals are compared, never
passed/skipped splits. Regenerate the figures with ``pytest --collect-only -q;
pytest -q``.

The translations were re-synced by hand once and then left alone, so they aged
past the guard's own tolerance with nothing failing (#1071): the English docs
get re-synced, the translations do not, and no test read any of them. They are
user-facing, so they are counted now.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COUNTED_FILES = (
    ROOT / "README.md",
    # The five translations each state the same total in their own prose
    # (#1071). English-only coverage is what let them drift unnoticed.
    ROOT / "README.es.md",
    ROOT / "README.ja.md",
    ROOT / "README.ko.md",
    ROOT / "README.pt-BR.md",
    ROOT / "README.zh-CN.md",
    ROOT / "docs" / "CONTRIBUTING_ONBOARDING.md",
    ROOT / "CHANGELOG.md",
)
# Small PRs move the total by a handful of tests; doc rot moves it by the
# hundreds (#316: exact, #630: 135). Tolerance 30 splits the difference.
TOLERANCE = 30

# Every prose form the three files use for the collected total (#664 review):
# "~2,053 collected", "roughly 2,053 tests collected", "~2,053 tests".
# Passed/skipped figures are deliberately unmatched: they vary by environment.
_COLLECTED_RES = (
    re.compile(r"~([\d,]+)`?\s+collected"),
    re.compile(r"roughly\s+([\d,]+)\s+tests\s+collected"),
    re.compile(r"~([\d,]+)\s+tests\b"),
)

# The translations state the same figures in their own spelling (#1071): the
# pytest comment, the scale bullet, and the library sentence. Passed/skipped
# figures are deliberately unmatched, as above. Each pattern was verified
# against its file to match the collected total only; the shared
# "~2,195 tests" tail form is covered by ``_COLLECTED_RES`` above.
_TRANSLATED_RES = {
    "es": (
        re.compile(r"~\s*([\d,]+)\s+recogidos"),
        re.compile(r"([\d,]+)\s+tests\s+recogidos"),
    ),
    "pt-BR": (
        re.compile(r"~\s*([\d,]+)\s+coletados"),
        re.compile(r"([\d,]+)\s+testes\s+coletados"),
    ),
    "ja": (
        re.compile(r"約\s*([\d,]+)\s*件収集"),
        re.compile(r"約\s*([\d,]+)\s*件のテストが収集"),
        re.compile(r"約\s*([\d,]+)\s*テスト"),
    ),
    "ko": (
        re.compile(r"약\s*([\d,]+)개\s*수집"),
        re.compile(r"약\s*([\d,]+)개\s*테스트가\s*수집"),
        re.compile(r"약\s*([\d,]+)\s*테스트"),
    ),
    "zh-CN": (
        re.compile(r"约\s*([\d,]+)\s*个收集"),
        re.compile(r"约\s*([\d,]+)\s*个测试被收集"),
        re.compile(r"约\s*([\d,]+)\s*个测试"),
    ),
}


def documented_total(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    res = list(_COLLECTED_RES)
    # A translated README is matched by its own spelling plus the shared
    # "~2,195 tests" tail form, which the translations also use.
    stem = path.name[len("README.") : -len(".md")] if path.name.startswith("README.") else ""
    if stem in _TRANSLATED_RES:
        res += list(_TRANSLATED_RES[stem])
    matches = [m for rx in res for m in rx.findall(text)]
    assert matches, f"{path.name} states no collected-total figure"
    totals = {int(m.replace(",", "")) for m in matches}
    assert len(totals) == 1, f"{path.name} states inconsistent figures: {sorted(totals)}"
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


def test_documented_counts_agree() -> None:
    totals = {f.name: documented_total(f) for f in COUNTED_FILES}
    assert len(set(totals.values())) == 1, f"documented counts disagree: {totals}"


@pytest.mark.slow
def test_documented_count_matches_suite() -> None:
    documented = documented_total(COUNTED_FILES[0])
    live = live_total()
    assert abs(live - documented) <= TOLERANCE, (
        f"suite collects {live} tests but docs say ~{documented}: "
        "re-sync README.md, docs/CONTRIBUTING_ONBOARDING.md, and CHANGELOG.md "
        "(pytest --collect-only -q; pytest -q)"
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
