"""DependencyGraph must not drop mixed-case or renamed dependencies (issue #1606).

``owner_of`` and ``files_using`` compared lower-cased declared names against
case-sensitive import names, so a dependency whose import name has an upper-case
letter, or whose import name is not a lower-casing of its distribution name,
matched nothing. Localized repair silently fell back to whole-state behaviour
for those dependencies.

Two gaps, each with a test:

* **case** -- ``CompanySdk`` is declared and imported as ``CompanySdk``; the
  declared name normalizes to ``companysdk`` while the import stayed ``CompanySdk``.
* **distribution vs import name** -- ``Pillow`` imports as ``PIL`` and ``PyYAML``
  imports as ``yaml``; no amount of case-folding reconciles those, so the alias
  table and ``importlib.metadata`` are the only paths that can.
"""

from __future__ import annotations

from pathlib import Path

from continuum.analysis import DependencyGraph


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest(root: Path, *deps: str) -> None:
    quoted = ", ".join(f'"{d}"' for d in deps)
    _write(root, "pyproject.toml", f'[project]\nname="x"\ndependencies=[{quoted}]\n')


def test_mixed_case_import_matches_its_declared_dependency(tmp_path: Path) -> None:
    _manifest(tmp_path, "CompanySdk")
    mod = _write(tmp_path, "mod.py", "import CompanySdk\n")

    graph = DependencyGraph(tmp_path)

    assert graph.declared == {"companysdk"}
    assert graph.owner_of(mod) == {"companysdk"}
    assert mod in graph.files_using("CompanySdk")


def test_mixed_case_dependency_is_not_reported_as_third_party(tmp_path: Path) -> None:
    _manifest(tmp_path, "CompanySdk")
    mod = _write(tmp_path, "mod.py", "import CompanySdk\n")

    assert DependencyGraph(tmp_path).third_party_imports(mod) == set()


def test_distribution_and_import_name_are_reconciled(tmp_path: Path) -> None:
    _manifest(tmp_path, "Pillow", "PyYAML")
    mod = _write(tmp_path, "mod.py", "import PIL\nimport yaml\n")

    graph = DependencyGraph(tmp_path)

    assert graph.owner_of(mod) == {"pillow", "pyyaml"}
    assert mod in graph.files_using("Pillow")
    assert mod in graph.files_using("PyYAML")


def test_renamed_distribution_is_not_reported_as_third_party(tmp_path: Path) -> None:
    _manifest(tmp_path, "Pillow")
    mod = _write(tmp_path, "mod.py", "import PIL\n")

    assert DependencyGraph(tmp_path).third_party_imports(mod) == set()


def test_case_folding_alone_reconciles_a_capitalized_import(tmp_path: Path) -> None:
    _manifest(tmp_path, "requests")
    mod = _write(tmp_path, "mod.py", "import Requests\nimport os\n")

    graph = DependencyGraph(tmp_path)

    # Case folding is the fix: ``Requests`` and ``requests`` are the same
    # dependency, so the import must be owned and must not read as third-party.
    assert graph.owner_of(mod) == {"requests"}
    assert graph.third_party_imports(mod) == set()


def test_an_undeclared_import_is_still_third_party(tmp_path: Path) -> None:
    _manifest(tmp_path, "numpy")
    mod = _write(tmp_path, "mod.py", "import pandas\n")

    graph = DependencyGraph(tmp_path)

    assert graph.owner_of(mod) == set()
    assert graph.third_party_imports(mod) == {"pandas"}


def test_owner_of_and_files_using_agree(tmp_path: Path) -> None:
    _manifest(tmp_path, "CompanySdk", "Pillow")
    mod = _write(tmp_path, "mod.py", "import CompanySdk\nimport PIL\n")

    graph = DependencyGraph(tmp_path)

    for owner in graph.owner_of(mod):
        assert mod in graph.files_using(owner)
