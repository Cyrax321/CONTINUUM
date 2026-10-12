from __future__ import annotations

from pathlib import Path

from continuum.analysis import DependencyGraph


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_pyproject_does_not_raise(tmp_path: Path) -> None:
    f = _write(tmp_path, "mod.py", "import os\n")
    graph = DependencyGraph(tmp_path)
    assert graph.declared == set()
    assert graph.owner_of(f) == set()


def test_requirements_txt_is_read(tmp_path: Path) -> None:
    _write(tmp_path, "requirements.txt", "numpy>=1.0\npandas==2.0\n# comment\n")
    graph = DependencyGraph(tmp_path)
    assert "numpy" in graph.declared and "pandas" in graph.declared
    assert "comment" not in graph.declared


def test_nested_imports_and_stdlib_vs_third_party(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\ndependencies=["numpy"]\n')
    f = _write(
        tmp_path / "pkg",
        "worker.py",
        "import os\nimport numpy\nimport pandas\n",
    )
    graph = DependencyGraph(tmp_path)
    assert graph.is_stdlib("os") is True
    assert graph.is_stdlib("numpy") is False
    # only declared deps are owners; pandas is imported but undeclared here
    assert graph.owner_of(f) == {"numpy"}
    assert f in graph.files_using("numpy")


def test_relative_imports_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\ndependencies=["numpy"]\n')
    f = _write(
        tmp_path / "pkg",
        "mod.py",
        "from .sibling import thing\nfrom . import other\nimport numpy\n",
    )
    graph = DependencyGraph(tmp_path)
    assert graph.owner_of(f) == {"numpy"}


def test_third_party_undeclared_reported(tmp_path: Path) -> None:
    f = _write(tmp_path, "mod.py", "import os\nimport requests\n")
    graph = DependencyGraph(tmp_path)
    assert graph.third_party_imports(f) == {"requests"}


def test_explicit_requirements_overrides_manifest(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\ndependencies=["numpy"]\n')
    graph = DependencyGraph(tmp_path, requirements=["pandas"])
    assert graph.declared == {"pandas"}


def test_mixed_case_and_distribution_aliases_agree(tmp_path: Path) -> None:
    # Regression test for issue #1606:
    # Declared names are lower-cased while imports may be mixed-case (FastAPI)
    # or use distinct import module names (Pillow -> PIL, PyYAML -> yaml).
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="demo"\ndependencies=["PyYAML>=6.0", "fastapi", "Pillow"]\n',
    )
    mod = _write(
        tmp_path,
        "mod.py",
        "from FastAPI import APIRouter\nfrom PIL import Image\nimport yaml\n",
    )
    graph = DependencyGraph(tmp_path)
    assert graph.declared == {"fastapi", "pillow", "pyyaml"}

    # owner_of identifies declared dependencies regardless of import casing or alias
    assert graph.owner_of(mod) == {"fastapi", "pillow", "pyyaml"}

    # files_using resolves via declared distribution name or import module name
    assert graph.files_using("fastapi") == {mod}
    assert graph.files_using("FastAPI") == {mod}
    assert graph.files_using("pillow") == {mod}
    assert graph.files_using("Pillow") == {mod}
    assert graph.files_using("PIL") == {mod}
    assert graph.files_using("pyyaml") == {mod}
    assert graph.files_using("PyYAML") == {mod}
    assert graph.files_using("yaml") == {mod}

    # All imports are declared, so none are reported as third-party undeclared
    assert graph.third_party_imports(mod) == set()


def test_custom_aliases_and_stdlib_casing(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "requirements.txt",
        "company-sdk>=1.0\n",
    )
    mod = _write(
        tmp_path,
        "worker.py",
        "import cProfile\nimport os\nfrom sdk.client import Client\nimport requests\n",
    )
    graph = DependencyGraph(tmp_path, aliases={"company-sdk": "sdk"})
    assert graph.is_stdlib("cProfile") is True
    assert graph.is_stdlib("OS") is True
    assert graph.owner_of(mod) == {"company-sdk"}
    assert graph.files_using("company-sdk") == {mod}
    assert graph.files_using("sdk") == {mod}
    assert graph.third_party_imports(mod) == {"requests"}
