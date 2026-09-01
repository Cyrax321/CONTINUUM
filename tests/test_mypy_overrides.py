"""Tests for the mypy overrides that keep a core install type-checkable (#315).

`mypy src/continuum` is one of the three checks `CONTRIBUTING.md` asks of a
contributor, and the core library declares exactly one dependency: pydantic.
Every optional package is imported lazily, behind a `try` or inside the call that
needs it, so a recovery tool never fails to import when it is needed most. That
leaves the type checker reading imports which resolve to nothing on a plain
`pip install -e .` clone, and without the per-module sections in `pyproject.toml`
that clone opens with 26 errors, none of them about the code.

These tests hold the config to the imports rather than to a list someone has to
remember to edit:

* Every third-party module `src/continuum` imports is covered by an
  `ignore_missing_imports` family, so a new optional import cannot land without
  one and quietly re-break a fresh clone.
* The strict relaxations that follow from those imports being `Any` stay scoped
  to modules that exist and hold an optional seam, so `strict = true` cannot be
  widened away under cover of this fix.
* All of the mypy config stays under `[tool.mypy]`, because the override that
  drifted below the coverage tables is how the missing families went unnoticed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE = REPO_ROOT / "src" / "continuum"

# pydantic is the one hard runtime dependency: always importable, and it ships
# its own types. Every other third-party import is optional by design.
ALWAYS_INSTALLED = {"pydantic", "pydantic_core"}

# The only strict checks an absent optional package is allowed to switch off.
# Both fire on `Any` that is only `Any` because the package is not installed.
RELAXABLE = {"disallow_subclassing_any", "disallow_untyped_decorators"}


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def _mypy_config() -> dict[str, object]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    config: dict[str, object] = data["tool"]["mypy"]
    return config


def _overrides() -> list[dict[str, object]]:
    overrides = _mypy_config().get("overrides")
    assert isinstance(overrides, list) and overrides, (
        "pyproject declares no [[tool.mypy.overrides]] sections"
    )
    return overrides


def _module_patterns(override: dict[str, object]) -> list[str]:
    modules = override["module"]
    if isinstance(modules, str):
        return [modules]
    assert isinstance(modules, list), f"`module` must be a string or a list, got {modules!r}"
    return [str(module) for module in modules]


def _excused_patterns() -> list[str]:
    """Every module pattern let off a missing implementation or stub."""
    patterns: list[str] = []
    for override in _overrides():
        if override.get("ignore_missing_imports"):
            patterns.extend(_module_patterns(override))
    return patterns


def _third_party_imports() -> dict[str, list[str]]:
    """Map each third-party module imported under `src/continuum` to its files.

    Walked with `ast` rather than by importing anything: the point is to see the
    imports an uninstalled environment would trip over, including the ones inside
    a function body or a `TYPE_CHECKING` block.
    """
    imported: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import has no third-party module to resolve.
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if root in sys.stdlib_module_names or root in {"continuum", "__future__"}:
                    continue
                if root in ALWAYS_INSTALLED:
                    continue
                imported.setdefault(name, set()).add(path.relative_to(REPO_ROOT).as_posix())
    return {name: sorted(files) for name, files in sorted(imported.items())}


def _module_is_real(module: str) -> bool:
    base = REPO_ROOT / "src" / Path(*module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_every_optional_import_is_excused() -> None:
    patterns = _excused_patterns()
    uncovered = {
        module: files
        for module, files in _third_party_imports().items()
        if not any(fnmatch(module, pattern) for pattern in patterns)
    }
    assert not uncovered, (
        "these modules are imported but not excused from missing-import errors, so "
        f"`mypy src/continuum` fails on an install that lacks them: {uncovered}. Add "
        "the family to [[tool.mypy.overrides]] in pyproject.toml."
    )


def test_strict_is_relaxed_only_for_the_modules_holding_an_optional_seam() -> None:
    assert _mypy_config().get("strict") is True, "the type-check gate is strict mode"

    for override in _overrides():
        relaxed = {key for key, value in override.items() if key != "module" and value is False}
        if not relaxed:
            continue
        assert relaxed <= RELAXABLE, (
            f"{sorted(relaxed)} switches off more than the checks an absent optional "
            f"package can account for ({sorted(RELAXABLE)})"
        )
        for module in _module_patterns(override):
            assert module.startswith("continuum."), (
                f"{module} is not a first-party module: relax strict only where the "
                "optional import is, never for a third-party family"
            )
            assert _module_is_real(module), (
                f"{module} has no file under src/, so this section relaxes nothing "
                "and mypy reports it as unused config"
            )


def test_mypy_config_is_not_split_across_the_file() -> None:
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    mypy_sections = [i for i, line in enumerate(lines) if line.startswith("[[tool.mypy.")]
    others = [
        i
        for i, line in enumerate(lines)
        if line.startswith("[tool.") and not line.startswith("[tool.mypy]")
    ]
    assert mypy_sections, "found no [[tool.mypy.overrides]] section to place"
    next_unrelated = min((i for i in others if i > min(mypy_sections)), default=len(lines))
    assert max(mypy_sections) < next_unrelated, (
        "a [[tool.mypy.overrides]] section sits below an unrelated [tool.*] table. "
        "Keep them together under [tool.mypy]: the one that drifted to the end of "
        "the file is why the missing families were not noticed (#315)."
    )
