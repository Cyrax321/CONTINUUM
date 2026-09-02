"""Tests for the committed pre-commit configuration (issue #537).

The hooks are a convenience, but a hook that disagrees with CI is worse than no
hook: it formats a file one way locally and the same file fails
`ruff format --check` on the PR. These pin the two agreements that keep the
local gate and the CI gate honest about each other:

* **One ruff version.** `rev` in `.pre-commit-config.yaml`, and the copy of it
  quoted in `CONTRIBUTING.md`, name the same ruff the `dev` extra pins in
  `pyproject.toml`.
* **The same paths.** Every directory the CI lint job hands to ruff is in scope
  for both hooks, and a directory CI does not lint stays out of scope, so
  `pre-commit run --all-files` on an untouched tree has nothing to say.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def _pinned_ruff_version() -> str:
    """Return the exact ruff version pinned by pyproject's ``dev`` extra."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = data["project"]["optional-dependencies"]["dev"]
    pins = [spec.split("==", 1)[1] for spec in dev if spec.startswith("ruff==")]
    assert len(pins) == 1, f"expected exactly one ruff== pin in the dev extra, got {dev}"
    return pins[0]


def _hook_file_patterns() -> dict[str, str]:
    """Map each configured hook id to its ``files`` pattern.

    Parsed by line rather than with a YAML library: the config is four keys
    deep, and the dev extra installs no YAML parser.
    """
    patterns: dict[str, str] = {}
    hook_id: str | None = None
    for line in PRECOMMIT_CONFIG.read_text(encoding="utf-8").splitlines():
        identifier = re.match(r"\s*- id:\s*(\S+)\s*$", line)
        if identifier:
            hook_id = identifier.group(1)
            continue
        scope = re.match(r"\s*files:\s*(\S+)\s*$", line)
        if scope and hook_id is not None:
            patterns[hook_id] = scope.group(1)
    return patterns


def _ci_lint_directories() -> set[str]:
    """Return the directories the CI lint job passes to ruff."""
    directories: set[str] = set()
    for line in CI_WORKFLOW.read_text(encoding="utf-8").splitlines():
        invocation = re.match(r"\s*run:\s*ruff\s+(?:check|format)\s+(?P<rest>.+)$", line)
        if invocation:
            directories.update(
                token.rstrip("/")
                for token in invocation.group("rest").split()
                if token.endswith("/")
            )
    return directories


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_precommit_pins_the_ruff_version_from_the_dev_extra() -> None:
    expected = f"v{_pinned_ruff_version()}"
    for path in (PRECOMMIT_CONFIG, CONTRIBUTING):
        revisions = re.findall(r"^\s*rev:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), re.M)
        assert revisions == [expected], (
            f"{path.name} must name the ruff version pinned in pyproject's dev extra "
            f"({expected}), got {revisions}"
        )


def test_hooks_are_scoped_to_the_directories_ci_lints() -> None:
    patterns = _hook_file_patterns()
    assert set(patterns) == {"ruff-check", "ruff-format"}, (
        f"expected a scoped ruff-check and ruff-format hook, got {patterns}"
    )
    linted = _ci_lint_directories()
    assert linted, "found no ruff invocation in the CI lint job"
    # benchmarks/ is not ruff-clean and CI does not lint it: in scope, the hooks
    # would fail on a tree nobody touched.
    assert "benchmarks" not in linted
    for hook_id, pattern in patterns.items():
        for directory in linted:
            assert re.match(pattern, f"{directory}/module.py"), (
                f"{hook_id} does not cover {directory}/, which CI lints"
            )
        assert not re.match(pattern, "benchmarks/run.py"), (
            f"{hook_id} covers benchmarks/, which CI does not lint"
        )
