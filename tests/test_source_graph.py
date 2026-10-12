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


def test_google_namespace_and_protobuf_scoping(tmp_path: Path) -> None:
    # protobuf should only match google.protobuf, never claiming unrelated google.* packages
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="demo"\ndependencies=["protobuf>=4.0"]\n',
    )
    mod_proto = _write(
        tmp_path,
        "proto_mod.py",
        "import google.protobuf\nfrom google import protobuf\n",
    )
    mod_cloud = _write(
        tmp_path,
        "cloud_mod.py",
        "import google.cloud.storage\nfrom google.cloud import storage\n",
    )
    graph = DependencyGraph(tmp_path)
    assert graph.declared == {"protobuf"}

    assert graph.owner_of(mod_proto) == {"protobuf"}
    assert graph.owner_of(mod_cloud) == set()

    assert graph.files_using("protobuf") == {mod_proto}
    assert graph.files_using("google.protobuf") == {mod_proto}
    assert graph.files_using("google.cloud") == {mod_cloud}

    assert graph.third_party_imports(mod_proto) == set()
    assert graph.third_party_imports(mod_cloud) == {"google.cloud"}


def test_pep503_canonicalization_and_distribution_spellings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "requirements.txt",
        "scikit_learn>=1.0\nopencv_python\npython_dateutil\n",
    )
    mod = _write(
        tmp_path,
        "mod.py",
        "import sklearn\nimport cv2\nimport dateutil\n",
    )
    graph = DependencyGraph(tmp_path)
    assert graph.declared == {"scikit_learn", "opencv_python", "python_dateutil"}
    assert graph.owner_of(mod) == {"scikit_learn", "opencv_python", "python_dateutil"}

    assert graph.files_using("scikit-learn") == {mod}
    assert graph.files_using("scikit_learn") == {mod}
    assert graph.files_using("sklearn") == {mod}
    assert graph.files_using("opencv-python") == {mod}
    assert graph.files_using("cv2") == {mod}
    assert graph.files_using("python-dateutil") == {mod}
    assert graph.files_using("dateutil") == {mod}

    assert graph.third_party_imports(mod) == set()


def test_installed_metadata_aliases_and_fallback(tmp_path: Path, monkeypatch) -> None:
    import importlib.metadata

    from continuum.analysis import depends

    mock_dists = {
        "custommod": ["custom-distribution"],
        "google": ["protobuf"],
    }
    monkeypatch.setattr(importlib.metadata, "packages_distributions", lambda: mock_dists)
    depends._installed_packages_distributions.cache_clear()
    depends._installed_dist_to_imports.cache_clear()
    depends._installed_import_to_dists.cache_clear()

    _write(
        tmp_path,
        "requirements.txt",
        "custom_distribution>=1.0\n",
    )
    mod = _write(
        tmp_path,
        "app.py",
        "import custommod\n",
    )
    graph = DependencyGraph(tmp_path)
    assert graph.owner_of(mod) == {"custom_distribution"}
    assert graph.files_using("custom-distribution") == {mod}
    assert graph.files_using("custom_distribution") == {mod}
    assert graph.files_using("custommod") == {mod}
    assert graph.third_party_imports(mod) == set()

    # Fallback when metadata access raises
    monkeypatch.setattr(
        importlib.metadata,
        "packages_distributions",
        lambda: (_ for _ in ()).throw(RuntimeError("metadata error")),
    )
    depends._installed_packages_distributions.cache_clear()
    depends._installed_dist_to_imports.cache_clear()
    depends._installed_import_to_dists.cache_clear()

    assert depends._installed_packages_distributions() == {}
    assert depends._installed_dist_to_imports() == {}
    assert depends._installed_import_to_dists() == {}

    fallback_graph = DependencyGraph(tmp_path, requirements=["foo"])
    assert fallback_graph.declared == {"foo"}

    # Restore clean caches
    monkeypatch.undo()
    depends._installed_packages_distributions.cache_clear()
    depends._installed_dist_to_imports.cache_clear()
    depends._installed_import_to_dists.cache_clear()


def test_custom_aliases_sequence_and_relative_path_resolution(tmp_path: Path) -> None:
    mod = _write(
        tmp_path,
        "worker.py",
        "import sub_a\nimport sub_b\n",
    )
    graph = DependencyGraph(
        tmp_path,
        requirements=["my-dist"],
        aliases={"my-dist": ["sub_a", "sub_b"]},
    )
    assert graph.owner_of(mod) == {"my-dist"}
    assert graph.owner_of(Path("worker.py")) == {"my-dist"}
    assert graph.owner_of(tmp_path / "absent.py") == set()

    assert graph.files_using("my-dist") == {mod}
    assert graph.files_using("sub_a") == {mod}
    assert graph.files_using("sub_b") == {mod}

    assert graph.third_party_imports(mod) == set()
    assert graph.third_party_imports(Path("worker.py")) == set()
    assert graph.third_party_imports(tmp_path / "absent.py") == set()


def test_scan_skips_excluded_dirs_and_syntax_errors(tmp_path: Path) -> None:
    _write(tmp_path, ".venv/ignored.py", "import requests\n")
    _write(tmp_path, "broken.py", "def broken syntax (\n")
    good = _write(tmp_path, "good.py", "import requests\n")
    graph = DependencyGraph(tmp_path, requirements=["requests"])
    assert graph.files_using("requests") == {good}
    assert graph.owner_of(good) == {"requests"}


def test_scan_os_error_handling(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        Path,
        "rglob",
        lambda self, pattern: (_ for _ in ()).throw(OSError("disk error")),
    )
    graph = DependencyGraph(tmp_path, requirements=["requests"])
    assert graph.files_using("requests") == set()


def test_malformed_pyproject_fallback(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[invalid toml format ==\n")
    graph = DependencyGraph(tmp_path)
    assert graph.declared == set()
